"""Interactive native preview host: PyQt6 window + libf3d in one process.

This module is the adaptation of F3D's ``minimal_qt6.py`` example to Prism:

* the widget draws into the Qt OpenGL context (``Engine.create_external``), so
  no native window is embedded into another process' window;
* Prism's own window shell (``prism.widgets.model_preview.ModelPreviewWindow``)
  is instantiated **inside this process** and receives the viewport through
  ``install_viewport``; no QWidget ever crosses a process boundary;
* the toolbar's mode/reset/cover requests arrive as IPC messages and the
  answers travel back as protocol events, so the main process never renders.

Rendering is demand driven: a frame is drawn when the camera, the mode, the
appearance or the widget size changed. There is no always-on 30 ms tick; the
example's timer exists for animation playback only.

Colour and light come from :mod:`prism.components.model_worker`'s
``ENVIRONMENT``/``ENVIRONMENT_REVISION``, so a cover and this view cannot drift
apart without the revision changing.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any, Mapping, Optional

from prism.model_library import native_protocol as wire

MODE_MATERIAL = 'material'
MODE_MATERIAL_EDGES = 'material_edges'
MODE_WIREFRAME = 'wireframe'


def _ensure_native_first() -> None:
    """Import libf3d before Qt in this process. Not optional.

    Measured on the pinned runtime: importing PyQt6 first makes ``pyf3d`` fail
    with "DLL load failed ... 初始化例程失败" because the Qt and F3D wheels ship
    overlapping native dependencies. Importing F3D first is fine — so the main
    Prism process, which is already Qt, must never import F3D at all.
    """
    import f3d  # noqa: F401 - imported for its side effect, before any Qt import


def _environment() -> tuple[dict, int]:
    from prism.components.model_worker import ENVIRONMENT, ENVIRONMENT_REVISION
    return dict(ENVIRONMENT), int(ENVIRONMENT_REVISION)


# ── the native viewport ───────────────────────────────────────────────────
def _qt():
    from PyQt6 import QtCore, QtGui, QtWidgets, QtOpenGLWidgets
    return QtCore, QtGui, QtWidgets, QtOpenGLWidgets


def _base_widget_class():
    _, _, QtWidgets, QtOpenGLWidgets = _qt()
    return QtOpenGLWidgets.QOpenGLWidget


def build_viewport_class():
    """Create the widget class with Qt already imported (import stays lazy)."""
    QtCore, QtGui, QtWidgets, QtOpenGLWidgets = _qt()

    class F3DViewport(QtOpenGLWidgets.QOpenGLWidget):
        """libf3d drawing into Qt's OpenGL context. Preview process only."""

        mode_changed = QtCore.pyqtSignal(str)
        cover_ready = QtCore.pyqtSignal(str)
        native_error = QtCore.pyqtSignal(str, str)
        camera_changed = QtCore.pyqtSignal(object)
        model_loaded = QtCore.pyqtSignal(object)
        model_presented = QtCore.pyqtSignal(object)

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
            self.setMinimumSize(320, 240)
            self.engine = None
            self.interactor = None
            self._pending: Optional[dict] = None
            self._mode = MODE_MATERIAL
            self._applied: dict = {}
            self._snapshot: Optional[dict] = None
            self._initialized = False
            self._dirty = True
            self._frames = 0
            self._missing_symbols: list = []
            self._wireframe_available = False
            self._waiting_frame = False
            self._source_path = None
            self._display_path = None
            self._channel_options = {}
            import tempfile
            self.channel_cache_root = Path(tempfile.gettempdir()) / 'Prism' / 'model-channels'

        # -- context ---------------------------------------------------
        def _proc_address(self, name):
            context = self.context()
            if context is None:
                return 0
            address = context.getProcAddress(name)
            if address is None:
                self._missing_symbols.append(str(name))
                return 0
            try:
                return int(address)
            except (TypeError, ValueError):
                self._missing_symbols.append(str(name))
                return 0

        def initializeGL(self):
            import f3d

            context = self.context()
            if context is None:
                self.native_error.emit(wire.ERR_NATIVE_CRASH, '没有可用的 OpenGL 上下文')
                return
            f3d.Engine.autoload_plugins()
            try:
                self.engine = f3d.Engine.create_external(self._proc_address)
            except Exception as error:  # noqa: BLE001 - reported to the host
                self.engine = None
                self.native_error.emit(wire.ERR_NATIVE_CRASH,
                                       '无法在外部队下文创建 F3D 引擎：%s' % error)
                return
            self.interactor = self.engine.interactor
            options, _ = _environment()
            for key, value in options.items():
                self.engine.options[key] = value
            self.engine.window.set_use_hdri_cache(False)
            self._resize_engine(self.width(), self.height())
            self._initialized = True
            if self._pending is not None:
                pending, self._pending = self._pending, None
                self._apply_request(pending)
            self._dirty = True

        def paintGL(self):
            if self.engine is None:
                return
            self.engine.window.render()
            self._frames += 1
            self._dirty = False
            if self._waiting_frame:
                self._waiting_frame = False
                self.model_presented.emit(self.scene_info())

        def resizeGL(self, width, height):
            self._resize_engine(width, height)

        def _resize_engine(self, width, height):
            if self.engine is None:
                return
            ratio = float(self.devicePixelRatioF() or 1.0)
            self.engine.window.size = (max(1, int(round(width * ratio))),
                                       max(1, int(round(height * ratio))))
            self._dirty = True

        def close_native(self):
            """Release the native engine exactly once, before the widget dies."""
            engine, self.engine = self.engine, None
            self.interactor = None
            self._initialized = False
            if engine is not None:
                try:
                    engine.scene.clear()
                except Exception:  # noqa: BLE001 - a lost context cannot be cleared
                    pass
                del engine

        def closeEvent(self, event):
            self.close_native()
            super().closeEvent(event)

        # -- requests --------------------------------------------------
        def request_load(self, request: Mapping[str, Any]) -> None:
            """Load a model. Applied immediately when the context already exists."""
            if self._initialized:
                self.makeCurrent()
                try:
                    self._apply_request(dict(request))
                finally:
                    self.doneCurrent()
            else:
                self._pending = dict(request)
                self.update()

        def _apply_request(self, request: Mapping[str, Any]) -> None:
            path = str(request.get('model_path') or '')
            if not path:
                self.native_error.emit(wire.ERR_MISSING_DEPENDENCY, '请求缺少模型路径')
                return
            try:
                for key in set(self._channel_options) | set(self._applied):
                    self.engine.options.remove_value(key)
                self.engine.scene.clear()
                self.engine.scene.add(path)
            except Exception as error:  # noqa: BLE001 - reported to the host
                self.native_error.emit(wire.ERR_BAD_FILE, str(error))
                return
            self._applied = {}
            self._channel_options = {}
            self._source_path = path
            self._display_path = path
            self.apply_snapshot(request.get('parameter_snapshot'))
            self.set_mode(str(request.get('mode') or MODE_MATERIAL))
            camera = request.get('camera')
            if camera:
                self.apply_camera(camera)
            else:
                self.engine.window.camera.reset_to_bounds(0.9)
            self._dirty = True
            self.update()
            self._waiting_frame = True
            self.model_loaded.emit(self.scene_info())

        def apply_snapshot(self, payload: Optional[Mapping[str, Any]]) -> None:
            from prism.model_library.f3d_parameters import ParameterSnapshot

            if self.engine is None:
                return
            snapshot = ParameterSnapshot.from_payload(payload)
            values = snapshot.option_values()
            for key in list(self._applied):
                if key not in values:
                    try:
                        self.engine.options.remove_value(key)
                    except Exception:  # noqa: BLE001
                        pass
            for key, value in values.items():
                self.engine.options[key] = value
            self._applied = values
            self._snapshot = dict(payload or {})
            self._dirty = True
            self.update()

        def set_mode(self, mode: str) -> bool:
            """Switch display mode. A true wireframe is refused, never faked."""
            if self.engine is None or not self._source_path:
                return False
            mode = str(mode or MODE_MATERIAL)
            if mode == MODE_WIREFRAME and not self._wireframe_available:
                self.native_error.emit(
                    wire.ERR_UNSUPPORTED_CAPABILITY,
                    'F3D 4.0.0 只有边线叠加，没有纯线框表示；未打补丁前不提供')
                return False
            from prism.model_library.preview_channels import CHANNELS, diagnostic
            if mode not in (MODE_MATERIAL, MODE_MATERIAL_EDGES, MODE_WIREFRAME) + CHANNELS:
                self.native_error.emit(wire.ERR_BAD_PROTOCOL, '未知显示模式：%s' % mode)
                return False
            previous_path = self._display_path
            previous_options = dict(self._channel_options)
            camera = self.camera_state()
            try:
                target, channel_options = Path(self._source_path), {}
                if mode in CHANNELS:
                    digest = hashlib.sha256((str(self._source_path) + json.dumps(self._snapshot or {}, sort_keys=True)).encode()).hexdigest()
                    target, channel_options = diagnostic(self._source_path, mode,
                        self._snapshot or {}, self.channel_cache_root / digest / mode)
                for key in self._channel_options:
                    try:
                        self.engine.options.remove_value(key)
                    except Exception:
                        pass
                # Global texture overrides alter imported actor materials too;
                # resetting the option alone does not restore FBX/OBJ bindings.
                if str(target) != self._display_path or previous_options != channel_options:
                    self.engine.scene.clear()
                    self.engine.scene.add(str(target))
                    self._display_path = str(target)
                    self.apply_camera(camera)
                self.apply_snapshot(self._snapshot)
                for key, value in channel_options.items():
                    self.engine.options[key] = value
                self._channel_options = channel_options
            except Exception as error:
                # A failed diagnostic must leave the last successful view usable.
                try:
                    for key in channel_options:
                        self.engine.options.remove_value(key)
                    if previous_path and str(target) != previous_path:
                        self.engine.scene.clear()
                        self.engine.scene.add(previous_path)
                        self.apply_camera(camera)
                    self._display_path = previous_path
                    self.apply_snapshot(self._snapshot)
                    for key, value in previous_options.items():
                        self.engine.options[key] = value
                    self._channel_options = previous_options
                except Exception:
                    pass
                self.native_error.emit(wire.ERR_UNSUPPORTED_CAPABILITY, str(error))
                return False
            self.engine.options['render.show_edges'] = (mode == MODE_MATERIAL_EDGES)
            self._mode = mode
            self._dirty = True
            self.update()
            self.mode_changed.emit(mode)
            return True

        @property
        def mode(self) -> str:
            return self._mode

        def set_wireframe_available(self, available: bool) -> None:
            self._wireframe_available = bool(available)

        # -- camera ----------------------------------------------------
        def apply_camera(self, camera: Mapping[str, Any]) -> None:
            if self.engine is None:
                return
            window = self.engine.window
            if str(camera.get('preset') or '') == 'reset' or not camera.get('position'):
                window.camera.reset_to_bounds(float(camera.get('zoom') or 0.9))
            else:
                window.camera.position = tuple(float(v) for v in camera['position'])
                window.camera.focal_point = tuple(float(v) for v in camera['focal_point'])
                window.camera.view_up = tuple(float(v) for v in camera['view_up'])
                if camera.get('view_angle'):
                    window.camera.view_angle = float(camera['view_angle'])
            self._dirty = True
            self.update()

        def reset_camera(self) -> dict:
            if self.engine is None:
                return {}
            self.engine.window.camera.reset_to_bounds(0.9)
            self._dirty = True
            self.update()
            state = self.camera_state()
            self.camera_changed.emit(state)
            return state

        def camera_state(self) -> dict:
            if self.engine is None:
                return {}
            state = self.engine.window.camera.state
            return {'position': list(state.position),
                    'focal_point': list(state.focal_point),
                    'view_up': list(state.view_up),
                    'view_angle': float(state.view_angle)}

        def scene_info(self) -> dict:
            if self.engine is None:
                return {}
            info = self.engine.scene.get_scene_info()
            return {'actors': int(info.number_of_actors),
                    'points': int(info.number_of_points),
                    'cells': int(info.number_of_cells)}

        # -- cover -----------------------------------------------------
        def capture_cover(self, destination: str) -> dict:
            """Write the current frame as a cover and report the exact camera."""
            if self.engine is None:
                raise RuntimeError('原生预览尚未初始化')
            self.makeCurrent()
            try:
                self.engine.window.render()
                image = self.engine.window.render_to_image()
            finally:
                self.doneCurrent()
            path = Path(destination)
            path.parent.mkdir(parents=True, exist_ok=True)
            staged = path.with_name(path.name + '.part')
            image.save(str(staged))
            os.replace(str(staged), str(path))
            data = path.read_bytes()
            self._dirty = True
            self.cover_ready.emit(str(path))
            return {'path': str(path), 'sha256': hashlib.sha256(data).hexdigest(),
                    'bytes': len(data), 'width': int(image.width),
                    'height': int(image.height), 'camera': self.camera_state(),
                    'mode': self._mode}

        # -- event forwarding -----------------------------------------
        def _update_modifiers(self, modifiers) -> None:
            QtCore, _, _, _ = _qt()
            if self.interactor is None:
                return
            modifier = self.interactor.InputModifier
            if (modifiers & QtCore.Qt.KeyboardModifier.ControlModifier) and (
                    modifiers & QtCore.Qt.KeyboardModifier.ShiftModifier):
                self.interactor.trigger_mod_update(modifier.CTRL_SHIFT)
            elif modifiers & QtCore.Qt.KeyboardModifier.ControlModifier:
                self.interactor.trigger_mod_update(modifier.CTRL)
            elif modifiers & QtCore.Qt.KeyboardModifier.ShiftModifier:
                self.interactor.trigger_mod_update(modifier.SHIFT)
            else:
                self.interactor.trigger_mod_update(modifier.NONE)

        def _map_button(self, button):
            QtCore, _, _, _ = _qt()
            if self.interactor is None:
                return None
            mapping = {QtCore.Qt.MouseButton.LeftButton: self.interactor.MouseButton.LEFT,
                       QtCore.Qt.MouseButton.RightButton: self.interactor.MouseButton.RIGHT,
                       QtCore.Qt.MouseButton.MiddleButton: self.interactor.MouseButton.MIDDLE}
            return mapping.get(button)

        def mousePressEvent(self, event):
            if self.interactor is None:
                return super().mousePressEvent(event)
            self.makeCurrent()
            self._update_modifiers(event.modifiers())
            button = self._map_button(event.button())
            if button is not None:
                position = event.position()
                self.interactor.trigger_mouse_position(position.x(), position.y())
                self.interactor.trigger_mouse_button(
                    self.interactor.InputAction.PRESS, button)
            self.doneCurrent()
            self._dirty = True
            self.update()

        def mouseMoveEvent(self, event):
            if self.interactor is None:
                return super().mouseMoveEvent(event)
            self.makeCurrent()
            self._update_modifiers(event.modifiers())
            position = event.position()
            self.interactor.trigger_mouse_position(position.x(), position.y())
            self.doneCurrent()
            self._dirty = True
            self.update()

        def mouseReleaseEvent(self, event):
            if self.interactor is None:
                return super().mouseReleaseEvent(event)
            self.makeCurrent()
            self._update_modifiers(event.modifiers())
            button = self._map_button(event.button())
            if button is not None:
                self.interactor.trigger_mouse_button(
                    self.interactor.InputAction.RELEASE, button)
                position = event.position()
                self.interactor.trigger_mouse_position(position.x(), position.y())
            self.doneCurrent()
            self._dirty = True
            self.update()
            self.camera_changed.emit(self.camera_state())

        def wheelEvent(self, event):
            if self.interactor is None:
                return super().wheelEvent(event)
            self.makeCurrent()
            self._update_modifiers(event.modifiers())
            delta = event.angleDelta()
            direction = (self.interactor.WheelDirection.FORWARD if delta.y() > 0
                         else self.interactor.WheelDirection.BACKWARD)
            self.interactor.trigger_mouse_wheel(direction)
            self.doneCurrent()
            self._dirty = True
            self.update()

        def keyPressEvent(self, event):
            if self.interactor is None:
                return super().keyPressEvent(event)
            self.makeCurrent()
            self._update_modifiers(event.modifiers())
            key = event.key()
            QtCore, _, _, _ = _qt()
            text = None
            if QtCore.Qt.Key.Key_A.value <= key <= QtCore.Qt.Key.Key_Z.value:
                text = chr(key).upper()
            else:
                text = {QtCore.Qt.Key.Key_Space.value: 'Space',
                        QtCore.Qt.Key.Key_Return.value: 'Return',
                        QtCore.Qt.Key.Key_Escape.value: 'Escape'}.get(key)
            if text:
                self.interactor.trigger_keyboard_key(self.interactor.InputAction.PRESS, text)
            self.doneCurrent()
            self._dirty = True
            self.update()

        def keyReleaseEvent(self, event):
            if self.interactor is None:
                return super().keyReleaseEvent(event)
            self.makeCurrent()
            QtCore, _, _, _ = _qt()
            key = event.key()
            text = None
            if QtCore.Qt.Key.Key_A.value <= key <= QtCore.Qt.Key.Key_Z.value:
                text = chr(key).upper()
            else:
                text = {QtCore.Qt.Key.Key_Space.value: 'Space',
                        QtCore.Qt.Key.Key_Return.value: 'Return',
                        QtCore.Qt.Key.Key_Escape.value: 'Escape'}.get(key)
            if text:
                self.interactor.trigger_keyboard_key(
                    self.interactor.InputAction.RELEASE, text)
            self.doneCurrent()
            return super().keyReleaseEvent(event)

        # -- introspection for the host and the tests ------------------
        @property
        def diagnostics(self) -> dict:
            return {'initialized': self._initialized, 'frames': self._frames,
                    'mode': self._mode, 'applied': sorted(self._applied),
                    'missing_gl_symbols': list(self._missing_symbols)[:16],
                    'dpr': float(self.devicePixelRatioF() or 1.0),
                    'has_engine': self.engine is not None}

    return F3DViewport


_VIEWPORT_CLASS = None


def create_f3d_widget(parent=None, prepared_bundle: Optional[Mapping[str, Any]] = None,
                      parameter_snapshot: Optional[Mapping[str, Any]] = None,
                      *, mode: str = MODE_MATERIAL,
                      camera: Optional[Mapping[str, Any]] = None):
    """Factory the preview process uses to install the native viewport.

    Allowed in the interactive helper process only. The inputs are the prepared
    bundle (``package_root``/``main_relative_path``/``manifest_path``/``lease_id``/
    ``source_revision``), a detached parameter snapshot and an optional camera —
    never a live scene, a database connection or a Material object.
    """
    global _VIEWPORT_CLASS
    if _VIEWPORT_CLASS is None:
        _VIEWPORT_CLASS = build_viewport_class()
    widget = _VIEWPORT_CLASS(parent)
    request = request_from_bundle(prepared_bundle, parameter_snapshot, mode=mode,
                                 camera=camera)
    if request:
        widget.request_load(request)
    return widget


def request_from_bundle(prepared_bundle: Optional[Mapping[str, Any]],
                        parameter_snapshot: Optional[Mapping[str, Any]] = None,
                        *, mode: str = MODE_MATERIAL,
                        camera: Optional[Mapping[str, Any]] = None) -> dict:
    """Turn a prepared bundle into the load request used by the viewport."""
    if not prepared_bundle:
        return {}
    root = str(prepared_bundle.get('package_root') or '')
    relative = str(prepared_bundle.get('main_relative_path') or '')
    if not root or not relative:
        raise ValueError('准备好的包缺少 package_root 或 main_relative_path')
    path = Path(root) / relative
    request = {'model_path': str(path), 'mode': str(mode or MODE_MATERIAL),
               'source_revision': int(prepared_bundle.get('source_revision') or 0),
               'lease_id': str(prepared_bundle.get('lease_id') or ''),
               'manifest_path': str(prepared_bundle.get('manifest_path') or ''),
               'parameter_snapshot': dict(parameter_snapshot or {})}
    if camera:
        request['camera'] = dict(camera)
    return request


# ── host process ──────────────────────────────────────────────────────────
class PreviewShellFallback:
    """Minimal shell used when Prism's window shell is unavailable.

    It exposes the same surface as
    :class:`prism.widgets.model_preview.ModelPreviewWindow` so the host wiring
    is identical in both cases: ``set_capabilities``, ``install_viewport`` and
    the four request signals.
    """

    def __init__(self):
        QtCore, _, QtWidgets, _ = _qt()
        self.window = QtWidgets.QDialog()
        self.window.setWindowTitle('模型预览')
        self.window.resize(1000, 780)
        layout = QtWidgets.QVBoxLayout(self.window)
        toolbar = QtWidgets.QHBoxLayout()
        self.mode = QtWidgets.QComboBox()
        for label, value in (('完整材质', MODE_MATERIAL),
                             ('材质＋边线', MODE_MATERIAL_EDGES),
                             ('纯线框', MODE_WIREFRAME)):
            self.mode.addItem(label, value)
        self.reset_button = QtWidgets.QPushButton('回到正面')
        self.cover_button = QtWidgets.QPushButton('设为封面')
        for control in (self.mode, self.reset_button, self.cover_button):
            control.setEnabled(False)
            toolbar.addWidget(control)
        toolbar.addStretch()
        self.close_button = QtWidgets.QPushButton('返回模型库')
        self.close_button.clicked.connect(self.window.close)
        toolbar.addWidget(self.close_button)
        layout.addLayout(toolbar)
        self.viewport_host = QtWidgets.QStackedWidget()
        self.placeholder = QtWidgets.QLabel('等待原生后端')
        self.placeholder.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.viewport_host.addWidget(self.placeholder)
        layout.addWidget(self.viewport_host, 1)
        self.status = QtWidgets.QLabel('等待模型后端')
        layout.addWidget(self.status)
        self.view_mode_requested = _Signal()
        self.reset_requested = _Signal()
        self.cover_requested = _Signal()
        self.close_requested = _Signal()
        self.mode.currentIndexChanged.connect(
            lambda index: self.view_mode_requested.emit(self.mode.itemData(index)))
        self.reset_button.clicked.connect(self.reset_requested.emit)
        self.cover_button.clicked.connect(self.cover_requested.emit)
        self.window.destroyed.connect(lambda *_: self.close_requested.emit())

    def set_capabilities(self, capabilities) -> None:
        preview = bool(getattr(capabilities, 'preview', False))
        wireframe = bool(getattr(capabilities, 'wireframe', False))
        for control in (self.mode, self.reset_button, self.cover_button):
            control.setEnabled(preview)
        for index in range(self.mode.count()):
            item = self.mode.model().item(index)
            if item is not None and self.mode.itemData(index) == MODE_WIREFRAME:
                item.setEnabled(wireframe)
        self.status.setText('预览已就绪' if preview else 'F3D 预览后端尚未接入')

    def install_viewport(self, widget):
        self.viewport_host.addWidget(widget)
        self.viewport_host.setCurrentWidget(widget)
        self.placeholder.hide()
        return widget

    def set_status(self, text: str) -> None:
        self.status.setText(str(text))

    def show(self) -> None:
        self.window.show()

    def close(self) -> None:
        self.window.close()


class _Signal:
    """Tiny stand-in for a Qt signal inside the fallback shell."""

    def __init__(self):
        self._slots: list = []

    def connect(self, slot) -> None:
        self._slots.append(slot)

    def emit(self, *args) -> None:
        for slot in list(self._slots):
            slot(*args)


def build_shell(prefer_prism: bool = True):
    """Prism's window shell when it is importable, otherwise the fallback."""
    if prefer_prism:
        try:
            from prism.widgets.model_preview import ModelPreviewWindow
            return ModelPreviewWindow(), True
        except Exception:  # noqa: BLE001 - the fallback keeps the host working
            pass
    return PreviewShellFallback(), False


class PreviewHost:
    """IPC client for the interactive process. Drives one window at a time."""

    def __init__(self, port: int, token: str, cache_root: Path, *,
                 shell_preference: bool = True, poll_ms: int = 30):
        self.channel = wire.Channel.connect('127.0.0.1', int(port), timeout=30.0)
        self.cache_root = Path(cache_root)
        self.cache_root.mkdir(parents=True, exist_ok=True)
        import tempfile
        self._channel_cache = tempfile.TemporaryDirectory(prefix='model-channels-', dir=self.cache_root)
        self._token = token
        self._handshake_cache: Optional[wire.Handshake] = None
        QtCore, _, QtWidgets, _ = _qt()
        self._app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
        from prism.widgets.preview_theme import apply_preview_theme
        apply_preview_theme(self._app)
        self._shell, self._prism_shell = build_shell(shell_preference)
        self._viewport = None
        self._wireframe = False
        self._last_request: dict = {}
        self._pending_open = None
        self._timer = QtCore.QTimer()
        self._timer.setInterval(max(5, int(poll_ms)))
        self._timer.timeout.connect(self._poll)
        self._closed = False

    # -- lifecycle -----------------------------------------------------
    def start(self) -> None:
        handshake = self._handshake()
        self._handshake_cache = handshake
        self._wireframe = bool(handshake.wireframe)
        self.channel.send(wire.message(wire.HELLO, token=self._token,
                                       handshake=handshake.to_payload()))
        self._timer.start()

    def run(self) -> int:
        self.start()
        return int(self._app.exec())

    def _handshake(self) -> wire.Handshake:
        import f3d
        info = f3d.Engine.get_lib_info()
        formats = {}
        for reader in f3d.Engine.get_readers_info():
            for extension in reader.extensions:
                ext = '.' + str(extension).lstrip('.').lower()
                formats.setdefault(ext, str(reader.name))
        _, environment_revision = _environment()
        return wire.Handshake(
            worker=wire.WORKER_NAME_PREVIEW,
            backend_version=str(info.version_full),
            vtk_version=str(info.vtk_version),
            python_version=platform.python_version(),
            pid=os.getpid(),
            formats={ext: formats.get(ext, '') for ext in wire.SUPPORTED_FORMATS},
            readers=tuple(str(reader.name) for reader in f3d.Engine.get_readers_info()),
            wireframe=False,
            material_edges=True,
            appearance=True,
            statistics='f3d-loaded',
            features={'environment_revision': environment_revision,
                      'shell': 'prism' if self._prism_shell else 'fallback',
                      'channel_diagnostics': 'per-model-availability',
                      'interactive': True},
            limits={'max_frame_bytes': wire.MAX_FRAME_BYTES},
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._timer.stop()
        except Exception:  # noqa: BLE001
            pass
        if self._viewport is not None:
            self._viewport.close_native()
            self._viewport = None
        try:
            self._shell.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.channel.send(wire.message(wire.BYE))
        except Exception:  # noqa: BLE001
            pass
        self.channel.close()
        channel_cache = getattr(self, '_channel_cache', None)
        if channel_cache is not None:
            channel_cache.cleanup()

    # -- IPC -----------------------------------------------------------
    def _poll(self) -> None:
        try:
            envelope = self.channel.recv(timeout=0)
        except wire.ProtocolError as error:
            self._emit_event('failed', error_code=wire.ERR_BAD_PROTOCOL,
                             error_message=str(error))
            self._quit()
            return
        if envelope is None:
            if self.channel.closed:
                self._quit()
            return
        try:
            kind = wire.check_message(envelope)
        except wire.ProtocolError as error:
            self._emit_event('failed', error_code=wire.ERR_BAD_PROTOCOL,
                             error_message=str(error))
            return
        if kind == wire.SHUTDOWN:
            self._quit()
        elif kind == wire.REQUEST:
            self._handle_request(envelope)
        elif kind == wire.CLOSE_PROJECT:
            self._handle_close_project(envelope)

    def _handle_request(self, envelope: Mapping[str, Any]) -> None:
        job_id = str(envelope.get('job_id') or '')
        identity = dict(envelope.get('identity') or {})
        payload = dict(envelope.get('payload') or {})
        action = str(payload.get('action') or envelope.get('kind') or 'preview')
        try:
            if action in ('preview', 'open'):
                self._pending_open = (job_id, identity)
                self._open(payload)
                # A widget existing is not a loaded, rendered model.
                return
            elif action == 'mode':
                outcome = self._set_mode(str(payload.get('mode') or MODE_MATERIAL))
            elif action == 'reset':
                outcome = {'camera': self._viewport.reset_camera() if self._viewport else {}}
            elif action == 'cover':
                outcome = self._capture_cover(payload, identity)
            elif action == 'close':
                self.close()
                outcome = {'closed': True}
            else:
                raise wire.ProtocolError('预览进程不支持的请求：%s' % action)
            self.channel.send(wire.message(wire.RESULT, job_id=job_id,
                                           state=wire.STATE_COMPLETED,
                                           identity=identity, **outcome))
        except Exception as error:  # noqa: BLE001 - reported to the controller
            self._pending_open = None
            code = getattr(error, 'code', wire.ERR_BAD_FILE)
            self.channel.send(wire.message(wire.RESULT, job_id=job_id,
                                           state=wire.STATE_FAILED,
                                           identity=identity, error_code=code,
                                           error_message=str(error)[:600]))

    def _open(self, payload: Mapping[str, Any]) -> dict:
        if self._closed:
            raise wire.ProtocolError('预览窗口已关闭')
        request = dict(payload)
        self._last_request = request
        if request.get('model_name'):
            self._shell.setWindowTitle(str(request['model_name']) + ' · 模型预览')
        if self._viewport is None:
            self._viewport = create_f3d_widget(
                None, request.get('prepared_bundle') or request.get('bundle'),
                request.get('parameter_snapshot'),
                mode=str(request.get('mode') or MODE_MATERIAL),
                camera=request.get('camera'))
            self._viewport.set_wireframe_available(self._wireframe)
            self._viewport.channel_cache_root = Path(self._channel_cache.name)
            self._connect_viewport(self._viewport)
            from prism.model_library.contracts import ModelCapabilities
            capabilities = ModelCapabilities(
                preview=True, wireframe=self._wireframe, edit_appearance=True,
                backend_version=(self._handshake_cache.backend_version
                                 if self._handshake_cache else ''))
            try:
                self._shell.set_capabilities(capabilities)
            except Exception:  # noqa: BLE001 - a shell without capabilities still shows
                pass
            try:
                self._shell.install_viewport(self._viewport)
                self._shell.show()
            except Exception:  # noqa: BLE001
                self._viewport.show()
        else:
            self._viewport.request_load(request_from_bundle(
                request.get('prepared_bundle') or request.get('bundle'),
                request.get('parameter_snapshot'),
                mode=str(request.get('mode') or MODE_MATERIAL),
                camera=request.get('camera')))
        self._present_window(request.get('owner_window_id', 0))
        return {'scene': self._viewport.scene_info() if self._viewport else {},
                'mode': self._viewport.mode if self._viewport else '',
                'ready': self._viewport is not None}

    def _present_window(self, owner_window_id=0):
        QtCore, QtGui, _, _ = _qt()
        if self._closed or self._app.closingDown():
            return
        if owner_window_id and sys.platform == 'win32' and self._app.platformName() == 'windows':
            if getattr(self, '_owner_id', None) != int(owner_window_id):
                from prism.qt_window_owner import bind_window_owner
                self._owner_window = bind_window_owner(self._shell, owner_window_id)
                self._owner_id = int(owner_window_id) if self._owner_window is not None else None
        def present():
            if self._closed or self._app.closingDown():
                return
            if self._shell.isMinimized():
                self._shell.showNormal()
            else:
                self._shell.show()
            self._shell.raise_()
            self._shell.activateWindow()
        present()
        QtCore.QTimer.singleShot(0, present)

    def _set_mode(self, mode: str) -> dict:
        if self._viewport is None:
            raise wire.ProtocolError('预览尚未打开模型')
        ok = self._viewport.set_mode(mode)
        if not ok:
            raise _CapabilityError('当前原生运行时无法绘制该显示模式：%s' % mode)
        return {'mode': self._viewport.mode}

    def _capture_cover(self, payload: Mapping[str, Any],
                       identity: Mapping[str, Any]) -> dict:
        if self._viewport is None:
            raise wire.ProtocolError('预览尚未打开模型')
        if self._viewport.mode not in (MODE_MATERIAL, MODE_MATERIAL_EDGES):
            raise _CapabilityError('通道查看不改材质，请返回完整材质后设为封面')
        destination = str(payload.get('output') or '')
        if not destination:
            model = str(identity.get('model_id') or 'model').replace('/', '_')
            destination = str(self.cache_root / 'scratch'
                              / ('%s-cover.png' % model))
        return {'outputs': self._viewport.capture_cover(destination)}

    def _handle_close_project(self, envelope: Mapping[str, Any]) -> None:
        self._emit_event('closed', project_session=str(envelope.get('project_session') or ''))
        self.close()
        self._quit()

    def _connect_viewport(self, viewport) -> None:
        viewport.model_presented.connect(self._open_presented)
        viewport.mode_changed.connect(
            self._mode_changed)
        viewport.camera_changed.connect(
            lambda state: self._emit_event('camera_changed', camera=state))
        viewport.cover_ready.connect(
            lambda path: self._emit_event('cover_ready', output_path=path))
        viewport.native_error.connect(
            self._open_failed)
        viewport.model_loaded.connect(
            self._model_loaded)
        for name, signal in (('view_mode_requested', 'mode'),
                             ('reset_requested', 'reset'),
                             ('cover_requested', 'cover'),
                             ('close_requested', 'close')):
            handler = getattr(self._shell, name, None)
            if handler is None or not hasattr(handler, 'connect'):
                continue
            handler.connect(getattr(self, '_shell_' + signal))

    # -- shell toolbar → native ---------------------------------------
    def _model_loaded(self, scene):
        from prism.model_library.preview_channels import modes_for, CHANNELS
        if hasattr(self._shell, 'set_available_channels'):
            try:
                reasons = modes_for(self._viewport._source_path, self._viewport._snapshot)
            except Exception as error:
                reasons = {key: str(error) for key in CHANNELS}
            self._shell.set_available_channels(reasons)
            self._mode_changed(self._viewport.mode)
        self._emit_event('native_loaded', scene=scene)

    def _mode_changed(self, mode):
        if hasattr(self._shell, 'show_mode'):
            self._shell.show_mode(mode)
        self._emit_event('mode_changed', mode=mode)

    def _open_presented(self, scene):
        self._present_window(self._last_request.get('owner_window_id', 0))
        pending, self._pending_open = self._pending_open, None
        if pending is not None:
            job_id, identity = pending
            self.channel.send(wire.message(wire.RESULT, job_id=job_id,
                state=wire.STATE_COMPLETED, identity=identity, scene=scene,
                mode=self._viewport.mode, ready=True))

    def _open_failed(self, code, message):
        pending, self._pending_open = self._pending_open, None
        if pending is not None:
            job_id, identity = pending
            self.channel.send(wire.message(wire.RESULT, job_id=job_id,
                state=wire.STATE_FAILED, identity=identity,
                error_code=code, error_message=str(message)[:600]))
        self._emit_event('failed', error_code=code, error_message=message)

    def _shell_mode(self, mode: str) -> None:
        if self._viewport is not None:
            try:
                self._set_mode(str(mode))
                self._shell.set_status(
                    '按文件参数查看通道，缺省值可能为纯色；不修改材质。拖动旋转，滚轮缩放。'
                    if mode not in ('material', 'material_edges') else '预览已就绪。拖动旋转，滚轮缩放。')
                self._emit_event('mode_requested', mode=str(mode))
            except Exception as error:
                self._mode_changed(self._viewport.mode)
                self._shell.set_status(str(error))

    def _shell_reset(self) -> None:
        if self._viewport is not None:
            state = self._viewport.reset_camera()
            self._emit_event('camera_changed', camera=state)

    def _shell_cover(self) -> None:
        self._emit_event('cover_requested')

    def _shell_close(self) -> None:
        self._emit_event('closed')
        self.close()
        self._quit()

    # -- helpers -------------------------------------------------------
    def _emit_event(self, name: str, **fields: Any) -> None:
        try:
            self.channel.send(wire.message(wire.EVENT, event=name, fields=fields))
        except Exception:  # noqa: BLE001
            pass

    def _quit(self) -> None:
        if self._closed:
            self._app.quit()
            return
        self.close()
        self._app.quit()


class _CapabilityError(RuntimeError):
    code = wire.ERR_UNSUPPORTED_CAPABILITY


def run_preview_host(argv: Optional[list] = None, *, shell_preference: bool = True) -> int:
    import argparse

    parser = argparse.ArgumentParser(description='Prism 模型库交互预览进程')
    parser.add_argument('--ipc-port', type=int, required=True)
    parser.add_argument('--ipc-token', required=True)
    parser.add_argument('--cache-root', required=True)
    parser.add_argument('--minimal-shell', action='store_true')
    args = parser.parse_args(argv)
    # Native library first: Qt and F3D cannot be loaded in the other order.
    _ensure_native_first()
    host = PreviewHost(args.ipc_port, args.ipc_token, Path(args.cache_root),
                       shell_preference=not args.minimal_shell)
    return host.run()


def main(argv: Optional[list] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            try:
                stream.reconfigure(encoding='utf-8', errors='replace')
            except Exception:  # noqa: BLE001
                pass
    return run_preview_host(argv)


if __name__ == '__main__':
    sys.exit(main())
