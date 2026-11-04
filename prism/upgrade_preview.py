"""Read-only resource browsing during conversion, never a writable fork."""
from pathlib import Path
import logging
from PyQt6 import QtCore, QtGui, QtWidgets

logger = logging.getLogger(__name__)


def active(view):
    return bool(getattr(view, '_upgrade_preview_readonly', False))


class PreviewGuard(QtCore.QObject):
    """Enable resource browsing while native and web mutation gates stay shut."""
    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self.viewport = view.viewport()
        self.saved = []
        self.actions = []
        self.pan = None
        self.interactive = view.isInteractive()
        view._upgrade_preview_readonly = True
        window = view.window()
        for widget in window.findChildren(QtWidgets.QWidget):
            if widget is view or widget.isAncestorOf(view) or view.isAncestorOf(widget):
                continue
            self.saved.append((widget, widget.isEnabled()))
        for action in window.findChildren(QtGui.QAction):
            self.actions.append((action, action.isEnabled()))
        for widget, _enabled in self.saved:
            widget.setEnabled(False)
        for action, _enabled in self.actions:
            action.setEnabled(False)
        view.setInteractive(False)
        QtWidgets.QApplication.instance().installEventFilter(self)

    def enable_browsing(self):
        if not active(self.view):
            return
        session = getattr(self.view, '_upgrade_preview_session', None)
        if session is not None and session.phase in ('opening_preview', 'opening_converted'):
            return
        window = self.view.window()
        panel = self.view.category_panel
        roots = [panel.resource_tree, window.tabs.tabBar(),
                 getattr(getattr(window, 'canvas_toolbar', None), 'camera_button', None)]
        roots += list(getattr(panel, '_nav_buttons', {}).values())
        roots += [getattr(panel, name, None) for name in ('search', 'search_type', 'search_results')]
        browser = getattr(window, 'asset_browser', None)
        roots += [getattr(browser, name, None) for name in ('list', 'search')]
        current = window.tabs.currentWidget()
        if hasattr(current, 'store_to_scene_async'):
            roots.append(current)
            self._editor_mode(current, True)
        library = getattr(window, 'material_library', None)
        roots += [getattr(library, name, None) for name in ('materials', 'search', 'folders')]
        self.navigation_roots = [root for root in roots if root is not None]
        for root in roots:
            if root is None:
                continue
            # Ancestors provide layout/navigation, siblings stay disabled.
            parent = root
            while parent is not None and parent is not window:
                parent.setEnabled(True)
                parent = parent.parentWidget()
            for child in root.findChildren(QtWidgets.QWidget):
                child.setEnabled(True)
            if isinstance(root, QtWidgets.QAbstractItemView):
                if not any(widget is root for widget, _ in getattr(self, 'drag_modes', [])):
                    if not hasattr(self, 'drag_modes'):
                        self.drag_modes = []
                    self.drag_modes.append((root, root.dragDropMode()))
                root.setDragDropMode(QtWidgets.QAbstractItemView.DragDropMode.NoDragDrop)
        self.view.setInteractive(False)

    def suspend_browsing(self):
        for root in getattr(self, 'navigation_roots', []):
            try:
                root.setEnabled(False)
            except RuntimeError:
                pass

    @staticmethod
    def _editor_mode(panel, readonly):
        try:
            page = getattr(panel, '_page', None)
            if page is not None:
                page.runJavaScript('window.__setPrismPreviewReadonly && '
                    'window.__setPrismPreviewReadonly(' + ('true' if readonly else 'false') + ')')
        except RuntimeError:
            pass  # A removed page cannot receive mode updates.

    def eventFilter(self, watched, event):
        view = self.view
        if not active(view):
            return False
        from PyQt6 import sip
        if sip.isdeleted(view):
            return False
        kind = event.type()
        types = QtCore.QEvent.Type
        window = view.window()
        if watched is not view and watched is not self.viewport:
            if not isinstance(watched, QtWidgets.QWidget) or not window.isAncestorOf(watched):
                return False
            details = getattr(getattr(window, 'material_library', None), 'details', None)
            if details is not None and (watched is details or details.isAncestorOf(watched)):
                if kind in (types.MouseButtonPress, types.MouseButtonRelease, types.MouseButtonDblClick,
                            types.KeyPress, types.KeyRelease, types.InputMethod, types.Wheel):
                    return True
            # Navigation is allowed, mutation gestures are not. Native editor
            # bridges and browser JS also reject writes independently.
            if kind in (types.ContextMenu, types.DragEnter, types.DragMove, types.Drop):
                return True
            if kind == types.MouseButtonDblClick and watched is not view.category_panel.resource_tree.viewport():
                # Material pop-out previews have independent editing controls.
                # Keep inspection in the disabled right-hand detail surface.
                library = getattr(window, 'material_library', None)
                cards = getattr(library, 'materials', None)
                if cards is not None and (watched is cards or cards.isAncestorOf(watched)):
                    return True
            if kind == types.KeyPress and event.key() in (
                    QtCore.Qt.Key.Key_Delete, QtCore.Qt.Key.Key_Backspace, QtCore.Qt.Key.Key_F2):
                return not isinstance(watched, QtWidgets.QLineEdit)
            tree = view.category_panel.resource_tree
            if watched is tree.viewport() and kind == types.MouseButtonPress:
                index = tree.indexAt(event.position().toPoint())
                if index.isValid():
                    rect = tree.itemDelegate().add_button_rect(tree.visualRect(index), index)
                    if rect is not None and rect.contains(event.position().toPoint()):
                        return True
            return False
        if kind == types.ShortcutOverride:
            event.accept()
            return True
        if kind in (types.KeyPress, types.KeyRelease, types.ContextMenu,
                    types.MouseButtonDblClick, types.DragEnter, types.DragMove, types.Drop):
            return True
        if kind == types.MouseButtonPress:
            self.pan = event.position().toPoint()
            return True
        if kind == types.MouseMove:
            if self.pan is not None:
                point = event.position().toPoint()
                delta = point - self.pan
                self.pan = point
                view.horizontalScrollBar().setValue(view.horizontalScrollBar().value() - delta.x())
                view.verticalScrollBar().setValue(view.verticalScrollBar().value() - delta.y())
            return True
        if kind == types.MouseButtonRelease:
            self.pan = None
            return True
        # Wheel zoom and paint are the existing canvas implementation.
        return False

    def release(self):
        if not active(self.view):
            return
        self.view._upgrade_preview_readonly = False
        QtWidgets.QApplication.instance().removeEventFilter(self)
        self.view.setInteractive(self.interactive)
        for widget, mode in getattr(self, 'drag_modes', []):
            try:
                widget.setDragDropMode(mode)
            except RuntimeError:
                pass
        for widget, enabled in self.saved + self.actions:
            try:
                widget.setEnabled(enabled)
            except RuntimeError:
                pass  # An old project editor may have been legitimately deleted.
        self.saved.clear()
        self.actions.clear()
        window = self.view.window()
        for panel in [getattr(window, 'document_panel', None), getattr(window, 'mindmap_panel', None)] + list(
                getattr(window, '_page_panels', {}).values()):
            if panel is None:
                continue
            try:
                self._editor_mode(panel, False)
                if hasattr(panel, '_page_stale'):
                    panel._page_stale = True
                if hasattr(panel, '_sent_tree'):
                    panel._sent_tree = None
                if window.tabs.currentWidget() is panel:
                    panel.load_from_scene()
            except RuntimeError:
                pass


class UpgradePreview(QtCore.QObject):
    def __init__(self, view, source, target, *, readonly=False):
        super().__init__(view)
        self.view, self.source, self.target = view, str(Path(source).resolve()), str(target)
        self.readonly = readonly
        self.guard = PreviewGuard(view)
        self.phase = 'opening_preview'
        self.epoch = getattr(view, '_trace_open_epoch', 0) + 1
        self.project_id = None
        self.camera = None
        self.allow_open = False
        self.expected_target = None
        self.status = QtWidgets.QDialog(view)
        self.status.setWindowTitle('升级旧版工程 · 只读浏览')
        self.status.setWindowModality(QtCore.Qt.WindowModality.NonModal)
        layout = QtWidgets.QVBoxLayout(self.status)
        self.label = QtWidgets.QLabel('正在打开旧工程；转换期间可只读浏览资源。')
        layout.addWidget(self.label)
        self.label.setWordWrap(True)
        self.progress_label = QtWidgets.QLabel('正在准备只读浏览…')
        self.progress_label.setWordWrap(True)
        layout.addWidget(self.progress_label)
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setRange(0, 0)
        layout.addWidget(self.progress_bar)
        self.cancel = QtWidgets.QPushButton('取消转换')
        self.cancel.setEnabled(False)
        layout.addWidget(self.cancel)
        self.retry = QtWidgets.QPushButton('重试转换')
        self.leave = QtWidgets.QPushButton('结束预览')
        self.retry.setEnabled(False)
        self.retry.hide()
        layout.addWidget(self.retry)
        layout.addWidget(self.leave)
        self.retry.clicked.connect(self.start_conversion)
        self.cancel.clicked.connect(self.cancel_conversion)
        self.leave.clicked.connect(self.end_preview)
        self.status.rejected.connect(self.end_preview)
        view.project_open_finished.connect(self.open_finished)
        view._upgrade_preview_session = self

    def start(self):
        self.status.show()
        from prism.paged_open_preparation import source_stamp
        try:
            self.source_stamp = source_stamp(self.source)
        except OSError as error:
            self.phase = 'open_failed'
            self.label.setText('旧工程无法读取；未开始转换：' + str(error))
            return
        self.allow_open = True
        try:
            self.view.open_from_file(self.source, readonly=True, _upgrade_preview=True)
        finally:
            self.allow_open = False
        operation = getattr(self.view, '_opening_operation', None)
        if operation is None or operation.epoch != self.epoch:
            self.label.setText('旧工程未能开始打开；未进行转换。')
            self.phase = 'open_failed'

    def owns(self):
        return (getattr(self.view, '_trace_open_epoch', None) == self.epoch
                and self.view.scene.project_id == self.project_id)

    def open_finished(self, event):
        if event.epoch != self.epoch:
            return
        if getattr(self, 'leaving', False):
            self.end_preview()
            return
        if self.phase == 'opening_preview':
            if event.filename != self.source or event.outcome != 'ready':
                self.label.setText('旧工程未能安全打开；未进行转换。')
                self.phase = 'open_failed'
                return
            self.project_id = event.project_id
            self.phase = 'preview'
            self.guard.enable_browsing()
            self.start_conversion()
        elif self.phase == 'opening_converted':
            if (event.outcome != 'ready' or event.filename != self.expected_target
                    or event.project_id != self.project_id):
                self.phase = 'handoff_failed'
                self.label.setText('新版工程已验证，但打开失败；当前保持只读，请结束预览后重新打开新版。')
                return
            self.phase = 'finished'
            try:
                from prism.upgrade_mapping import remember
                remember(getattr(self.view, 'settings', None), self.source, self.target)
            except Exception:
                # A local reuse hint must never turn a successful conversion
                # into failure or prevent the read-only guard being released.
                logger.exception('Could not remember verified project upgrade')
            if getattr(self, 'resource', None):
                kind, page_id = self.resource
                if kind in ('canvas', 'document', 'mindmap') and page_id:
                    self.view.window().open_workspace_page(kind, page_id)
            if self.camera and self.camera[0] == self.view.current_canvas_id:
                self.view.setTransform(self.camera[1])
                self.view.centerOn(self.camera[2])
            self.finish()

    def start_conversion(self):
        if self.phase not in ('preview', 'conversion_failed') or not self.owns():
            return
        from prism.actions.storage_workflow import run
        from prism.storage.entrypoint import convert
        from prism.paged_open_preparation import require_stamp
        self.phase = 'converting'
        self.retry.setEnabled(False)
        self.retry.hide()
        self.cancel.show()
        self.cancel.setEnabled(True)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.show()
        self.progress_label.setText('正在准备转换…')
        self.leave.setText('取消转换并结束预览')
        self.label.setText('后台转换中：可切换画布、文档、脑图和素材；暂不可编辑或保存。')
        def job(cancel, progress):
            require_stamp(self.source, self.source_stamp)
            result = convert(self.source, self.target, cancel=cancel, progress=progress)
            require_stamp(self.source, self.source_stamp)
            return str(result)
        accepted = run(self.view, '升级并校验工程', job,
            self.converted, with_progress=True, nonmodal=True, failed=self.conversion_failed,
            progress_changed=self.update_progress)
        if not accepted:
            self.conversion_failed('已有工程操作尚未结束，请稍后重试。')

    def update_progress(self, value):
        if self.phase != 'converting' or not self.owns():
            return
        if not self.cancel.isEnabled():
            return  # Do not overwrite the user's pending cancellation state.
        phase = value.get('phase')
        text = {'metadata': '正在复制工程数据…', 'objects': '正在复制并校验原件…',
                'verification': '正在核对全部数据及引用…'}.get(phase, '正在转换工程…')
        if phase == 'objects':
            text += f" 已处理 {value.get('processed', value.get('copied_this_run', 0))} 项"
        self.progress_label.setText(text)
        # Counts have no reliable total; keep indeterminate progress honest.

    def cancel_conversion(self):
        operation = getattr(self.view, '_storage_operation', None)
        if self.phase != 'converting' or operation is None:
            return
        operation.requestInterruption()
        self.cancel.setEnabled(False)
        self.progress_label.setText('正在安全取消转换；完成后可继续只读浏览。')

    def conversion_failed(self, error):
        self.phase = 'conversion_failed'
        self.cancel.hide()
        self.progress_bar.hide()
        self.progress_label.setText('转换已停止，旧工程保持只读。')
        self.retry.show()
        self.label.setText('转换未完成，仍在只读浏览旧工程：\n' + error)
        self.leave.setText('结束预览')
        self.retry.setEnabled(self.owns())
        self.status.show()
        if getattr(self, 'leaving', False):
            self.end_preview()

    def converted(self, path):
        if not self.owns():
            self.finish()
            return
        if getattr(self, 'leaving', False):
            self.end_preview()
            return
        # convert() publishes only after all metadata and original-byte checks.
        self.camera = (self.view.current_canvas_id, QtGui.QTransform(self.view.transform()),
                       self.view.mapToScene(self.view.viewport().rect().center()))
        window = self.view.window()
        self.resource = (getattr(window, 'current_page_kind', None), getattr(window, 'current_page_id', None))
        from prism.storage.managed import database_path
        try:
            from prism.storage.entrypoint import is_entry
            if not is_entry(path):
                raise ValueError('验证后的新版入口不存在或格式无效')
            self.expected_target = str(database_path(path))
        except Exception as error:
            logger.exception('Verified upgrade target unavailable during handoff')
            self.conversion_failed('新版入口无法读取，旧源仍保持只读：' + str(error))
            return
        self.phase = 'opening_converted'
        self.cancel.hide()
        self.progress_label.setText('校验完成，正在打开新版工程…')
        self.guard.suspend_browsing()
        self.epoch += 1
        self.label.setText('转换验证完成，正在切换新版工程…')
        self.allow_open = True
        try:
            self.view.open_from_file(path, readonly=self.readonly)
        finally:
            self.allow_open = False
        if getattr(self.view, '_trace_open_epoch', None) != self.epoch:
            self.epoch = getattr(self.view, '_trace_open_epoch', self.epoch)
            self.phase = 'handoff_failed'
            self.label.setText('新版工程已发布，但切换未开始；请结束预览后打开新版。')

    def end_preview(self):
        if getattr(self, '_finished', False):
            return
        if getattr(self.view, '_trace_open_epoch', None) != self.epoch:
            self.finish()
            return
        operation = getattr(self.view, '_storage_operation', None)
        if operation is not None:
            self.leaving = True
            operation.requestInterruption()
            self.label.setText('正在安全取消转换；旧源保持不变。')
            return
        opening = getattr(self.view, '_opening_operation', None)
        if opening is not None and not opening.terminal:
            self.leaving = True
            self.view._cancel_open(opening)
            return
        self.view.clear_scene()
        self.finish()

    def finish(self):
        if getattr(self, '_finished', False):
            return
        self._finished = True
        try:
            self.view.project_open_finished.disconnect(self.open_finished)
        except (TypeError, RuntimeError):
            pass
        self.guard.release()
        self.view._upgrade_preview_session = None
        self.status.close()
        self.status.deleteLater()
        self.deleteLater()
