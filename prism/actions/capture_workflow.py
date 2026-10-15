"""Screen capture and desktop references using the optional upstream helper."""
from PyQt6 import QtCore, QtWidgets
from prism.capture_service import CaptureService
from prism.item_lifecycle import logical_item


def service(view):
    result = getattr(view, '_capture_service', None)
    if result is None:
        result = view._capture_service = CaptureService(view)
        result.imported.connect(lambda target, path: import_capture(view, target, path))
        result.failed.connect(lambda text: QtWidgets.QMessageBox.warning(view, '截图与桌面贴图', text))
    return result


def target(view):
    canvas_id = view.current_canvas_id
    if view._material_canvas() is not None or not canvas_id:
        pages = [('默认画布', 'default-canvas')]
        pages += [(page.get('title', '未命名画布'), page['id'])
                  for page in view.scene.workspace_pages if page.get('kind') == 'canvas'
                  and page.get('id') != 'default-canvas']
        labels = [f'{name} ({index+1})' for index, (name, _) in enumerate(pages)]
        choice, ok = QtWidgets.QInputDialog.getItem(view, '截图导入位置', '选择目标画布', labels, 0, False)
        if not ok:
            return None
        canvas_id = pages[labels.index(choice)][1]
    center = view.mapToScene(view.get_view_center())
    return dict(project_id=view.scene.project_id, canvas_id=canvas_id, x=center.x(), y=center.y())


def capture(view):
    center = view.mapToScene(view.get_view_center())
    selected_target = dict(project_id=view.scene.project_id,
        canvas_id=view.current_canvas_id, x=center.x(), y=center.y(),
        choose_canvas=view._material_canvas() is not None or not view.current_canvas_id)
    try:
        service(view).capture(selected_target)
    except (OSError, ValueError, RuntimeError) as error:
        QtWidgets.QMessageBox.warning(view, '截图与桌面贴图', str(error))


def pin(view):
    canvas = view._material_canvas()
    selected = [canvas.model().items[index.row()] for index in canvas.selectedIndexes()] if canvas else view.scene.selectedItems(user_only=True)
    images = [logical_item(item) for item in selected if getattr(item, 'TYPE', None) == 'pixmap']
    if not images:
        QtWidgets.QMessageBox.information(view, '钉在桌面', '请先选中图片')
        return
    if len(images) > 8:
        QtWidgets.QMessageBox.information(view, '钉在桌面', '一次最多准备 8 张贴图，请减少选择')
        return
    try:
        for item in images:
            service(view).pin(item)
    except (OSError, ValueError, RuntimeError) as error:
        QtWidgets.QMessageBox.warning(view, '钉在桌面', str(error))


def inbox(view):
    from prism.capture_service import inbox_directory
    directory = inbox_directory()
    directory.mkdir(parents=True, exist_ok=True)
    paths, _ = QtWidgets.QFileDialog.getOpenFileNames(view, '截图收件箱', str(directory), '截图 (*.png)')
    if not paths:
        return
    selected_target = target(view)
    if selected_target is None:
        return
    worker = getattr(view, 'worker', None)
    if worker is not None and hasattr(worker, 'isRunning') and worker.isRunning():
        QtWidgets.QMessageBox.information(view, '截图收件箱', '正在导入素材，请稍后再试')
        return
    view.window().open_workspace_page('canvas', selected_target['canvas_id'])
    view.do_insert_images(paths)


def import_capture(view, requested_target, path):
    selected_target = requested_target or target(view)
    if selected_target is None:
        return
    if selected_target['project_id'] != view.scene.project_id:
        QtWidgets.QMessageBox.information(view, '截图已保留', '截图期间已切换工程，图片没有自动导入。文件保留在：\n'+path)
        return
    if selected_target.get('choose_canvas'):
        selected_target = target(view)
        if selected_target is None:
            return
    canvas_id = selected_target['canvas_id']
    if canvas_id != 'default-canvas' and not any(page.get('id') == canvas_id for page in view.scene.workspace_pages):
        QtWidgets.QMessageBox.information(view, '截图已保留', '目标画布已不存在。图片保留在：\n'+path)
        return
    worker = getattr(view, 'worker', None)
    if worker is not None and hasattr(worker, 'isRunning') and worker.isRunning():
        QtWidgets.QMessageBox.information(view, '截图已保留', '正在导入其他素材，请稍后导入截图：\n'+path)
        return
    view.window().open_workspace_page('canvas', canvas_id)
    position = view.mapFromScene(QtCore.QPointF(selected_target['x'], selected_target['y']))
    view.do_insert_images([path], pos=position)
