"""Import Eagle into the open project through the existing resource workflow."""
import os
import time
from PyQt6 import QtWidgets


def import_eagle(view):
    from prism.eagle_import import library_root, convert_library
    from prism.actions.storage_workflow import run, _open_result

    if not view._allow_import_during_open():
        return
    if getattr(view, '_import_in_progress', False):
        return
    source = QtWidgets.QFileDialog.getExistingDirectory(view, '选择 Eagle .library 资料库文件夹')
    if not source:
        return
    try:
        root = library_root(source)
    except (OSError, ValueError) as error:
        QtWidgets.QMessageBox.warning(view, '无法读取 Eagle 资料库', str(error))
        return
    if view.filename:
        from prism.eagle_import import prepare_current_import
        identity = (view.scene.project_id, getattr(view, '_trace_open_epoch', None), view.filename)
        def prepared(result):
            if identity != (view.scene.project_id, getattr(view, '_trace_open_epoch', None), view.filename):
                return
            def apply():
                try:
                    _apply_current(view, result)
                except Exception as error:
                    QtWidgets.QMessageBox.warning(view, 'Eagle 导入失败', str(error))
            if not result.models:
                apply()
                return
            composition = view.window().model_composition
            waiting = {'done':False}
            def ready(_callback=None):
                if waiting['done']:
                    return
                current = (view.scene.project_id, getattr(view, '_trace_open_epoch', None), view.filename)
                if current != identity or composition._scope is None:
                    waiting['done'] = True
                    composition.delivered.disconnect(ready)
                elif composition.service is not None:
                    waiting['done'] = True
                    composition.delivered.disconnect(ready)
                    apply()
            # Binding is asynchronous. Observe the existing GUI delivery signal
            # before starting it; never poll or ask the user to retry the import.
            composition.delivered.connect(ready)
            composition.ensure()
            if composition.service is not None:
                ready()
        return run(view, '导入 Eagle 资料库',
            lambda cancel, progress: prepare_current_import(root, cancel=cancel, progress=progress),
            prepared, with_progress=True)
    destination = view._choose_project_destination('选择转换后项目的保存位置',
        suggested_name=root.name.removesuffix('.library'))
    if destination is None:
        return

    def complete(result):
        details = (f'已转换 {result.folders} 个文件夹为画布；'
                   f'{result.images} 张图片、{result.videos} 个视频、{result.models} 个模型。\n'
                   f'{result.deleted} 个删除项保留在回收站。\n原 Eagle 资料库未修改。')
        if result.models:
            details += '\n模型在模型库中，沿用原分类；图片和视频位于画布。'
        if result.attachments:
            details += f'\n另有 {result.attachments} 个非画布格式保存在项目附件中。'
        QtWidgets.QMessageBox.information(view, 'Eagle 转换完成', details)
        _open_result(view, result.entry)

    run(view, '转换 Eagle 资料库',
        lambda cancel, progress: convert_library(root, destination, cancel=cancel, progress=progress),
        complete, with_progress=True)


def _apply_current(view, prepared):
    from prism.actions.import_workflow import _apply_plan, _import_assets
    if not view._allow_import_during_open() or getattr(view, '_import_in_progress', False):
        raise ValueError('当前项目仍在处理导入，请完成后重试')
    service = view.category_panel.resource_service
    models = None
    if prepared.models:
        view.window().model_composition.ensure()
        models = view.window().model_composition.service
        if models is None or models.importer is None:
            raise ValueError('当前项目的模型导入服务尚未就绪，请等待项目打开完成后重试')
        models._require_write()
    outcome = _apply_plan(view, service, prepared.plan, None)
    if outcome.canceled:
        return
    if outcome.errors or len(outcome.page_ids) != len(prepared.plan.pages):
        raise ValueError('\n'.join(outcome.errors) or '部分画布未创建，素材未导入')
    metadata = {}
    for page, page_id in zip(prepared.plan.pages, outcome.page_ids):
        for path in page.assets:
            key = os.path.normpath(path)
            metadata.setdefault(key, []).append(dict(prepared.metadata[path], canvasId=page_id))
    outcome.errors.extend(f'{path}：{reason}' for path,reason in prepared.plan.skipped)
    def loaded():
        if not prepared.models:
            return
        try:
            _import_models_current(view, models, prepared)
        except Exception as error:
            QtWidgets.QMessageBox.warning(view, 'Eagle 模型导入失败', str(error))
    _import_assets(view, prepared.plan, outcome, None, item_metadata=metadata, on_loaded=loaded)
    return outcome


def _import_models_current(view, models, prepared):
    tree = view.category_panel.resource_service
    tree.enable_model_section()
    installed = {}
    def folder(parts):
        if parts not in installed:
            parent = folder(parts[:-1]) if len(parts)>1 else None
            title = tree.unique_suggestion('model',parent,'folder',parts[-1][1])
            installed[parts] = tree.createFolder('model',title,parent)['id']
        return installed[parts]
    for path,meta,folders in prepared.models:
        target = (('eagle',prepared.plan.root_title), *prepared.folder_paths.get(next(iter(folders), None), ()))
        data = dict(name=meta['name'], tags=meta.get('tags') or [], notes=str(meta.get('annotation') or ''),
                    rating=max(0,min(5,int(meta.get('star') or 0))), eagle=meta)
        if meta.get('isDeleted'):
            data['deleted_at'] = int(meta.get('modificationTime') or time.time()*1000)
        models.import_paths((path,), folder_id=folder(target), metadata=data)
    view.mark_content_dirty()
