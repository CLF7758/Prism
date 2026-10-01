"""Ask for a frame interval, then pull the frames out of a video."""

import logging
import os

from PyQt6 import QtCore, QtGui, QtWidgets

from prism.fileio.video_frames import format_timestamp
from prism.fileio.video_service import VideoFrameService
from prism.i18n import _

logger = logging.getLogger(__name__)

#: JPEG quality the frames are written with.  It used to be a spinbox in the
#: dialog; the user asked for it to go, so JPEG frames are saved at this fixed
#: value and PNG ones (lossless) never looked at it anyway.
JPEG_QUALITY = 95


class ExtractFramesDialog(QtWidgets.QDialog):
    """Pick how far apart the frames should be, then watch them arrive.

    The dialog owns the extraction and collects the kept frames; the caller
    decides where they end up on the canvas.
    """

    def __init__(self, view, video_item, parent=None):
        super().__init__(parent or view.window())
        self.view = view
        self.video_item = video_item
        self.images = []
        self._extractor = None
        self._probe = None
        # 探测和抽帧走同一个服务：它决定用哪个解码器（默认 QtMultimedia，
        # 打不开就退 PyAV），并保证两段用同一个后端 —— 探测时定的那个。
        # 抽到一半换解码器的话，前后的帧来自两套时间轴，结果没法解释。
        self._service = VideoFrameService(self)
        self._info = None
        self._position_ms = 0
        self._running = False

        self.setWindowTitle(_('Extract Frames from Video'))
        self.resize(440, 260)

        layout = QtWidgets.QVBoxLayout(self)

        name = getattr(video_item, 'filename', None) or _('this video')
        headline = QtWidgets.QLabel(
            _('Pull still frames out of “{name}”.').format(
                name=QtCore.QFileInfo(name).fileName()))
        headline.setWordWrap(True)
        layout.addWidget(headline)

        # Length, pixel size and frame rate, so the interval is chosen with
        # the real video in view instead of a guess.
        self.info_label = QtWidgets.QLabel(_('Reading the video…'))
        self.info_label.setWordWrap(True)
        layout.addWidget(self.info_label)

        form = QtWidgets.QHBoxLayout()
        form.addWidget(QtWidgets.QLabel('每隔'))
        self.interval = QtWidgets.QDoubleSpinBox()
        self.interval.setRange(0.2, 60.0)
        self.interval.setSingleStep(0.5)
        self.interval.setDecimals(1)
        self.interval.setValue(2.0)
        self.interval.setMinimumWidth(max(120, self.interval.fontMetrics().horizontalAdvance('60.0') + 76))
        self.interval.setStyleSheet(
            'QDoubleSpinBox { padding: 4px 24px 4px 8px; }'
            'QDoubleSpinBox QLineEdit { padding: 0; border: none; min-width: 0; background: transparent; }')
        form.addWidget(self.interval)
        form.addWidget(QtWidgets.QLabel('秒提取一张'))
        form.addStretch()
        layout.addLayout(form)

        self.dedup = QtWidgets.QCheckBox(_('Skip near-duplicate frames'))
        self.dedup.setChecked(True)
        self.dedup.setToolTip(_(
            'Frames that look the same as a recent one are dropped, so a '
            'video that holds a still shot does not produce a run of copies.'))
        layout.addWidget(self.dedup)

        # Output format.  PNG is lossless, which is what you want for
        # reference stills; JPEG is there for when the folder would
        # otherwise get too big.
        output = QtWidgets.QHBoxLayout()
        output.addWidget(QtWidgets.QLabel(_('Save as:')))
        self.format_choice = QtWidgets.QComboBox()
        self.format_choice.addItem(_('PNG (lossless)'), 'PNG')
        self.format_choice.addItem(_('JPEG (smaller)'), 'JPEG')
        output.addWidget(self.format_choice)

        output.addStretch()
        layout.addLayout(output)

        self.progress = QtWidgets.QProgressBar()
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        self.status = QtWidgets.QLabel(_('Ready.'))
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.preview = QtWidgets.QListWidget()
        self.preview.setIconSize(QtCore.QSize(120, 80))
        layout.addWidget(self.preview, 1)
        self.save_button = QtWidgets.QPushButton(_('Save Frames to Folder…'))
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self._save_images)
        layout.addWidget(self.save_button)
        self.resize(640, 520)
        self.buttons = QtWidgets.QDialogButtonBox()
        self.start_button = self.buttons.addButton(
            _('Start'), QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        self.close_button = self.buttons.addButton(
            _('Close'), QtWidgets.QDialogButtonBox.ButtonRole.RejectRole)
        self.start_button.clicked.connect(self._start)
        self.close_button.clicked.connect(self._on_close)
        layout.addWidget(self.buttons)

        if not getattr(video_item, '_video_url', None):
            self.start_button.setEnabled(False)
            self.info_label.setText('')
            self.status.setText(
                _('This video has no readable source, so frames cannot be '
                  'pulled from it.'))
        else:
            self._start_probe(video_item._video_url)

    # ── Video information ─────────────────────────────────────

    def _start_probe(self, url):
        """Ask the decoder what this video is, without pulling frames."""
        self._probe = self._service.probe(url)
        self._probe.ready.connect(self._on_info_ready)
        self._probe.failed.connect(self._on_info_failed)
        self._probe.start()

    def _on_info_ready(self, info):
        self._probe = None
        # The walk reports the same details once it has loaded the media;
        # an empty reading never overwrites a good one.
        if info.duration_ms or self._info is None:
            self._info = info
            self.info_label.setText(self._describe(info))

    def _on_info_failed(self, message):
        self._probe = None
        # Not fatal: the walk itself reports its own error with more
        # context, so this only has to stop the label from saying
        # "reading" forever.
        if self._info is None:
            self.info_label.setText(message)

    @staticmethod
    def _describe(info):
        """One line: how long, how big, how fast."""
        parts = []
        if info.duration_ms:
            parts.append(_('Length {duration}').format(
                duration=format_timestamp(info.duration_ms)))
        resolution = info.resolution_text()
        if resolution:
            parts.append(resolution)
        if info.frame_rate:
            parts.append(_('{fps} fps').format(fps='%.2f' % info.frame_rate))
        if not parts:
            return _('The decoder reported no details for this video.')
        return ' · '.join(parts)

    # ── Extraction ────────────────────────────────────────────

    def _start(self):
        url = getattr(self.video_item, '_video_url', None)
        if url is None:
            return
        # The walk reports the same details; keeping both players alive
        # would decode the video twice.
        if self._probe is not None:
            self._probe.cancel()
            self._probe = None
        self._running = True
        self.images = []
        self.preview.clear()
        self.save_button.setEnabled(False)
        self.start_button.setEnabled(False)
        self.interval.setEnabled(False)
        self.dedup.setEnabled(False)
        self.progress.setRange(0, 0)             # busy until the total is known
        self.status.setText(_('Opening the video…'))

        self._extractor = self._service.make_extractor(
            url, self.interval.value(), dedup=self.dedup.isChecked())
        self._extractor.progress.connect(self._on_progress)
        self._extractor.kept_frame.connect(self._on_kept_frame)
        self._extractor.finished.connect(self._on_finished)
        self._extractor.failed.connect(self._on_failed)
        self._extractor.video_info.connect(self._on_info_ready)
        self._extractor.position_changed.connect(self._on_position)
        self._extractor.start()

    def _on_position(self, position_ms):
        """Remember where the walk is, so progress can name the moment."""
        self._position_ms = position_ms
        total = self._info.duration_ms if self._info else 0
        if total:
            self.progress.setFormat(
                f'%p%%  ·  {format_timestamp(position_ms)}'
                f' / {format_timestamp(total)}')

    def _on_progress(self, done, total):
        if self.progress.maximum() != total:
            self.progress.setRange(0, max(total, 1))
        self.progress.setValue(done)
        self.status.setText(
            _('At {stamp}: checked {done} of {total} frames, kept {kept}.')
            .format(stamp=format_timestamp(self._position_ms), done=done,
                    total=total, kept=len(self.images)))

    def _on_kept_frame(self, payload):
        # (QImage, position_ms) - the timestamp is kept so the canvas item
        # can say which moment of the video it came from.
        self.images.append(payload)
        image, position = payload
        row = QtWidgets.QListWidgetItem(
            _('{seconds:.2f} s').format(seconds=position / 1000))
        row.setIcon(QtGui.QIcon(QtGui.QPixmap.fromImage(image).scaled(
            120, 80, QtCore.Qt.AspectRatioMode.KeepAspectRatio)))
        self.preview.addItem(row)

    def _on_finished(self, kept, total):
        self._running = False
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(total)
        self.start_button.setEnabled(True)
        self.interval.setEnabled(True)
        self.dedup.setEnabled(True)
        self.status.setText(
            _('Done: kept {kept} of {total} sampled frames.')
            .format(kept=kept, total=total))
        if kept:
            self.close_button.setText(_('Add to Canvas'))
            self.save_button.setEnabled(True)

    def _save_images(self):
        directory = QtWidgets.QFileDialog.getExistingDirectory(
            self, _('Choose a folder for the frames'))
        if not directory:
            return
        image_format = self.format_choice.currentData() or 'PNG'
        extension = '.jpg' if image_format == 'JPEG' else '.png'
        # 质量不再是用户能调的东西：JPEG 按这个固定值存，PNG 本来就不看它。
        quality = JPEG_QUALITY
        saved = 0
        for index, (image, position) in enumerate(self.images, 1):
            stem = f'frame_{index:04d}_{position:010d}ms'
            path = os.path.join(directory, stem + extension)
            counter = 1
            while os.path.exists(path):
                path = os.path.join(
                    directory, f'{stem}_{counter}{extension}')
                counter += 1
            if image.save(path, image_format, quality):
                saved += 1
        self.status.setText(
            _('Saved {saved} of {total} frames to {directory}').format(
                saved=saved, total=len(self.images), directory=directory))

    def _on_failed(self, message):
        self._running = False
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.start_button.setEnabled(True)
        self.interval.setEnabled(True)
        self.dedup.setEnabled(True)
        self.status.setText(message)

    def _stop_work(self):
        """Cancel whatever is still running so nothing outlives the dialog."""
        if self._probe is not None:
            self._probe.cancel()
            self._probe = None
        if self._running and self._extractor is not None:
            self._extractor.cancel()
            self._running = False

    def _on_close(self):
        self._stop_work()
        self.accept()

    def reject(self):
        self._stop_work()
        super().reject()
