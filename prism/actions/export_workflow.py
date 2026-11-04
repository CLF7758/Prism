"""UI export workflows kept outside :mod:`prism.view`.

These are application-facing adapters: they may show dialogs and start a
worker, but they do not reach into the graphics view's implementation.  A
menu action in the view remains a one-line dispatch point.
"""
import itertools
import logging
import os
import copy

from PyQt6 import QtCore, QtWidgets

from prism import fileio, widgets
from prism.export_service import (ExportService, ExportTarget,
                                  OriginalExportUnavailable,
                                  SelectionUnresolved)
from prism.fileio.export import ImagesToDirectoryExporter, exporter_registry
from prism.i18n import _
from prism.utils import get_file_extension_from_format


logger = logging.getLogger(__name__)


#: 这两句还没有进 zh_CN 字典（字典是别人的文件，这一轮不能改），所以先
#: 直接写中文；字典的 owner 可以把它们收进字典、再换成 _()。
INCOMPLETE_SUMMARY = '导出不完整：{count} 个文件没有写出，下面是每个文件的原因。'
CANCELLED_MESSAGE = '导出已取消：已经写出的文件被撤掉了，其余没有导出。'


def selected_export_path(path, chosen_filter, extensions):
    """The format dropdown wins over a pre-filled default file suffix."""
    import re
    matches = re.findall(r'\*\.(\w+)', chosen_filter or '')
    chosen = matches[0].lower() if matches else None
    stem, suffix = os.path.splitext(path)
    old = suffix.lstrip('.').lower()
    if len(matches) > 1 and old in extensions:
        return path
    if chosen in extensions:
        if old in extensions and old != chosen:
            return stem+'.'+chosen
        if not suffix:
            return path+'.'+chosen
        if old not in extensions:
            return path+'.'+chosen
    return path


def export_scene(view):
    """Ask for an output file and export the current scene asynchronously."""
    directory = os.path.dirname(view.filename) if view.filename else None
    filename, formatstr = QtWidgets.QFileDialog.getSaveFileName(
        parent=view,
        caption='导出当前画布',
        directory=directory,
        filter=';;'.join(('Image Files (*.png *.jpg *.jpeg *.svg)',
                          'PNG (*.png)',
                          'JPEG (*.jpg *.jpeg)',
                          'SVG (*.svg)',
                          'PDF (*.pdf)')))
    if not filename:
        return

    filename = selected_export_path(filename, formatstr, ('png', 'jpg', 'jpeg', 'svg', 'pdf'))

    _name, extension = os.path.splitext(filename)
    if not extension:
        extension = get_file_extension_from_format(formatstr)
        filename = f'{filename}.{extension}'
    extension = extension.lower()
    logger.debug('Got export filename %s', filename)

    if extension.lstrip('.') == 'pdf':
        from prism.fileio.pdf_export import export_scene_pdf
        try:
            export_scene_pdf(view.scene, filename)
        except (OSError, ValueError) as error:
            widgets.PrismNotification(view, str(error))
        else:
            widgets.PrismNotification(view, 'PDF 已导出：'+filename)
        return

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
    if _store_open_panel(view) is False:
        widgets.PrismNotification(view, _('Editor content could not be read. Please retry exporting.'))
        return
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
    from prism.fileio.document_export import EXPORT_FORMATS
    target = selected_export_path(target, chosen, tuple(e.extension.lstrip('.') for e in EXPORT_FORMATS))
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
    if _store_open_panel(view) is False:
        widgets.PrismNotification(view, _('Editor content could not be read. Please retry exporting.'))
        return
    tree = panel._scene_tree()
    if not tree:
        widgets.PrismNotification(
            view, _('There is no mind map to export yet.'))
        return
    options = (
        (_('XMind'), 'xmind'), (_('FreeMind'), 'mm'), (_('JSON'), 'json'),
        (_('Markdown'), 'md'), (_('PNG image'), 'png'), ('PDF', 'pdf'))
    pattern = ';;'.join(f'{label} (*.{ext})' for label, ext in options)
    target, chosen = QtWidgets.QFileDialog.getSaveFileName(
        view, _('Export mind map'), suggested_filename(tree, 'xmind'), pattern)
    if not target:
        return
    target = selected_export_path(target, chosen, (*SUPPORTED_FORMATS, 'pdf'))
    extension = os.path.splitext(target)[1].lower().lstrip('.')
    if extension not in (*SUPPORTED_FORMATS, 'pdf'):
        extension = next((ext for _label, ext in options
                          if f'(*.{ext})' in (chosen or '')), 'xmind')
        target = f'{target}.{extension}'
    try:
        if extension == 'pdf':
            from prism.fileio.resource_export import ExportEntry, ExportPlan, write_single_entry
            frozen_tree = copy.deepcopy(tree)
            frozen_attachments = copy.deepcopy(getattr(view.scene, 'attachments', {}) or {})
            def write_pdf(path):
                export_mindmap(frozen_tree, path, 'pdf', frozen_attachments,
                               cancel=lambda: bool(worker.canceled))
            plan = ExportPlan('mindmap', entries=(ExportEntry('mindmap.pdf', writer=write_pdf),))
            worker = view.worker = fileio.ThreadedIO(write_single_entry, plan, target)
            worker.finished.connect(lambda path, errors: _on_resource_export_finished(view, path, errors, plan))
            def release():
                if view.worker is worker:
                    view.worker = None
                worker.func = None
                worker.args = ()
                worker.kwargs = {}
            stopped = QtCore.QThread.finished.__get__(worker, type(worker))
            stopped.connect(release)
            stopped.connect(worker.deleteLater)
            view.progress = widgets.PrismProgressDialog(_('Export mind map'), worker=worker, parent=view)
            worker.start()
            return
        if extension == 'png':
            panel.export_snapshot(target)
        else:
            # The attachment table is snapshotted, like the PDF branch and
            # the resource-tree export do: the export must not read a table
            # that is still being edited while it writes.
            export_mindmap(tree, target, extension,
                           attachments=copy.deepcopy(
                               getattr(view.scene, 'attachments', None) or {}))
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

    按页打开的工程里 scene 只有一页，「导哪些」不再等于「scene 里有什么」：
    已加载的对象 + 目录里那一整份。目录不可用（`CatalogNotReady`）时仍然
    明确拒绝并说明原因 —— 宁可不做，也不能把已加载的那一页说成全项目。
    """
    service = view.export_service
    scene = getattr(view, 'scene', None)
    source = None
    if getattr(scene, '_partial_project_source', None):
        from prism.project_catalog_index import CatalogNotReady

        requested = _browser_selection(view) if selected_only else None
        unloaded = [item for item in (requested or ())
                    if getattr(item, '_catalog_proxy', False)]
        scope = _catalog_scope(view)
        if scope is None:
            widgets.PrismNotification(view, _(
                'This selection contains unloaded assets. They can be '
                'exported once the project directory is ready.') if unloaded
                else _(
                    'Whole-project image export needs the project directory, '
                    'which is not ready yet. Export the loaded images, or '
                    'reopen with full loading.'))
            return
        try:
            items, source = _partial_export_selection(
                view, service, scope, selected_only, requested)
        except SelectionUnresolved as reason:
            widgets.PrismNotification(view, str(reason))
            return
        except CatalogNotReady as reason:
            widgets.PrismNotification(view, _(
                'The project directory stopped being usable, so nothing was '
                'exported: {reason}').format(reason=reason))
            return
    else:
        items = service.items_for(selected_only=selected_only)
    items = [item for item in items
             if getattr(item, 'TYPE', None) == 'pixmap'
             and getattr(item, '_deleted_at', None) is None]
    if not items and source is None:
        widgets.PrismNotification(view, _('No exportable files selected'))
        return
    mode = view._ask_export_mode()
    if mode is None:
        return

    target = (ExportTarget.ADJUSTED if mode == 'adjusted'
              else ExportTarget.ORIGINAL)

    try:
        plan = service.plan(target, items=items, source=source)
    except OriginalExportUnavailable as reason:
        QtWidgets.QMessageBox.warning(view, _('Original files unavailable'),
            str(reason)+'\n原件未能完整取得，原件导出已停止。不会使用预览图或重新编码替代。')
        return

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
    options = dict(plan.options)
    if plan.source is not None:
        # 目录驱动的批量导出：逐个弹窗对十万级没有意义，自动避让即可。
        options.update(source=plan.source, unique_names=True)
    view.exporter = plan.exporter(view.scene, directory, **options)
    if plan.items or plan.source is not None:
        # 计划里带了明确的素材清单（用户可能只选了其中几张）。导出器
        # 自己会从 scene 取全部，"导哪些"这一步得在这里落下去 ——
        # 清单为空也一样：选中的全是未加载素材时，绝不能顺手把 scene 里
        # 那些没选的一页也导出去。
        chosen = set(plan.items)
        view.exporter.items = [item for item in view.exporter.items
                               if item in chosen]
    view.exporter.num_total = len(view.exporter.items) + len(plan.source or ())
    view.worker = fileio.ThreadedIO(view.exporter.export)
    view.worker.user_input_required.connect(view.on_export_images_file_exists)
    view.worker.finished.connect(view.on_export_finished)
    view.progress = widgets.PrismProgressDialog(
        f'Exporting to {directory}', worker=view.worker, parent=view)
    view.worker.start()


# ── 按页打开的工程：取项来自目录，不只是 scene ──────────────────


def _catalog_scope(view):
    """可用且 ready 的只读目录范围；不可用时是 ``None``。

    宁可返回 None 让界面明确拒绝，也不拿一个空的、过期的目录冒充全项目。
    """
    scope = getattr(view, '_open_scope', None)
    if scope is None:
        return None
    try:
        return scope if scope.ready else None
    except Exception:                                 # noqa: BLE001
        logger.exception('Project directory unavailable')
        return None


def _browser_selection(view):
    """资产浏览器当前选中的那些（用户看到的），不在浏览器页签时是 None。

    未加载的素材在浏览器里是 `_catalog_proxy` 目录代理；已加载的是 scene
    里的真对象。用它而不是 scene 的选中集，才谈得上「导的就是用户选的」。
    """
    window = getattr(view, 'window', None)
    window = window() if callable(window) else None
    browser = getattr(window, 'asset_browser', None)
    if browser is None:
        return None
    tabs = getattr(window, 'tabs', None)
    if tabs is not None and tabs.currentWidget() is not browser:
        return None
    items = getattr(getattr(browser, 'model', None), 'items', None)
    selected = getattr(getattr(browser, 'list', None), 'selectedIndexes', None)
    if items is None or not callable(selected):
        return None
    requested, seen = [], set()
    for index in selected():
        try:
            item = items[index.row()]
        except (AttributeError, IndexError, TypeError):
            continue
        if isinstance(item, dict):
            continue            # 文档/脑图的搜索结果，不是图片素材
        if id(item) in seen:
            continue
        seen.add(id(item))
        requested.append(item)
    return requested


def _partial_export_selection(view, service, scope, selected_only, requested):
    """`(已加载素材, 磁盘来源)` —— 按需打开的工程里「导哪些」的答案。

    已加载的对象优先：它们可能带着用户**没保存**的编辑，磁盘上那一行旧记录
    不能盖掉它。剩下的才是目录记录。
    """
    archive = getattr(getattr(view, 'scene', None), '_partial_project_source', None)
    if requested is not None:
        loaded = [item for item in requested
                  if not getattr(item, '_catalog_proxy', False)]
        ids = [int(item.save_id) for item in requested
               if getattr(item, '_catalog_proxy', False)
               and getattr(item, 'save_id', None) is not None]
    elif selected_only:
        # 画布页上「导出选中」：scene 的选中集里只有已加载的那些，绝不能
        # 顺手把整个目录也算进去 —— 那是比少导更糟的错。
        loaded = service.items_for(selected_only=True)
        ids = []
    else:
        loaded = service.items_for(selected_only=False)
        ids = None
    source = service.source_for(scope, ids, loaded=loaded, archive=archive)
    if source is not None and ids is not None:
        missing = source.missing_ids()
        if missing:
            raise SelectionUnresolved(_(
                '{count} of the selected assets are no longer in the project '
                '({ids}), so nothing was exported.').format(
                    count=len(missing),
                    ids=', '.join(str(key) for key in missing[:5])))
    return loaded, source


def _catalog_page_assets(scope, page_id, loaded=(), *, all_media=False):
    """某一页画布上还没加载的素材，按目录记录逐批取。"""
    from prism.export_service import (OnDiskExportSource,
                                      catalog_ids_for_canvas)

    for kind in (('pixmap', 'video', 'glb') if all_media else ('pixmap',)):
        ids = catalog_ids_for_canvas(scope, page_id, kind=kind)
        source = OnDiskExportSource(scope, ids, loaded=loaded,
                                    archive=getattr(scope, 'project_path', None))
        for batch in source.batches():
            for candidate in batch:
                candidate.TYPE = kind
                yield candidate
        if source.unresolved:
            from prism.fileio.resource_export import ResourceExportError
            raise ResourceExportError('目录中有素材已无法解析，未将部分结果当成完整导出。')


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


def ask_original_asset_scope(view, title):
    dialog = QtWidgets.QDialog(view)
    dialog.setWindowTitle(_('Export scope') + ': ' + title)
    dialog.setMinimumWidth(420)
    layout = QtWidgets.QVBoxLayout(dialog)
    choice = QtWidgets.QComboBox()
    choice.addItem(_('Image originals'), 'original_images')
    choice.addItem(_('All asset originals'), 'original_all')
    layout.addWidget(choice)
    explanation = QtWidgets.QLabel()
    explanation.setWordWrap(True)
    layout.addWidget(explanation)
    def update(index):
        explanation.setText(_('Images only; videos and 3D models are excluded.') if index == 0 else
            _('Images, videos and 3D models are included. Original bytes are copied without re-encoding.'))
    choice.currentIndexChanged.connect(update)
    update(0)
    buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok |
                                        QtWidgets.QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
        return None
    return choice.currentData()


def export_resource_subtree(view, section, node_id=None, *, typed=False):
    """导出左侧树里被右键的那一项，以及它下面的所有层级。

    导出的东西按类型定：文档 -> ``.docx``、脑图 -> ``.xmind``、画布 ->
    该画布素材的原文件（图片 / 视频 / 3D 模型，逐字节复制）。文件夹节点
    变成同名的子目录，所以导出的目录形状 = 左侧树的形状。目标形态
    （文件夹 / ``.zip``）在导出前由用户选。

    ``node_id`` 为 None 表示整个分区（右键点在分区根行上）。
    """
    from prism.fileio.resource_export import (
        SECTION_TITLES, ExportCancelled, ResourceExportError,
        ResourceSubtreeExporter, write_directory, write_zip, write_single_entry, write_new_directory)
    from prism.fileio.resource_export_preparation import PageBodyReader
    from prism.project_catalog_index import CatalogNotReady

    scope = None
    if section == 'canvas' and getattr(view.scene, '_partial_project_source', None):
        # 按页打开的工程：画布页里的素材大半还在磁盘上，走目录取，不整份拒绝。
        scope = _catalog_scope(view)
        if scope is None:
            widgets.PrismNotification(view, _(
                'Folder export needs the project directory, which is not '
                'ready yet. Retry once it is built, or reopen with full '
                'loading.'))
            return

    panel = getattr(view, 'category_panel', None)
    service = getattr(panel, 'resource_service', None)
    if service is None:
        return
    if typed and hasattr(view, '_allow_project_replacement') and not view._allow_project_replacement():
        return
    # 文档/脑图面板里刚敲的内容还在编辑器里，先让它回写 scene —— 导出的
    # 必须是用户眼前看到的那一份。
    if _store_open_panel(view) is False:
        widgets.PrismNotification(view, _('Editor content could not be read. Please retry exporting.'))
        return

    node = _resource_export_node(service, node_id)
    title = node['title'] if node is not None else SECTION_TITLES.get(
        section, section)
    identity = _resource_export_identity(view, service, node_id)
    base = os.path.dirname(view.filename) if view.filename else ''
    single_target = None
    document_format = 'docx'
    mindmap_format = 'xmind'
    asset_mode = 'legacy'
    canvas_leaf = False
    if typed:
        asset_mode = 'original_all'
        if node is not None and node.get('nodeType') == 'page' and section in ('document', 'mindmap'):
            formats = ('Word (*.docx);;PDF (*.pdf)' if section == 'document' else
                       'XMind (*.xmind);;' + _('PDF (entire mind map, tiled pages) (*.pdf)'))
            extension = 'docx' if section == 'document' else 'xmind'
            from prism.fileio.resource_export import safe_name
            single_target, chosen = QtWidgets.QFileDialog.getSaveFileName(view, '导出：'+title,
                os.path.join(base, safe_name(title)+'.'+extension), formats)
            if not single_target:
                return
            single_target = selected_export_path(single_target, chosen, ('docx', 'pdf') if section == 'document' else ('xmind', 'pdf'))
            document_format = 'pdf' if section == 'document' and single_target.lower().endswith('.pdf') else 'docx'
            mindmap_format = 'pdf' if section == 'mindmap' and single_target.lower().endswith('.pdf') else 'xmind'
        elif section == 'canvas':
            asset_mode = ask_original_asset_scope(view, title)
            if asset_mode is None:
                return
            canvas_leaf = node is not None and node.get('nodeType') == 'page'
        if identity != _resource_export_identity(view, service, node_id):
            widgets.PrismNotification(view, '工程或资源已变化，请重新选择导出目标。')
            return
    # 正文在哪儿：场景里已经有的直接用，还在工程文件里的（按页打开的
    # 工程）写文件的时候再去读 —— 那次读发生在写盘线程上，不占界面。
    bodies = PageBodyReader(
        pages=getattr(view.scene, 'workspace_pages', []) or [],
        source=_export_body_source(view),
        deferred_page_ids=getattr(view.scene, '_deferred_page_ids', ()) or ())
    exporter = ResourceSubtreeExporter(
        service,
        pages=bodies.pages,
        attachments=copy.deepcopy(getattr(view.scene, 'attachments', {}) or {}),
        canvas_items=lambda page_id: _canvas_assets(view, page_id, scope,
            all_media=typed and asset_mode == 'original_all'),
        bodies=bodies, document_format=document_format, asset_mode=asset_mode,
        mindmap_format=mindmap_format)
    try:
        plan = exporter.build_plan(section, node_id)
    except (ResourceExportError, CatalogNotReady) as exc:
        # 目录不可用就说不可用：半个文件夹不能被当成一次完整导出。
        widgets.PrismNotification(view, str(exc))
        return

    shape = 'single' if single_target else 'folder' if canvas_leaf else ask_export_shape(view, title)
    if shape is None:
        return
    if shape == 'single':
        if len(plan.entries) != 1:
            plan.close()
            widgets.PrismNotification(view, '\n'.join(plan.all_notes) or '目标资源为空，没有导出文件。')
            return
        target, writer = single_target, write_single_entry
    elif shape == 'zip':
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
        if typed and os.path.exists(root):
            plan.close()
            widgets.PrismNotification(view, '导出目标已存在，请选择其它文件夹；不会合并或覆盖已有内容。')
            return
        if (not typed and os.path.isdir(root) and os.listdir(root)
                and not _confirm_existing_folder(view, root)):
            return
        target = directory
        writer = write_new_directory if typed else write_directory

    # 服务标成"要确认"的（比如有素材只能重新编码导出），问一句再走。
    for warning in plan.warnings:
        answer = QtWidgets.QMessageBox.question(
            view, _('Export “{title}”').format(title=title), warning)
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return
    for note in plan.notes:
        logger.debug('导出说明：%s', note)

    if typed and (identity != _resource_export_identity(view, service, node_id)
                  or getattr(view.category_panel, 'resource_service', None) is not service
                  or (hasattr(view, '_allow_project_replacement') and not view._allow_project_replacement())):
        plan.close()
        widgets.PrismNotification(view, '工程或资源已变化，请重新选择导出目标。')
        return
    view.worker = fileio.ThreadedIO(writer, plan, target)
    view.worker.finished.connect(
        lambda path, errors: _on_resource_export_finished(
            view, path, errors, plan))
    if typed and isinstance(view.worker, QtCore.QThread):
        worker = view.worker
        def release_worker():
            if view.worker is worker:
                view.worker = None
            # ThreadedIO.kwargs contains worker=self. Drop the completed
            # plan and self-cycle now, rather than waiting for a gen2 GC.
            worker.args = ()
            worker.kwargs = {}
            worker.func = None
        stopped = QtCore.QThread.finished.__get__(worker, type(worker))
        stopped.connect(release_worker)
        stopped.connect(worker.deleteLater)
    view.progress = widgets.PrismProgressDialog(
        _('Exporting to {directory}').format(directory=target),
        worker=view.worker, parent=view)
    view.worker.start()


def _resource_export_identity(view, service, node_id):
    node = _resource_export_node(service, node_id)
    return (getattr(view.scene, 'project_id', None), getattr(view, '_trace_open_epoch', None),
            view.filename, getattr(view, '_content_revision', None), getattr(view, '_undo_revision', None),
            tuple((key, node.get(key)) for key in ('id', 'section', 'nodeType', 'pageId', 'parentId', 'title', 'deletedAt'))
            if node is not None else None)


def _resource_export_node(service, node_id):
    node = service._by_id(node_id) if node_id is not None else None
    if node is None and node_id == 'implicit:default-canvas':
        return dict(id=node_id, section='canvas', nodeType='page', title='默认画布',
                    pageId='default-canvas', parentId=None)
    return node


def _on_resource_export_finished(view, path, errors, plan):
    """导出线程收尾：先照实报错，再报成功。"""
    bodies = getattr(plan, 'bodies', None)
    cancelled = bool(bodies is not None and getattr(bodies, 'cancelled', False))
    from prism.fileio.resource_export import ExportCancelled
    failures = [str(error) for error in errors
                if not isinstance(error, ExportCancelled)]
    notes = _export_notes(plan)
    if failures:
        view.on_export_finished(path, [
            INCOMPLETE_SUMMARY.format(count=len(failures))] + failures)
    if failures or cancelled:
        extras = []
        if cancelled:
            extras.append(CANCELLED_MESSAGE)
        extras.extend(notes)
        if extras:
            widgets.PrismNotification(view, '\n'.join(extras))
        return
    message = _('Exported {count} file(s) to {path}').format(
        count=plan.file_count, path=path)
    if notes:
        message = '\n'.join([message] + list(notes))
    widgets.PrismNotification(view, message)


def _export_notes(plan):
    """这次导出要告诉用户的话：计划里就知道的，加上写的时候才发现的。"""
    notes = getattr(plan, 'all_notes', None)
    if notes is not None:
        return list(notes)
    return list(getattr(plan, 'notes', ()) or ())


def _export_body_source(view):
    """The project file a page's text can still be read from.

    ``_board_extras_source`` is where the scene's deferred bodies come
    from, and a save moves it to the new file - so it is the first place
    to look.  ``_partial_project_source`` is the project a page-scoped
    open came from, and the view's own file is the last resort.
    """
    scene = getattr(view, 'scene', None)
    for attribute in ('_board_extras_source', '_partial_project_source'):
        candidate = getattr(scene, attribute, None)
        if candidate:
            return str(candidate)
    filename = getattr(view, 'filename', None)
    return str(filename) if filename else None


def _store_open_panel(view):
    """让当前打开的面板把内容回写 scene，供导出读取。"""
    commit = getattr(view.parent, '_store_current_page', None)
    if callable(commit):
        return commit()
    tabs = getattr(view.parent, 'tabs', None)
    panel = tabs.currentWidget() if tabs is not None else None
    store = getattr(panel, 'store_to_scene', None)
    if callable(store):
        return store()
    return True


def _canvas_assets(view, page_id, scope=None, *, all_media=False):
    """某个画布页上的素材：已加载的在前，没加载的按目录记录补在后面。

    归属不明的老素材（没有 canvasId）算默认画布的 —— `_apply_filter`
    也是这么对待它们的（每个画布都显示），导出时至少有一个明确的去处，
    不会凭空漏掉。

    `scope` 非空（按页打开的工程）时，这一页里没加载的素材也从目录取，
    否则文件夹导出只会拿到当前已加载的那几十张。
    """
    items = []
    for item in view.scene.items_for_save():
        if getattr(item, '_deleted_at', None) is not None:
            continue
        canvas = getattr(item, '_canvas_id', None) or 'default-canvas'
        if canvas == page_id:
            items.append(item)
    if scope is None:
        return items
    return itertools.chain(items, _catalog_page_assets(scope, page_id, items, all_media=all_media))


def _confirm_existing_folder(view, root):
    answer = QtWidgets.QMessageBox.question(
        view, _('Folder already exists'),
        _('The folder {path} already exists. Files of the same name will be '
          'replaced. Continue?').format(path=root))
    return answer == QtWidgets.QMessageBox.StandardButton.Yes
