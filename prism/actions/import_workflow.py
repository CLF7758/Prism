"""Document import UI workflows outside the graphics view."""
import logging
import os
import shutil
import tempfile
import uuid

from PyQt6 import QtCore, QtWidgets

from prism import fileio, widgets
from prism.i18n import _


logger = logging.getLogger(__name__)


def import_document(view):
    """Choose a DOCX file and load it into a document page."""
    from prism.fileio.importers import ImportFileError, ImportReport, docx_to_html

    filename, _selected = QtWidgets.QFileDialog.getOpenFileName(
        parent=view, caption=_('Import Word Document'),
        directory=view._import_directory(),
        filter=_('Word Document (*.docx)'))
    if not filename:
        return
    panel = view._import_target_panel('DocumentPanel', 'document')
    if panel is None:
        return
    report = ImportReport()
    try:
        html = docx_to_html(
            filename, attachment_saver=view._store_imported_attachment,
            report=report)
    except ImportFileError as exc:
        widgets.PrismNotification(view, str(exc))
        return
    panel.editor.setHtml(html)
    panel.store_to_scene()
    panel.load_from_scene()
    view.mark_content_dirty()
    if hasattr(view.parent, 'statusBar'):
        view.parent.statusBar().showMessage(
            _('Imported {name}').format(name=os.path.basename(filename)), 4000)
    logger.info('Imported %s: %s', os.path.basename(filename), report.as_dict())
    summary = report.summary()
    if summary:
        widgets.PrismNotification(view, summary)


# ── 把外面的一棵目录树导进左侧树 ──────────────────────────────
#
# 规则和「拖一个文件夹进画布」是同一套（用户定的）：一层目录一个容器、
# 层级跟着目录走、中间层没有自己的内容就不建空容器、重名自动加序号、
# 不认识的文件跳过而不影响其它。扫描规则在
# :mod:`prism.fileio.resource_import` 里，这里只负责问、看、落地。

#: 节点多于这个数才值得开进度条 —— 一两个页面的导入不该闪一下窗口。
PROGRESS_AFTER = 4


class ImportOutcome:
    """落地之后的样子：归属、收尾和报告都从它读。"""

    def __init__(self, section):
        self.section = section
        #: 建出来的节点，按创建顺序
        self.nodes = []
        #: relative_path -> **文件夹**节点的 id。
        #: 子节点只能挂在文件夹上（画布页是叶子），所以一个目录同时
        #: 产生「同名文件夹 + 画布页」时，这里存的必须是文件夹那一个。
        self.folder_ids = {}
        #: 新建的页面 id，按创建顺序
        self.page_ids = []
        #: 素材文件（归一化路径）-> 它该去的画布页 id
        self.asset_targets = {}
        self.errors = []
        self.canceled = False

    @property
    def folders(self):
        return sum(1 for node in self.nodes
                   if node['nodeType'] == 'folder')

    @property
    def pages(self):
        return len(self.page_ids)

    @property
    def first_page_id(self):
        return self.page_ids[0] if self.page_ids else None


def ask_import_source(parent, title):
    """问一句：从文件夹、压缩包，还是从一批文件导入。

    :returns: ``'folder'`` / ``'archive'`` / ``'files'`` / ``None``（取消）
    """
    box = QtWidgets.QMessageBox(parent)
    box.setWindowTitle(_('Import into “{title}”').format(title=title))
    box.setText(_('What you pick is rebuilt under this item.'))
    folder_button = box.addButton(
        _('From a folder'), QtWidgets.QMessageBox.ButtonRole.AcceptRole)
    archive_button = box.addButton(
        _('From a .zip archive'), QtWidgets.QMessageBox.ButtonRole.AcceptRole)
    files_button = box.addButton(
        _('From files'), QtWidgets.QMessageBox.ButtonRole.AcceptRole)
    box.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
    box.exec()
    clicked = box.clickedButton()
    if clicked is folder_button:
        return 'folder'
    if clicked is archive_button:
        return 'archive'
    if clicked is files_button:
        return 'files'
    return None


def import_resource_subtree(view, section, node_id=None):
    """把外面的一个目录 / ``.zip`` / 一批文件导进左侧树。

    文档 → ``.docx`` 各成一页、脑图 → ``.xmind`` 各成一页、画布 → 目录
    里的图片视频归到该目录的画布页；文件夹节点撑住目录的层级。重名自动
    加序号，不认识的文件跳过。``node_id`` 为 None 表示整个分区。
    """
    from prism.fileio.resource_import import (
        SECTION_TITLES, ResourceImportError, ResourceTreeScanner,
        extract_archive)

    panel = getattr(view, 'category_panel', None)
    service = getattr(panel, 'resource_service', None)
    if service is None:
        return
    node = service._by_id(node_id) if node_id is not None else None
    if node_id is not None and (node is None
                                or node.get('deletedAt') is not None):
        widgets.PrismNotification(view, _('This item no longer exists.'))
        return
    title = node['title'] if node is not None else SECTION_TITLES.get(
        section, section)

    shape = ask_import_source(view, title)
    if shape is None:
        return
    base = os.path.dirname(view.filename) if view.filename else ''

    if shape == 'files':
        _import_loose_files(view, service, section, node_id, base)
        return

    if shape == 'archive':
        archive, _chosen = QtWidgets.QFileDialog.getOpenFileName(
            view, _('Import a .zip archive'), base, 'Zip (*.zip)')
        if not archive:
            return
        # 临时目录用 mkdtemp 显式管理：`TemporaryDirectory` 一没人引用就
        # 把目录删掉，而素材是另一条线程在读的 —— 会删得比读完早。
        # 清理放在导入收尾那一步（见 _drop_staging）。
        staging = tempfile.mkdtemp(prefix='prism-import-')
        try:
            directory = extract_archive(archive, staging)
        except ResourceImportError as exc:
            _drop_staging(staging)
            widgets.PrismNotification(view, str(exc))
            return
        root_title = os.path.splitext(os.path.basename(archive))[0]
        if _stripped_a_top_folder(directory, staging):
            # 导出的 zip 里那层「根名/」被剥掉了：名字沿用那一层，
            # 导出的东西再导入回来还是原来的名字。
            root_title = os.path.basename(directory)
        _scan_and_apply(view, service, section, node_id, directory,
                        root_title=root_title, staging=staging)
        return

    directory = QtWidgets.QFileDialog.getExistingDirectory(
        view, _('Choose a folder to import'), base)
    if not directory:
        return
    _scan_and_apply(view, service, section, node_id, directory)


def _scan_and_apply(view, service, section, node_id, directory,
                    root_title=None, staging=None):
    """扫一遍 → 给用户看一眼 → 落地。"""
    from prism.fileio.resource_import import (
        ResourceImportError, ResourceTreeScanner)

    try:
        plan = ResourceTreeScanner(section).scan(directory, root_title)
    except ResourceImportError as exc:
        _drop_staging(staging)
        widgets.PrismNotification(view, str(exc))
        return

    from prism.widgets.import_preview import ImportPreviewDialog

    target = service._by_id(node_id)['title'] if node_id else \
        _section_title(section)
    dialog = ImportPreviewDialog(plan, target, parent=view)
    if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
        _drop_staging(staging)
        return

    outcome = _apply_plan(view, service, plan, node_id)
    _import_assets(view, plan, outcome, staging)


def _section_title(section):
    from prism.fileio.resource_import import SECTION_TITLES

    return SECTION_TITLES.get(section, section)


def _stripped_a_top_folder(directory, staging):
    """解压时剥掉了顶层目录吗（导出写的正是那一层）。"""
    return os.path.dirname(os.path.normpath(str(directory))) == \
        os.path.normpath(str(staging))


def _drop_staging(staging):
    """解压出来的临时目录：整条导入链路走完了才删。

    ``TemporaryDirectory`` 不能用 —— 对象一旦没人引用目录就没了，而
    素材是另一条线程异步读的。
    """
    if staging:
        shutil.rmtree(str(staging), ignore_errors=True)


def _import_loose_files(view, service, section, node_id, base):
    """选一批散装文件：文档/脑图各建一页；画布素材归当前画布。"""
    if section == 'canvas':
        filenames, _chosen = QtWidgets.QFileDialog.getOpenFileNames(
            view, _('Import files'), base,
            _('Images, videos and models'))
        if filenames:
            # 和拖单个文件进来是同一条路：不建画布，素材归当前画布。
            view.do_insert_images(filenames)
        return

    from prism.fileio.resource_import import (
        ResourceImportError, scan_files)

    pattern = (_('Word Document (*.docx)') if section == 'document'
               else _('XMind Mind Map (*.xmind)'))
    filenames, _chosen = QtWidgets.QFileDialog.getOpenFileNames(
        view, _('Import files'), base, pattern)
    if not filenames:
        return
    try:
        plan = scan_files(section, filenames)
    except ResourceImportError as exc:
        widgets.PrismNotification(view, str(exc))
        return

    from prism.widgets.import_preview import ImportPreviewDialog

    node = service._by_id(node_id) if node_id else None
    target = node['title'] if node is not None else _section_title(section)
    dialog = ImportPreviewDialog(plan, target, parent=view)
    if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
        return
    outcome = _apply_plan(view, service, plan, node_id)
    _import_assets(view, plan, outcome, None)


def _apply_plan(view, service, plan, parent_id):
    """建节点、灌内容。取消时保留已经建好的部分。"""
    workspace_service = getattr(getattr(view, 'parent', None),
                               'workspace_service', None)
    outcome = ImportOutcome(plan.section)
    progress = None
    if len(plan.nodes) > PROGRESS_AFTER:
        progress = QtWidgets.QProgressDialog(
            _('Importing…'), _('Cancel'), 0, len(plan.nodes), parent=view)
        progress.setWindowModality(QtCore.Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
    try:
        for index, entry in enumerate(plan.nodes):
            if progress is not None and progress.wasCanceled():
                outcome.canceled = True
                break
            _create_node(view, service, workspace_service, plan, entry,
                         parent_id, outcome)
            if progress is not None:
                progress.setValue(index + 1)
                QtWidgets.QApplication.processEvents()
    finally:
        if progress is not None:
            progress.close()
            progress.deleteLater()
    return outcome


def _create_node(view, service, workspace_service, plan, entry, parent_id,
                 outcome):
    """按计划建一个节点（文件夹或页面），并把它登记到页面表。"""
    try:
        target_parent = parent_id
        if entry.parent_path is not None:
            # 只认文件夹：一个目录同时产生「同名文件夹 + 画布页」时，
            # 子节点要挂在文件夹上 —— 挂在画布页上是被服务层拒绝的。
            looked_up = outcome.folder_ids.get(entry.parent_path)
            if looked_up is None:
                # 父节点没建成（比如它那层失败了）：这一项挂到导入目标下，
                # 不整块丢掉 —— 报告里会说。
                logger.warning('Missing parent for %s', entry.relative_path)
            else:
                target_parent = looked_up
        if entry.node_type == 'folder':
            name = service.unique_suggestion(plan.section, target_parent,
                                             'folder', entry.title)
            node = service.createFolder(plan.section, name, target_parent)
            outcome.folder_ids[entry.relative_path] = node['id']
            outcome.nodes.append(node)
            return
        page_id = uuid.uuid4().hex
        name = service.unique_suggestion(plan.section, target_parent, 'page',
                                         entry.title)
        node = service.createPage(plan.section, name, page_id, target_parent)
        outcome.nodes.append(node)
        outcome.page_ids.append(page_id)
        for path in entry.assets:
            outcome.asset_targets[os.path.normpath(path)] = page_id
        _fill_page_content(view, plan, entry, workspace_service, name, page_id,
                           outcome)
    except ValueError as exc:
        logger.warning('Could not import %s: %s', entry.relative_path, exc)
        outcome.errors.append(f'{entry.title}: {exc}')


def _fill_page_content(view, plan, entry, workspace_service, name, page_id,
                       outcome):
    """把页面登记进旧的页面表，再把文件内容灌进去。"""
    page = None
    if workspace_service is not None:
        try:
            page = workspace_service.add(plan.section, name, page_id=page_id)
        except ValueError as exc:
            outcome.errors.append(f'{name}: {exc}')
    if page is None or not entry.source_file:
        return
    from prism.fileio.importers import ImportFileError

    path = str(entry.source_file)
    name = os.path.basename(path)
    kind = 'XMind' if plan.section == 'mindmap' else 'Word'
    if not os.path.isfile(path):
        # 解压出来的文件不在了 —— 这不是"内容有问题"，要说清楚。
        logger.warning('The source file for %s is gone', path)
        outcome.errors.append(
            _('{name}: the file was gone before it could be read.').format(
                name=name))
        return
    try:
        if plan.section == 'document':
            from prism.fileio.importers import docx_to_html

            page['content'] = docx_to_html(
                path, attachment_saver=view._store_imported_attachment)
        else:
            from prism.fileio.importers import xmind_to_tree

            page['content'] = xmind_to_tree(path)
    except ImportFileError as exc:
        outcome.errors.append(_read_failure(name, kind, exc))
    except Exception as exc:                        # noqa: BLE001
        logger.exception('Could not read %s', path)
        outcome.errors.append(_read_failure(name, kind, exc))


def _read_failure(name, kind, exc):
    """读不出来时给用户的一句话 —— 带原因，但不带临时路径。

    原始异常（python-docx / zipfile 的原文）里有解压临时目录的路径，
    摆给用户看只会让人以为文件丢了。细节进日志。
    """
    text = str(exc).lower()
    if 'package not found' in text or 'not a zip' in text or \
            'badzip' in text:
        return _('{name} is not a valid {kind} file.').format(
            name=name, kind=kind)
    return _('Could not read {name}.').format(name=name)


def _import_assets(view, plan, outcome, staging):
    """素材走现有那条加载链路；读完再收尾。"""
    files = list(plan.asset_files)
    if not files:
        _finish_import(view, plan, outcome)
        _drop_staging(staging)
        return
    before = {id(item) for item in view.scene.items_for_save()}
    pos = view.get_view_center()
    view.worker = fileio.ThreadedIO(fileio.load_media, files,
                                    view.mapToScene(pos), view.scene)
    view.worker.progress.connect(view.on_items_loaded)
    view.worker.finished.connect(
        lambda _name, errors: _on_assets_loaded(
            view, plan, outcome, before, list(errors), staging))
    view.progress = widgets.PrismProgressDialog(
        _('Importing assets'), worker=view.worker, parent=view)
    view.worker.start()


def _on_assets_loaded(view, plan, outcome, before_ids, errors, staging):
    outcome.errors.extend(errors)
    _assign_new_assets(view, before_ids, outcome.asset_targets)
    _finish_import(view, plan, outcome)
    _drop_staging(staging)


def _assign_new_assets(view, before_ids, asset_targets):
    """把这次新导入的素材归到它们各自的画布页上。

    走 ``ChangeMetadata``：归属变化要能 Ctrl+Z 退回去 —— 和「把素材拖到
    左栏的画布行」「拖一个文件夹建画布」是同一条路。
    """
    if not asset_targets:
        return
    from prism import commands

    # 目标画布页必须真的建出来了：导入中途失败时指过去，素材会在筛选里
    # 直接消失（归属指向一个不存在的画布）。
    known = {page.get('id') for page in
             getattr(view.scene, 'workspace_pages', []) or []}
    groups = {}
    for item in view.scene.items_for_save():
        if id(item) in before_ids:
            continue
        path = os.path.normpath(str(getattr(item, 'filename', '') or ''))
        page_id = asset_targets.get(path)
        if page_id and page_id in known:
            groups.setdefault(page_id, []).append(item)
    for page_id, group in groups.items():
        view.undo_stack.push(commands.ChangeMetadata(
            group, 'canvas_id', [page_id for _ in group]))


def _finish_import(view, plan, outcome):
    """切到第一页、标脏、报数。"""
    view.mark_content_dirty()
    first = outcome.first_page_id
    if first:
        opener = getattr(getattr(view, 'parent', None),
                         'open_workspace_page', None)
        if callable(opener):
            opener(plan.section, first)
    widgets.PrismNotification(view, _report_text(plan, outcome))
    if outcome.errors:
        QtWidgets.QMessageBox.warning(
            view, _('Some files could not be imported'),
            '\n'.join(outcome.errors[:20]))


def _report_text(plan, outcome):
    if plan.section == 'canvas':
        text = _('Imported {canvases} canvas(es) and {folders} folder(s), '
                 'with {assets} asset(s).').format(
                     canvases=outcome.pages, folders=outcome.folders,
                     assets=len(outcome.asset_targets))
    else:
        text = _('Imported {pages} page(s) and {folders} folder(s).').format(
            pages=outcome.pages, folders=outcome.folders)
    if outcome.canceled:
        text += ' ' + _('Cancelled — what was imported so far is kept.')
    return text
