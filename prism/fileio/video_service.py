"""视频解码适配层：把两个后端包成同一组信号。

任务书 T9 要求「第三方库的调用必须放在对应 Adapter 内」，并点名
``VideoDecoderAdapter → VideoFrameService``。这就是那一层的实现。

**两个后端**

  QtMultimedia（`video_frames.py`）  默认。系统解码器，不用额外依赖。
  PyAV（`video_pyav.py`）            可选。415 种容器，快得多，代价是
                                     打包 +68 MB 和 GPL 的 FFmpeg 构建。

**后端什么时候定下来**

在**探测**阶段定，抽帧一路用它。不在抽帧中途切换 —— 抽到一半换解码器，
已经交出去的帧和后面的帧来自两套时间轴，结果没法解释。所以服务在探测
成功时记住这个 url 用哪个后端，抽帧时照着来。

**信号形状刻意和 `video_frames.FrameExtractor` 保持一致**

    ready(VideoInfo)                 探测成功
    failed(str)                      失败，说明可以直接给用户看
    progress(done, total)            抽帧进度
    kept_frame((QImage, position_ms)) 抽到一帧
    finished(kept, total)            抽帧结束
    video_info(VideoInfo)            解码器报的时长/分辨率
    position_changed(int)            当前扫描位置

所以 `widgets/extract_frames.py` 接上来时一行都不用改。

PyAV 特有的"跳过多少个坏包"走单独一个信号 ``skipped_packets(int)``，
QtMultimedia 永远不会发它。放在 finished 的参数里会逼着 UI 一起改，
而那是 T1 刚验收过的东西。
"""
import logging
import os

from PyQt6 import QtCore

from prism.fileio import video_pyav
from prism.fileio.video_frames import (COLOR_DISTANCE, COMPARE_WINDOW,
                                       DUPLICATE_DISTANCE)
from prism.similarity import (average_color, color_distance,
                              difference_hash, hamming_distance)

logger = logging.getLogger(__name__)

#: 后端标识。给界面显示和日志用。
BACKEND_QT = 'qtmultimedia'
BACKEND_PYAV = 'pyav'


def preferred_backend():
    """没别的原因时先用哪个后端。

    T1 的实测（tools/backend_compare_report.txt，五个样例两个后端各抽
    一遍）：**PyAV 全胜，没有一个 QtMultimedia 更快的**。最快的差 3.9
    倍，最悬殊的 315 倍（sample.ts 12.6s vs 0.04s）；sample.avi 更是
    只有 PyAV 能解码。

    QtMultimedia 仍然是**播放和画布预览**用的后端（任务书 T1 明确要求
    播放继续用它），只是抽帧不先找它。
    """
    return BACKEND_PYAV if video_pyav.available() else BACKEND_QT


def available_backends():
    """当前环境里能用哪些后端。"""
    names = [BACKEND_QT]
    if video_pyav.available():
        names.append(BACKEND_PYAV)
    return names


class VideoProbeAdapter(QtCore.QObject):
    """探测视频信息，QtMultimedia 不行就退 PyAV。

    对上层就是 `ready` / `failed` 两个信号，看不出里面换过手。
    """

    ready = QtCore.pyqtSignal(object)        # VideoInfo
    failed = QtCore.pyqtSignal(str)
    #: 实际用了哪个后端，成功时才发。给界面显示和排查用。
    backend_chosen = QtCore.pyqtSignal(str)

    def __init__(self, url, parent=None):
        super().__init__(parent)
        self._url = url
        self._probe = None
        #: 已经试过的后端。防止两个后端来回弹（A 失败退 B，B 失败退 A）。
        self._tried = set()
        self._backend = preferred_backend()

    @property
    def backend(self):
        return self._backend

    def start(self):
        self._start_backend(preferred_backend())

    def _start_backend(self, backend):
        """换成指定后端再探一次。"""
        self._backend = backend
        self._tried.add(backend)
        if backend == BACKEND_PYAV:
            self._probe = video_pyav.PyAVProbe(self._url, parent=self)
            self._probe.ready.connect(self._on_ready)
            self._probe.failed.connect(self._on_pyav_failed)
        else:
            from prism.fileio.video_frames import VideoProbe

            self._probe = VideoProbe(self._url, parent=self)
            self._probe.ready.connect(self._on_ready)
            self._probe.failed.connect(self._on_qt_failed)
        self._probe.start()

    def _fallback(self, other):
        """还有没试过、而且真的可用的后端就换过去，返回是否换了。

        「可用」这一条不能省：PyAV 没装的时候，QtMultimedia 失败之后会
        照样去建一个 PyAVProbe，然后在构造里炸掉。
        """
        if other in self._tried:
            return False
        if other == BACKEND_PYAV and not video_pyav.available():
            return False
        logger.info('%s 没读成，改用 %s 重试', self._backend, other)
        self._start_backend(other)
        return True

    @staticmethod
    def _metadata_is_usable(info):
        """这份探测结果值不值得用。

        「成功打开」不等于「读到了东西」：实测 QtMultimedia 对 mkv / ts
        会给出 0x0 的分辨率和 0.00 的帧率。这种结果不能让调用方拿去用，
        要换另一个后端再试一次。判据对两个后端一视同仁。
        """
        if info is None:
            return False
        if not getattr(info, 'width', 0) or not getattr(info, 'height', 0):
            return False
        if not getattr(info, 'frame_rate', 0):
            return False
        if getattr(info, 'duration_ms', 0) <= 0:
            return False
        return True

    def _on_ready(self, info):
        other = (BACKEND_QT if self._backend == BACKEND_PYAV
                 else BACKEND_PYAV)
        if not self._metadata_is_usable(info):
            logger.info(
                '%s 读到的元数据不可用（%sx%s, %s fps），换 %s 重试',
                self._backend, getattr(info, 'width', '?'),
                getattr(info, 'height', '?'), getattr(info, 'frame_rate', '?'),
                other)
            if self._fallback(other):
                return
            # 两个都读不出可用的元数据。还是把这份交出去 —— 总比什么都不
            # 说强，调用方至少能看到分辨率是 0x0。
            logger.warning('两个后端都没读出可用的元数据')
        self.backend_chosen.emit(self._backend)
        self.ready.emit(info)

    def _on_qt_failed(self, message):
        """QtMultimedia 打不开 —— PyAV 可能认得这个容器。"""
        if self._fallback(BACKEND_PYAV):
            return
        self.failed.emit(message)

    def _on_pyav_failed(self, message):
        """PyAV 打不开 —— 退回去试 QtMultimedia。

        这一条是换默认之后才需要的：以前 PyAV 是后备，失败就是终点。
        """
        if self._fallback(BACKEND_QT):
            return
        self.failed.emit(message)

    def cancel(self):
        if self._probe is not None:
            self._probe.cancel()

    def wait(self, timeout_ms=10000):
        probe = self._probe
        if probe is not None and hasattr(probe, 'wait'):
            return probe.wait(timeout_ms)
        return True


class VideoFrameExtractorAdapter(QtCore.QObject):
    """按时间间隔抽帧。

    ``backend`` 决定用哪个解码器 —— 正常由 `VideoFrameService` 从上一步
    探测的结果传进来，直接构造则走 QtMultimedia。
    """

    progress = QtCore.pyqtSignal(int, int)          # 保留帧数, 已处理
    kept_frame = QtCore.pyqtSignal(object)          # (QImage, position_ms)
    finished = QtCore.pyqtSignal(int, int)          # 保留帧数, 总数
    failed = QtCore.pyqtSignal(str)
    video_info = QtCore.pyqtSignal(object)          # VideoInfo
    position_changed = QtCore.pyqtSignal(int)
    #: PyAV 跳过多少个坏包。QtMultimedia 永远不发。
    skipped_packets = QtCore.pyqtSignal(int)

    def __init__(self, url, interval_seconds=2.0, backend=None,
                 dedup=True, parent=None):
        super().__init__(parent)
        self._url = url
        self._interval = interval_seconds
        # None 表示"调用方没指定，用当前默认"。写成 backend=BACKEND_QT
        # 会把默认值烤死在签名里，改了 preferred_backend() 也带不动它。
        self._backend = backend or preferred_backend()
        self._dedup = bool(dedup)
        self._extractor = None
        #: 已经交出去的帧的 (结构哈希, 平均色)，PyAV 那条路用它判重。
        self._hashes = []
        self._kept = 0

    @property
    def backend(self):
        return self._backend

    def _offer_pyav_frame(self, image, position_ms):
        """PyAV 那条路要自己判重。

        QtMultimedia 的 FrameExtractor 内置了这套判断（它那边的 _offer），
        PyAV 这条没有 —— 不在这里补上，用户勾了「去掉重复帧」切到 PyAV
        就静默失效了。判据和常量都用同一套，两条路的结果才可比。
        """
        if self._dedup:
            value = difference_hash(image)
            colour = average_color(image)
            if value is not None:
                for previous_hash, previous_colour in \
                        self._hashes[-COMPARE_WINDOW:]:
                    if (hamming_distance(value, previous_hash)
                            <= DUPLICATE_DISTANCE
                            and color_distance(colour, previous_colour)
                            <= COLOR_DISTANCE):
                        logger.trace('PyAV: 丢掉一个近似重复帧')
                        return
                self._hashes.append((value, colour))
        self._kept += 1
        self.kept_frame.emit((image, position_ms))

    def start(self):
        if self._backend == BACKEND_PYAV and video_pyav.available():
            self._start_pyav()
        else:
            # 在这里就定下来，不留给 _start_qt —— 两者一旦不一致，
            # backend 属性会说谎。
            self._backend = BACKEND_QT
            self._start_qt()

    def _start_qt(self):
        from prism.fileio.video_frames import FrameExtractor

        self._backend = BACKEND_QT
        extractor = FrameExtractor(
            self._url, self._interval, dedup=self._dedup, parent=self)
        extractor.progress.connect(self.progress)
        extractor.kept_frame.connect(self.kept_frame)
        extractor.failed.connect(self.failed)
        extractor.video_info.connect(self.video_info)
        extractor.position_changed.connect(self.position_changed)
        # QtMultimedia 这条路的 finished 是 (kept, total)，直接透传；
        # 它没有坏包的概念，所以 skipped_packets 一次都不发。
        extractor.finished.connect(self.finished)
        self._extractor = extractor
        extractor.start()

    def _start_pyav(self):
        from prism.fileio import video_pyav as backend

        extractor = backend.PyAVFrameExtractor(
            self._url, self._interval, parent=self)
        # PyAV 发的是 frame_ready(image, position) 两个参数。这里不直接转
        # 发 —— 先过一遍判重，让两条路的"去掉重复帧"行为一致；再由
        # _offer_pyav_frame 打包成 (image, position) 元组发出去。
        extractor.frame_ready.connect(self._offer_pyav_frame)
        extractor.progress.connect(self.progress)

        def on_finished(kept, seen, bad):
            if bad:
                self.skipped_packets.emit(bad)
            self.finished.emit(kept, seen)

        extractor.finished.connect(on_finished)
        extractor.failed.connect(self.failed)
        self._extractor = extractor
        extractor.start()

    def cancel(self):
        if self._extractor is not None:
            self._extractor.cancel()

    def wait(self, timeout_ms=10000):
        extractor = self._extractor
        if extractor is not None and hasattr(extractor, 'wait'):
            return extractor.wait(timeout_ms)
        return True


class VideoFrameService(QtCore.QObject):
    """抽帧这件事的入口：先探测定后端，再按那个后端抽。

    任务书 T9 要求「服务不得持有整个 `PrismGraphicsView`，依赖必须显式
    传入」。这里不持有任何界面对象 —— 它只是决定用哪个解码器，并把
    「这个 url 走哪条路」记下来，好让抽帧和探测用同一个后端。
    """

    #: 探测时定下来的后端名，供界面显示与排查。
    backend_used = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._chosen = {}

    def backend_for(self, url):
        """这个 url 之前探测出来用的是哪个后端。没探过就是默认。"""
        return self._chosen.get(self._key(url), preferred_backend())

    def probe(self, url):
        """造一个探测器。探测成功时记住它选了哪个后端。"""
        adapter = VideoProbeAdapter(url, parent=self)
        adapter.backend_chosen.connect(
            lambda name, key=self._key(url): self._remember(key, name))
        adapter.backend_chosen.connect(self.backend_used)
        return adapter

    def make_extractor(self, url, interval_seconds=2.0, dedup=True):
        """造一个抽帧器，用探测阶段定下来的那个后端。"""
        return VideoFrameExtractorAdapter(
            url, interval_seconds, backend=self.backend_for(url),
            dedup=dedup, parent=self)

    def forget(self, url):
        """忘掉某个 url 的后端选择（换文件时用）。"""
        self._chosen.pop(self._key(url), None)

    def clear(self):
        self._chosen.clear()

    def _remember(self, key, name):
        self._chosen[key] = name

    @staticmethod
    def _key(url):
        """把 QUrl 和路径归一到同一个 key。

        QUrl.toString() 给的是 ``file:///C:/...``，而 str(path) 给的是
        ``C:\\...`` —— 两者对不上，会让同一个视频在探测和抽帧时被当成
        两个不同的东西，从而用上不同的后端。normcase 顺手抹平 Windows
        上的大小写差异。
        """
        if url is None:
            return ''
        if isinstance(url, QtCore.QUrl):
            if url.isLocalFile():
                return os.path.normcase(url.toLocalFile())
            return url.toString()
        return os.path.normcase(str(url))
