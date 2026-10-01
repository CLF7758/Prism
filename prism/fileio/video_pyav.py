"""PyAV 后端的视频探测与抽帧（可选）。

T1 的评估结论在 `tools/t1_pyav_report.md`：PyAV 能读 **415 种容器**，
60 秒视频抽帧只要 **0.13-0.19 秒**，代价是打包体积 **+68 MB** 和捆绑
**GPL 的 FFmpeg 构建**（libx264/libx265）。

这个后端**是可选的**：PyAV 不在时 `available()` 返回 False，调用方回落
到 QtMultimedia。默认仍然走 QtMultimedia，PyAV 用于它读不了的容器。

接口刻意和 QtMultimedia 那条路保持一致（QObject + 信号），上层不用关心
用的是哪个后端。信号从工作线程发出，Qt 的队列连接会让槽跑在主线程，所以
接收方不需要自己处理线程问题。

**坏包必须容忍**：评估时发现直接顺序解码会在中途抛 InvalidDataError
整体失败，而**同一个文件 QtMultimedia 能读**。所以这里逐 packet 解码、
跳过坏包继续，并把跳过的数量报给用户 —— 严格模式的结果只能是"整体
失败"，宽容模式才说得出"抽到 N 帧、跳过 M 个坏包"。
"""
import logging
import os

from PyQt6 import QtCore, QtGui

from prism.fileio.video_frames import VideoInfo

logger = logging.getLogger(__name__)

try:
    import av
    PYAV_AVAILABLE = True
except ImportError:                                     # pragma: no cover
    av = None
    PYAV_AVAILABLE = False

#: 打开容器时可以传的选项。``ignore_err`` 让解码器丢掉它判定为坏的数据，
#: 而不是整体放弃 —— 有些录像就是这样，QtMultimedia 能放，PyAV 默认不容忍。
OPEN_OPTIONS = {'err_detect': 'ignore_err'}


def available():
    """PyAV 是否可用。"""
    return PYAV_AVAILABLE


def unavailable_reason():
    """给用户看的说法，PyAV 不在时用。"""
    if PYAV_AVAILABLE:
        return ''
    return 'PyAV 未安装，用它打开这种容器需要先装上 av'


def local_path(url):
    """QUrl 或字符串 → 本地路径。"""
    if url is None:
        return ''
    if isinstance(url, QtCore.QUrl) and url.isLocalFile():
        return url.toLocalFile()
    return str(url)


def frame_to_qimage(frame):
    """PyAV 的一帧 → QImage。

    不能借 video_frames.frame_to_pixmap —— 那个收的是 QImage，是给
    QtMultimedia 的 QVideoFrame 那条路用的。PyAV 的帧要先变成 numpy 数组。

    ``to_ndarray`` 给出 ``(高, 宽, 3)`` 的 uint8 缓冲；RGB888 每行正好
    是 ``宽 x 3`` 字节，不需要考虑行对齐。
    """
    array = frame.to_ndarray(format='rgb24')
    height, width = array.shape[:2]
    # copy() 不能省：QImage 不持有传进去的那块内存，numpy 数组一被回收
    # 像素就没了，画布上会出现花屏或直接崩。
    buffer = array.tobytes()
    image = QtGui.QImage(buffer, width, height, width * 3,
                         QtGui.QImage.Format.Format_RGB888)
    return image.copy()


def _probe_sync(path):
    """读容器信息。同步，调用方负责放进线程。"""
    with av.open(path, options=OPEN_OPTIONS) as container:
        video = next((s for s in container.streams if s.type == 'video'),
                     None)
        if video is None:
            raise ValueError('这个文件里没有视频流')
        context = video.codec_context
        duration_ms = 0
        if container.duration:
            duration_ms = int(container.duration / av.time_base * 1000)
        rate = float(video.average_rate) if video.average_rate else 0.0
        return VideoInfo(
            duration_ms=duration_ms,
            width=context.width,
            height=context.height,
            frame_rate=rate,
            has_audio=any(s.type == 'audio' for s in container.streams),
        )


class _ProbeThread(QtCore.QThread):
    done = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self._path = path
        self._stop = False

    def request_stop(self):
        self._stop = True

    def run(self):
        try:
            info = _probe_sync(self._path)
        except Exception as error:
            logger.info('PyAV could not probe %s: %s', self._path, error)
            self.failed.emit(str(error))
            return
        if not self._stop:
            self.done.emit(info)


class PyAVProbe(QtCore.QObject):
    """探测视频的基本信息。信号和 QtMultimedia 的 VideoProbe 一致。"""

    ready = QtCore.pyqtSignal(object)        # VideoInfo
    failed = QtCore.pyqtSignal(str)

    def __init__(self, url, parent=None):
        super().__init__(parent)
        self._path = local_path(url)
        self._thread = None

    def start(self):
        if not PYAV_AVAILABLE:
            self.failed.emit(unavailable_reason())
            return
        if not self._path or not os.path.exists(self._path):
            self.failed.emit(f'找不到文件：{self._path}')
            return
        self._thread = _ProbeThread(self._path, self)
        self._thread.done.connect(self.ready)
        self._thread.failed.connect(self.failed)
        self._thread.start()

    def cancel(self):
        if self._thread is not None:
            self._thread.request_stop()

    def wait(self, timeout_ms=10000):
        """等线程结束。测试和关闭流程用。"""
        if self._thread is not None:
            return self._thread.wait(timeout_ms)
        return True


class _ExtractThread(QtCore.QThread):
    progress = QtCore.pyqtSignal(int, int)
    frame_ready = QtCore.pyqtSignal(object, int)
    done = QtCore.pyqtSignal(int, int, int)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, path, interval, parent=None):
        super().__init__(parent)
        self._path = path
        self._interval = interval
        self._stop = False

    def request_stop(self):
        self._stop = True

    def run(self):
        kept = 0
        seen = 0
        bad_packets = 0
        last_ms = -10 ** 9
        step_ms = max(1, int(self._interval * 1000))
        try:
            with av.open(self._path, options=OPEN_OPTIONS) as container:
                video = next(
                    (s for s in container.streams if s.type == 'video'), None)
                if video is None:
                    self.failed.emit('这个文件里没有视频流')
                    return
                # 按包解码而不是 container.decode()：一个坏包不该让整段抽帧
                # 作废。实测源文件里就有一个，而 QtMultimedia 读得过去。
                for packet in container.demux(video):
                    if self._stop:
                        break
                    try:
                        frames = packet.decode()
                    except av.error.FFmpegError:
                        bad_packets += 1
                        continue
                    for frame in frames:
                        if self._stop:
                            break
                        seen += 1
                        position_ms = 0
                        if frame.pts is not None:
                            position_ms = int(
                                frame.pts * frame.time_base * 1000)
                        if position_ms - last_ms < step_ms:
                            continue
                        last_ms = position_ms
                        try:
                            image = frame_to_qimage(frame)
                        except Exception:
                            logger.debug('Could not convert a frame at %d ms',
                                         position_ms, exc_info=True)
                            continue
                        if image.isNull():
                            continue
                        kept += 1
                        self.frame_ready.emit(image, position_ms)
                        self.progress.emit(kept, seen)
        except Exception as error:
            logger.info('PyAV extraction failed for %s: %s', self._path, error)
            self.failed.emit(str(error))
            return
        self.done.emit(kept, seen, bad_packets)


class PyAVFrameExtractor(QtCore.QObject):
    """按时间间隔抽帧，跳过坏包。

    信号和 QtMultimedia 那条路一致，上层不用改：

        progress(保留帧数, 已处理帧数)
        frame_ready(QImage, 毫秒位置)
        finished(保留帧数, 扫到的总帧数, 跳过的坏包数)
        failed(说明)
    """

    progress = QtCore.pyqtSignal(int, int)
    frame_ready = QtCore.pyqtSignal(object, int)
    finished = QtCore.pyqtSignal(int, int, int)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, url, interval_seconds=2.0, parent=None):
        super().__init__(parent)
        self._path = local_path(url)
        self._interval = float(interval_seconds)
        self._thread = None

    def start(self):
        if not PYAV_AVAILABLE:
            self.failed.emit(unavailable_reason())
            return
        if not self._path or not os.path.exists(self._path):
            self.failed.emit(f'找不到文件：{self._path}')
            return
        self._thread = _ExtractThread(self._path, self._interval, self)
        self._thread.progress.connect(self.progress)
        self._thread.frame_ready.connect(self.frame_ready)
        self._thread.done.connect(self.finished)
        self._thread.failed.connect(self.failed)
        self._thread.start()

    def cancel(self):
        if self._thread is not None:
            self._thread.request_stop()

    def wait(self, timeout_ms=10000):
        """等线程结束。测试和关闭流程用。"""
        if self._thread is not None:
            return self._thread.wait(timeout_ms)
        return True
