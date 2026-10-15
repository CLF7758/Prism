"""Windows global shortcut adapter for the existing screenshot action."""
import ctypes
import logging
import sys
from PyQt6 import QtCore, QtGui, QtWidgets

logger = logging.getLogger(__name__)


class CaptureHotkey(QtCore.QAbstractNativeEventFilter):
    IDENTIFIER = 0x5052

    def __init__(self, view, action):
        super().__init__()
        self.view, self.action = view, action
        self.registered = False
        self.current = None
        self.installed = False
        self.app = QtWidgets.QApplication.instance()
        if sys.platform == 'win32' and self.app.platformName() == 'windows':
            self.app.installNativeEventFilter(self)
            self.installed = True
            action.changed.connect(self.refresh)
            self.refresh()
            self.prewarm_timer = QtCore.QTimer(view)
            self.prewarm_timer.setSingleShot(True)
            self.prewarm_timer.timeout.connect(self.prewarm)
            self.prewarm_timer.start(3000)

    def prewarm(self):
        from prism.capture_service import component_command
        from prism.actions.capture_workflow import service
        try:
            component_command()
            service(self.view).start()
        except (OSError, RuntimeError) as error:
            logger.info('Screenshot helper prewarm unavailable: %s', error)

    def refresh(self):
        sequence = self.action.shortcut()
        value = sequence.toString(QtGui.QKeySequence.SequenceFormat.PortableText)
        if value == self.current:
            return
        self.current = value
        self.unregister()
        if sequence.isEmpty() or sequence.count() != 1:
            return
        combination = sequence[0]
        key = int(combination.key())
        if not (ord('0') <= key <= ord('Z')):
            return
        modifiers = combination.keyboardModifiers()
        mask = 0x4000  # MOD_NOREPEAT
        for flag, bit in [(QtCore.Qt.KeyboardModifier.AltModifier, 1),
                          (QtCore.Qt.KeyboardModifier.ControlModifier, 2),
                          (QtCore.Qt.KeyboardModifier.ShiftModifier, 4),
                          (QtCore.Qt.KeyboardModifier.MetaModifier, 8)]:
            if modifiers & flag:
                mask |= bit
        self.registered = bool(ctypes.windll.user32.RegisterHotKey(None, self.IDENTIFIER, mask, key))
        if not self.registered:
            logger.warning('Screenshot global shortcut is unavailable: %s', value)
            self.action.setToolTip(f'全局截图快捷键 {value} 被占用，请在快捷键设置中更换')
        else:
            self.action.setToolTip(f'屏幕截图（全局快捷键 {value}）')

    def nativeEventFilter(self, event_type, message):
        from ctypes import wintypes
        native = ctypes.cast(int(message), ctypes.POINTER(wintypes.MSG)).contents
        if native.message == 0x0312 and native.wParam == self.IDENTIFIER:
            QtCore.QTimer.singleShot(0, self.view.on_action_screen_capture)
            return True, 0
        return False, 0

    def unregister(self):
        if self.registered:
            ctypes.windll.user32.UnregisterHotKey(None, self.IDENTIFIER)
            self.registered = False

    def close(self):
        if hasattr(self, 'prewarm_timer'):
            self.prewarm_timer.stop()
        self.unregister()
        if self.installed:
            self.app.removeNativeEventFilter(self)
            self.action.changed.disconnect(self.refresh)
            self.installed = False
