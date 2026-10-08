"""UI export workflows kept outside :mod:`prism.view`.

These are application-facing adapters: they may show dialogs and start a
worker, but they do not reach into the graphics view's implementation.  A
menu action in the view remains a one-line dispatch point.
"""
import logging
import os

from PyQt6 import QtWidgets

from prism import fileio, widgets
from prism.export_service import (ExportService, ExportTarget,
                                  OriginalExportUnavailable)
from prism.fileio.export import ImagesToDirectoryExporter, exporter_registry
from prism.i18n import _
from prism.utils import get_file_extension_from_format


logger = logging.getLogger(__name__)


def export_scene(view):
    """Ask for an output file and export the current scene asynchronously."""
    directory = os.path.dirname(view.filename) if view.filename else None
    filename, formatstr = QtWidgets.QFileDialog.getSaveFileName(
        parent=view,
        caption=_('Export Scene to Image'),
        directory=directory,
        filter=';;'.join(('Image Files (*.png *.jpg *.jpeg *.svg)',
                          'PNG (*.png)',
                          'JPEG (*.jpg *.jpeg)',
                          'SVG (*.svg)')))
    if not filename:
        return

    _name, extension = os.path.splitext(filename)
    if not extension:
        extension = get_file_extension_from_format(formatstr)
        filename = f'{filename}.{extension}'
    extension = extension.lower()
    logger.debug('Got export filename %s', filename)

    exporter = exporter_registry[extension](view.scene)
    if not exporter.get_user_input(view):
        return
    view.worker = fileio.ThreadedIO(exporter.export, filename)
    view.worker.finished.connect(view.on_export_finished)
    view.progress = widgets.PrismProgressDialog(
        f'Exporting {filename}', worker=view.worker, parent=view)
    view.worker.start()


def export_document(view):
    """Export the active document through the registered format writer."""
    from prism.fileio.document_export import (
        DocumentExportError, filter_string, with_extension, writer_for)

    panel = view._current_panel_of('DocumentPanel')
    if panel is None:
        widgets.PrismNotification(view, _('Open a document page first.'))
        return
    panel.store_to_scene()
    html = panel.editor.toHtml()
    if not html.strip():
        widgets.PrismNotification(
            view, _('The document note is empty, nothing to export.'))
        return
    base = os.path.splitext(view.filename or '')[0] or 'note'
    target, chosen = QtWidgets.QFileDialog.getSaveFileName(
        view, _('Export Current Document'), base + '.docx', filter_string())
    if not target:
        return
    entry = writer_for(target, chosen)
    target = with_extension(target, entry)
    try:
        entry.writer(html, getattr(view.scene, 'attachments', {}) or {}, target)
    except DocumentExportError as exc:
        widgets.PrismNotification(view, str(exc))
        return
    logger.info('Exported the note to %s', target)
    widgets.PrismNotification(view, _(
        'Exported the current document to {path}').format(path=target))


def export_mindmap_action(view):
    """Export the active mind map in an exchange or image format."""
    from prism.fileio.mindmap_export import (
        SUPPORTED_FORMATS, MindMapExportError, export_mindmap,
        suggested_filename)

    panel = view._current_panel_of('MindMapPanel')
    if panel is None:
        widgets.PrismNotification(view, _('Open a mind map page first.'))
        return
    panel.store_to_scene()
    tree = panel._scene_tree()
    if not tree:
        widgets.PrismNotification(
            view, _('There is no mind map to export yet.'))
        return
    options = (
        (_('XMind'), 'xmind'), (_('FreeMind'), 'mm'), (_('JSON'), 'json'),
        (_('Markdown'), 'md'), (_('PNG image'), 'png'))
    pattern = ';;'.join(f'{label} (*.{ext})' for label, ext in options)
    target, chosen = QtWidgets.QFileDialog.getSaveFileName(
        view, _('Export mind map'), suggested_filename(tree, 'xmind'), pattern)
    if not target:
        return
    extension = os.path.splitext(target)[1].lower().lstrip('.')
    if extension not in SUPPORTED_FORMATS:
        extension = next((ext for _label, ext in options
                          if f'(*.{ext})' in (chosen or '')), 'xmind')
        target = f'{target}.{extension}'
    try:
        if extension == 'png':
            panel.export_snapshot(target)
        else:
            export_mindmap(tree, target, extension)
    except MindMapExportError as exc:
        widgets.PrismNotification(view, str(exc))
        return
    except (OSError, ValueError) as exc:
        widgets.PrismNotification(view, _(
            'Could not write {path}: {reason}').format(path=target, reason=exc))
        return
    logger.info('Exported mind map to %s', target)
    widgets.PrismNotification(
        view, _('Exported mind map to {path}').format(path=target))


def export_images(view, selected_only=False):
    """Export scene images using the mode already chosen by the UI.

    这个函数只做界面该做的三件事：问用户、照计划执行、显示进度。
    「导哪些、用哪个导出器、会不会重新编码」都问 `ExportService`。
    """
    service = view.export_service
    items = [item for item in service.items_for(selected_only=selected_only)
             if getattr(item, 'TYPE', None) == 'pixmap']
    if not items:
        widgets.PrismNotification(view, _('No exportable files selected'))
        return
    mode = view._ask_export_mode()
    if mode is None:
        return

    target = (ExportTarget.ADJUSTED if mode == 'adjusted'
              else ExportTarget.ORIGINAL)

    try:
        plan = service.plan(target, items=items)
    except OriginalExportUnavailable as reason:
        # 一张有原字节的都没有，做不了真正的原图导出。任务书 T2 要求
        # 这时**提示用户**「只能导当前渲染结果」，而不是悄悄降级。
        answer = QtWidgets.QMessageBox.question(
            view, _('Original files unavailable'),
            _('{reason}\n\nThe original files are not available.\n\n'
              'Export the current rendering instead?\n'
              '(It is re-encoded, not the original.)').format(reason=reason))
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        plan = service.plan(ExportTarget.ADJUSTED, items=items)

    # 服务标成"要确认"的（比如"一半素材没有原字节"），问一句再走。
    for warning in plan.warnings:
        answer = QtWidgets.QMessageBox.question(
            view, _('Original files unavailable'), warning)
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return
    for note in plan.notes:
        logger.debug('导出说明：%s', note)

    directory = os.path.dirname(view.filename) if view.filename else None
    directory = QtWidgets.QFileDialog.getExistingDirectory(
        parent=view, caption=_('Export Images'), directory=directory)
    if not directory:
        return
    logger.debug('Got export directory %s', directory)
    view.exporter = plan.exporter(view.scene, directory, **plan.options)
    if plan.items:
        # 计划里带了明确的素材清单（用户可能只选了其中几张）。导出器
        # 自己会从 scene 取全部，"导哪些"这一步得在这里落下去。
        chosen = set(plan.items)
        view.exporter.items = [item for item in view.exporter.items
                               if item in chosen]
        view.exporter.num_total = len(view.exporter.items)
    view.worker = fileio.ThreadedIO(view.exporter.export)
    view.worker.user_input_required.connect(view.on_export_images_file_exists)
    view.worker.finished.connect(view.on_export_finished)
    view.progress = widgets.PrismProgressDialog(
        f'Exporting to {directory}', worker=view.worker, parent=view)
    view.worker.start()


# ── 导出左侧树的一项（含它下面的全部内容）──────────────────────────


def ask_export_shape(parent, title):
    """问一句：导出到文件夹，还是打包成一个 .zip。

    :returns: ``'folder'`` / ``'zip'`` / ``None``（取消）
    """
    choice = QtWidgets.QMessageBox(parent)
    choice.setWindowTitle(_('Export “{title}”').format(title=title))
    choice.setText(_('The export contains this item and everything '
                     'below it.'))
    folder_button = choice.addButton(
        _('Into a folder'), QtWidgets.QMessageBox.ButtonRole.AcceptRole)
    zip_button = choice.addButton(
        _('As a .zip archive'), QtWidgets.QMessageBox.ButtonRole.AcceptRole)
    choice.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
    choice.exec()
    clicked = choice.clickedButton()
    if clicked is folder_button:
        return 'folder'
    if clicked is zip_button:
        return 'zip'
    return None


def export_resource_subtree(view, section, node_id=None):
    """导出左侧树里被右键的那一项，以及它下面的所有层级。

    导出的东西按类型定：文档 -> ``.docx``、脑图 -> ``.xmind``、画布 ->
    该画布素材的原文件（图片 / 视频 / 3D 模型，逐字节复制）。文件夹节点
    变成同名的子目录，所以导出的目录形状 = 左侧树的形状。目标形态
    （文件夹 / ``.zip``）在导出前由用户选。

    ``node_id`` 为 None 表示整个分区（右键点在分区根行上）。
    """
    from prism.fileio.resource_export import (
        SECTION_TITLES, ResourceExportError, ResourceSubtreeExporter,
        write_directory, write_zip)

    panel = getattr(view, 'category_panel', None)
    service = getattr(panel, 'resource_service', None)
    if service is None:
        return
    # 文档/脑图面板里刚敲的内容还在编辑器里，先让它回写 scene —— 导出的
    # 必须是用户眼前看到的那一份。
    _store_open_panel(view)

    node = service._by_id(node_id) if node_id is not None else None
    title = node['title'] if node is not None else SECTION_TITLES.get(
        section, section)
    exporter = ResourceSubtreeExporter(
        service,
        pages=getattr(view.scene, 'workspace_pages', []) or [],
        attachments=getattr(view.scene, 'attachments', {}) or {},
        canvas_items=lambda page_id: _canvas_assets(view, page_id))
    try:
        plan = exporter.build_plan(section, node_id)
    except ResourceExportError as exc:
        widgets.PrismNotification(view, str(exc))
        return

    shape = ask_export_shape(view, title)
    if shape is None:
        return
    base = os.path.dirname(view.filename) if view.filename else ''
    if shape == 'zip':
        target, _chosen = QtWidgets.QFileDialog.getSaveFileName(
            view, _('Export as .zip'),
            os.path.join(base, plan.root_name + '.zip'), 'Zip (*.zip)')
        if not target:
            return
        if not target.lower().endswith('.zip'):
            target += '.zip'
        writer = write_zip
    else:
        directory = QtWidgets.QFileDialog.getExistingDirectory(
            view, _('Choose where to export “{title}”').format(title=title),
            base)
        if not directory:
            return
        root = os.path.join(directory, plan.root_name)
        # 只在会真的盖掉东西时问一句：空目录直接往里写。
        if (os.path.isdir(root) and os.listdir(root)
                and not _confirm_existing_folder(view, root)):
            return
        target = directory
        writer = write_directory

    # 服务标成"要确认"的（比如有素材只能重新编码导出），问一句再走。
    for warning in plan.warnings:
        answer = QtWidgets.QMessageBox.question(
            view, _('Export “{title}”').format(title=title), warning)
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return
    for note in plan.notes:
        logger.debug('导出说明：%s', note)

    view.worker = fileio.ThreadedIO(writer, plan, target)
    view.worker.finished.connect(
        lambda path, errors: _on_resource_export_finished(
            view, path, errors, plan))
    view.progress = widgets.PrismProgressDialog(
        _('Exporting to {directory}').format(directory=target),
        worker=view.worker, parent=view)
    view.worker.start()


def _on_resource_export_finished(view, path, errors, plan):
    """导出线程收尾：先照实报错，再报成功。"""
    if errors:
        view.on_export_finished(path, errors)
        return
    widgets.PrismNotification(view, _(
        'Exported {count} file(s) to {path}').format(
            count=plan.file_count, path=path))


def _store_open_panel(view):
    """让当前打开的面板把内容回写 scene，供导出读取。"""
    tabs = getattr(view.parent, 'tabs', None)
    panel = tabs.currentWidget() if tabs is not None else None
    store = getattr(panel, 'store_to_scene', None)
    if callable(store):
        store()


def _canvas_assets(view, page_id):
    """某个画布页上的素材，按 scene 里的顺序。

    归属不明的老素材（没有 canvasId）算默认画布的 —— `_apply_filter`
    也是这么对待它们的（每个画布都显示），导出时至少有一个明确的去处，
    不会凭空漏掉。
    """
    items = []
    for item in view.scene.items_for_save():
        canvas = getattr(item, '_canvas_id', None) or 'default-canvas'
        if canvas == page_id:
            items.append(item)
    return items


def _confirm_existing_folder(view, root):
    answer = QtWidgets.QMessageBox.question(
        view, _('Folder already exists'),
        _('The folder {path} already exists. Files of the same name will be '
          'replaced. Continue?').format(path=root))
    return answer == QtWidgets.QMessageBox.StandardButton.Yes
