"""Shared resource sets for all/unclassified/untagged/recent/trash.

Pages and both libraries retain the existing project-search metadata. Image
candidates come from the existing whole-project catalog (or the caller's
already prepared catalog items), including session overlays and unloaded
image proxies. No original bytes are read here. Counters and cards use the
same scope predicates, query filters and valid-resource recent limit.
"""
from __future__ import annotations
from types import SimpleNamespace

from prism.recent_usage import timestamp as _timestamp
from prism.recent_usage import PROJECT_LIMIT

from prism.project_search import search_project_steps

#: 左栏那五个按钮，也是这张列表的五个口径。
SCOPES = ('all', 'unclassified', 'untagged', 'recent', 'trash')

SCOPE_TITLES = {
    'all': '全部素材',
    'unclassified': '未分类',
    'untagged': '未标签',
    'recent': '最近使用',
    'trash': '回收站',
}

#: 搜索结果里的这两类不是资源，是树里的容器和标签 —— 不进素材列表。
_NON_RESOURCE_KINDS = ('folder', 'tag')

#: 图片素材没落到任何画布上时的归属。
_UNASSIGNED_CANVAS = 'default-canvas'


def _has_valid_tags(tags):
    """标签要算「有标签」，至少得有一个非空白字符串。

    跟图片素材那套判定同一个口径（``project_catalog._has_valid_tags``），
    否则同一张卡片在「未标签」里出现与否会看心情。
    """
    return any(str(tag).strip() for tag in (tags or ()))


def _nodes(service):
    return {node['id']: node for node in getattr(service, 'nodes', ()) or ()}


def _deleted_ancestor(node_id, nodes):
    seen = set()
    while node_id in nodes and node_id not in seen:
        seen.add(node_id)
        node = nodes[node_id]
        if node.get('deletedAt') is not None:
            return True
        node_id = node.get('parentId')
    return False


def _deleted_pages(service, page_records):
    nodes = _nodes(service)
    deleted = {node.get('pageId') for node in nodes.values()
               if node.get('pageId') and _deleted_ancestor(node['id'], nodes)}
    deleted.update(page['id'] for page in page_records or ()
                   if page.get('id') and (page.get('deletedAt') or page.get('deleted_at')))
    return deleted


def _page_owners(service):
    """页面 id → 所属文件夹 id（在分区根就是 ``None``）。"""
    owners = {}
    nodes = _nodes(service)
    for node in nodes.values():
        if node.get('nodeType') == 'page' and node.get('pageId'):
            parent = nodes.get(node.get('parentId'))
            owners[node['pageId']] = (parent['id'] if parent
                and parent.get('nodeType') == 'folder'
                and parent.get('section') == node.get('section') else None)
    return owners


def _owner_of(result, owners):
    """这个资源归在哪儿 —— 判定「未分类」的唯一依据。

    * 图片素材：它在哪张画布上（画布本身就是一个页面）。
    * 画布 / 脑图 / 文档：它在树上的父文件夹（在分区根就是没归属）。
    * 材质 / 模型：它在自己那个库里的文件夹。
    """
    kind = result.get('kind')
    if kind == 'asset':
        return result.get('page_id') or _UNASSIGNED_CANVAS
    if kind in ('document', 'mindmap', 'canvas'):
        return owners.get(result.get('page_id'))
    if kind in ('material', 'model'):
        return result.get('folder_id') or None
    return None


def _is_unclassified(result, owners, catalog=None, known=None, nodes=None):
    kind = result.get('kind')
    owner = _owner_of(result, owners)
    if kind == 'asset':
        if catalog is not None:
            return catalog.is_unclassified(result['item'], known)
        # 和图片素材那套一致：默认画布上的（或者没落画布的）算没归类。
        return owner in (_UNASSIGNED_CANVAS, '', None)
    if kind in ('material', 'model') and owner and nodes is not None:
        parent = nodes.get(owner)
        return not parent or parent.get('nodeType') != 'folder' or parent.get('section') != kind
    return not owner


def recent_entries(project_id):
    """最近使用账本（用户级目录，按工程分组）。

    账本记在用户目录里，不写工程文件 —— 工程是用户自己的文件，最近使用
    是应用自己的状态。
    """
    try:
        from prism.recent_usage import ledger
        # Removed/trashed entries must not consume the visible 50-row budget.
        # Join with current resources first, then apply that limit.
        return ledger().recent(project_id or '', limit=None)
    except Exception:                           # noqa: BLE001 - logging only
        return []


def record_use(project_id, section, resource_id, name=''):
    """记一笔「最近使用」（用户级目录，按工程分组）。"""
    if not resource_id:
        return
    try:
        from prism.recent_usage import ledger
        return ledger().record(project_id or '', section, resource_id, name)
    except Exception:                           # noqa: BLE001 - logging only
        pass


def _recent_for(scene, recent):
    """最近使用条目：调用方给了就用给定的，没给就现读账本。

    必须能现读 —— 否则「最近使用」在有人忘了传参时就是空的，而左栏的
    数字和点进去看到的列表会各说各话。
    """
    if recent is not None:
        return recent
    return recent_entries(getattr(scene, 'project_id', ''))


def directory_scope_filters(catalog, scope, *, recent_limit=PROJECT_LIMIT):
    """Shared indexed image candidates for cards and sidebar recent counts."""
    if scope == 'trash':
        return {'include_deleted': True}
    if scope == 'untagged':
        return {'untagged_only': True}
    if scope not in ('unclassified', 'recent'):
        return {}
    filters = {'recent_only': True} if scope == 'recent' else {}
    if scope == 'recent' and recent_limit is not None:
        filters['recent_limit'] = recent_limit
    known, deleted_pages = catalog._known_canvas_ids(), catalog._deleted_page_ids()
    if scope == 'unclassified' or deleted_pages:
        # Resolve buckets in the small aggregate, then seek canvas_id's index.
        groups = catalog._catalog_scope().grouped_counts()
        canvases = {cid for cid, deleted, _untagged, _count in groups
                    if not deleted and (cid not in deleted_pages if scope == 'recent'
                        else cid == 'default-canvas' or cid not in known)}
        filters.update(canvas_ids=tuple(sorted(canvases, key=lambda value: (value is not None, str(value)))),
                       known_canvas_ids=tuple(sorted(known)))
    return filters


def recent_ledger_rows(scope, recent):
    """Join persistent usage IDs without scanning unused project images."""
    stamps = {}
    for entry in recent or ():
        if entry.get('section') != 'asset':
            continue
        try:
            key = int(entry.get('resource_id'))
        except (TypeError, ValueError):
            continue
        stamps[key] = max(stamps.get(key, 0), _timestamp(entry.get('ts')))
    rows = []
    for row in scope.card_rows(stamps):
        row = dict(row)
        row['last_used'] = max(_timestamp(row.get('last_used')), stamps[row['id']])
        rows.append(row)
    return rows


def _result_identity(result):
    """结果 → ``(类别, 资源 id)``，和最近使用账本用的是同一套 id。"""
    result_id = str(result.get('result_id') or '')
    prefix, _, ident = result_id.partition(':')
    if prefix == 'page':
        return result.get('page_kind') or 'canvas', ident
    if prefix in ('asset', 'material', 'model'):
        return prefix, ident
    return prefix, ident


def _missing_page_results(live, page_records, deleted_pages=()):
    """树里还没有节点的页面（默认画布、刚建的脑图）也是资源。

    搜索只在页面**有节点**时才命中它 —— 搜索要的是能定位的位置。素材列表
    要的是项目里所有东西，所以这里把没节点的页面补回来：它们没有归属，
    算未分类。
    """
    known = {r.get('page_id') for r in live if r.get('kind') in
             ('document', 'mindmap', 'canvas')}
    results = []
    for page in page_records or ():
        page_id = page.get('id')
        if not page_id or page_id in known or page_id in deleted_pages:
            continue
        kind = page.get('kind') or 'canvas'
        known.add(page_id)
        results.append(dict(kind=kind, title=page.get('title', ''),
                            tags=list(page.get('tags') or ()),
                            snippet='', matches=[], version='',
                            page_id=page_id, page_kind=kind,
                            result_id='page:' + str(page_id)))
    return results


def _asset_result(item, *, entry=None, deleted=False, include_text=True):
    from prism.project_search import item_key, asset_search_text
    get = lambda name, default='': getattr(entry, name, default) if entry is not None else default
    return dict(kind='asset', title=get('title') or getattr(item, '_title', '')
                    or getattr(item, 'filename', '') or '未命名素材',
                tags=list(get('tags', getattr(item, '_tags', ())) or ()),
                snippet='', matches=[], version='', item=item, item_id=item_key(item),
                page_id=get('canvas_id', getattr(item, '_canvas_id', None)),
                page_kind='canvas', path=get('path', ''),
                rating=get('rating', getattr(item, '_rating', 0)),
                catalog_text=asset_search_text(item) if include_text else '', result_id='asset:' + item_key(item),
                deleted=deleted)


def _asset_results(scene, catalog, *, deleted=False, asset_items=None):
    if asset_items is not None:
        return [_asset_result(item, deleted=deleted) for item in asset_items
                if (getattr(item, '_deleted_at', None) is not None) == deleted]
    if catalog is not None:
        return [_asset_result(entry.item, entry=entry, deleted=deleted)
                for entry in catalog.query(include_deleted=deleted)]
    if getattr(scene, '_partial_project_source', None):
        from prism.project_catalog_index import CatalogNotReady
        raise CatalogNotReady('Whole-project resources require the project asset directory')
    if deleted:
        from prism.project_search import searchable_items
        return [_asset_result(item, deleted=True) for item in searchable_items(scene)
                if getattr(item, '_deleted_at', None) is not None]
    return None


def _live_results(scene, service, page_records=None, catalog=None, asset_items=None, *, assets_only=False):
    steps = _live_results_steps(scene, service, page_records, catalog, asset_items,
                                assets_only=assets_only)
    while True:
        try:
            next(steps)
        except StopIteration as complete:
            return complete.value


def _live_results_steps(scene, service, page_records=None, catalog=None, asset_items=None, *, assets_only=False):
    """项目里所有**活着**的资源结果（不含树容器和标签）。"""
    if page_records is None:
        page_records = getattr(scene, 'workspace_pages', []) or []
    assets = _asset_results(scene, catalog, asset_items=asset_items)
    if assets_only and assets is not None:
        deleted = _deleted_pages(service, page_records)
        return [result for result in assets if result.get('page_id') not in deleted]
    search_scene = scene
    search_pages = page_records
    if assets is not None:
        # Image candidates already came from the directory. Do not build their
        # search cards (or walk canvas contents) only to discard them below.
        # Keep the existing search core and its library/path/tag semantics.
        from prism import tags
        search_pages = [dict(page, content=None) for page in page_records]
        search_scene = SimpleNamespace(items=tuple, iter_assets=tuple,
            workspace_nodes=getattr(scene, 'workspace_nodes', ()),
            workspace_pages=search_pages, material_records=getattr(scene, 'material_records', ()),
            model_records=getattr(scene, 'model_records', ()), tag_system=tags.tag_system(scene),
            note_html='', mindmap_tree=None)
    results = yield from search_project_steps(search_scene, service, '',
                                              page_records=search_pages, allow_empty=True)
    results = [r for r in results if r.get('kind') not in _NON_RESOURCE_KINDS]
    deleted = _deleted_pages(service, page_records)
    results += _missing_page_results(results, page_records, deleted)
    if assets is not None:
        results = [r for r in results if r.get('kind') != 'asset'] + assets
    nodes = _nodes(service)
    results = [r for r in results if (
        r.get('page_id') not in deleted if r.get('kind') in ('asset', 'canvas', 'mindmap', 'document')
        else not _deleted_ancestor(r.get('folder_id'), nodes))]
    return _library_covers(results, scene)


def _library_covers(results, scene):
    """Carry existing cover metadata; visible-card painting reads the bytes."""
    materials = {record['id']: record.get('preview') or {}
                 for record in getattr(scene, 'material_records', ()) or ()}
    models = {record.get('id') or record.get('model_id'): record.get('thumbnail') or {}
              for record in getattr(scene, 'model_records', ()) or ()}
    for result in results:
        if result.get('kind') == 'material':
            result['cover'] = materials.get(result.get('material_id'), {})
        elif result.get('kind') == 'model':
            result['cover'] = models.get(result.get('model_id'), {})
    return results


def _to_items(results):
    """结果 → 网格能装的条目。

    图片素材是素材对象（网格用 ``lod.draw_image`` 画真实缩略图），其余是
    结果字典（网格用 ``SearchCard`` 画卡片）。
    """
    items = []
    for result in results:
        if result.get('kind') == 'asset':
            item = result.get('item')
            if item is not None:
                items.append(item)
        else:
            items.append(result)
    return items


def _trash_results(scene, service, catalog=None, page_records=None, asset_items=None, *, assets_only=False):
    steps = _trash_results_steps(scene, service, catalog, page_records, asset_items,
                                 assets_only=assets_only)
    while True:
        try:
            next(steps)
        except StopIteration as complete:
            return complete.value


def _trash_results_steps(scene, service, catalog=None, page_records=None, asset_items=None, *, assets_only=False):
    """项目里所有**软删**的资源结果。

    搜索只看活着的资源，软删的那些得单独收 —— 收的时候用的是同一套结果
    形态，卡片才能和「全部素材」里的一模一样。
    """

    if assets_only and asset_items is not None:
        return _asset_results(scene, catalog, deleted=True, asset_items=asset_items)
    results = []
    if page_records is None:
        page_records = getattr(scene, 'workspace_pages', []) or []
    # 页面类型（画布/脑图/文档）从页面记录里取：软删的树节点只存了标题，
    # 不存类型 —— 硬编码成「文档」会让回收站里的画布显示成文档。
    pages = {page.get('id'): page for page in page_records or ()}
    all_nodes = _nodes(service)
    nodes = {key: n for key, n in all_nodes.items() if _deleted_ancestor(key, all_nodes)}
    for node in nodes.values():
        yield
        if node.get('nodeType') != 'page':
            continue
        page_id = node.get('pageId')
        if not page_id:
            continue
        page = pages.get(page_id, {})
        kind = page.get('kind') or node.get('section') or 'canvas'
        title = node.get('title') or ''
        results.append(dict(kind=kind, title=title, tags=list(page.get('tags') or ()),
                            snippet='', matches=[], version='',
                            page_id=page_id, page_kind=kind,
                            owner_id=node.get('parentId'), path='',
                            result_id='page:' + str(page_id),
                            deleted=True))
    known = {result.get('page_id') for result in results}
    for page_id in _deleted_pages(service, page_records) - known:
        yield
        page = pages.get(page_id)
        if page is not None:
            kind = page.get('kind') or 'canvas'
            results.append(dict(kind=kind, title=page.get('title', ''),
                                tags=list(page.get('tags') or ()), snippet='', matches=[],
                                version='', page_id=page_id, page_kind=kind,
                                result_id='page:' + str(page_id), deleted=True))
    records = getattr(scene, 'material_records', ()) or ()
    for material in records:
        yield
        if not material.get('deletedAt') and not _deleted_ancestor(material.get('folderId'), all_nodes):
            continue
        results.append(dict(kind='material', title=material.get('name', ''),
                            tags=list(material.get('tags') or ()),
                            snippet='', matches=[], version='',
                            material_id=material.get('id'),
                            folder_id=material.get('folderId'),
                            page_kind='material', path='材质库',
                            result_id='material:' + str(material.get('id')),
                            deleted=True))
    models = getattr(scene, 'model_records', ()) or ()
    for model in models:
        yield
        if not model.get('deleted_at') and not _deleted_ancestor(model.get('folder_id'), all_nodes):
            continue
        model_id = model.get('id') or model.get('model_id')
        results.append(dict(kind='model', title=model.get('name', ''),
                            tags=list(model.get('tags') or ()),
                            snippet='', matches=[], version='',
                            model_id=model_id,
                            folder_id=model.get('folder_id'),
                            page_kind='model', path='模型库',
                            result_id='model:' + str(model_id),
                            deleted=True))
    assets = _asset_results(scene, catalog, deleted=True, asset_items=asset_items)
    if assets is not None:
        results.extend(assets)
    return _library_covers(results, scene)


def _recent_stamps(recent):
    stamps = {}
    for entry in recent or ():
        section, key = entry.get('section'), entry.get('resource_id')
        if section and key is not None:
            identity = (section, str(key))
            stamps[identity] = max(stamps.get(identity, 0.0), _timestamp(entry.get('ts')))
    return stamps


def _recent_stamp(result, stamps):
    value = stamps.get(_result_identity(result), 0.0)
    if result.get('kind') == 'asset':
        value = max(value, _timestamp(getattr(result.get('item'), '_last_used', None)))
    return value


def _page_text(page):
    from prism.project_search import plain_text
    if page.get('kind') != 'mindmap':
        return plain_text(page.get('content') or '')
    texts, pending = [], [page.get('content')]
    while pending:
        node = pending.pop()
        if not isinstance(node, dict):
            continue
        data = node.get('data', {})
        if isinstance(data, dict):
            texts.extend(plain_text(data.get(key, '')) for key in ('text', 'note', 'hyperlink'))
            node_tags = data.get('tag') or []
            texts.extend([node_tags] if isinstance(node_tags, str) else map(str, node_tags))
        pending.extend(child for child in node.get('children', ()) or () if isinstance(child, dict))
    return ' '.join(texts)


def _filter_results(results, scene, page_records, **options):
    matches = _make_result_filter(scene, page_records, **options)
    return [result for result in results if matches(result)]


def folder_page_results(scene, page_ids, *, page_records, **options):
    """Page cards for a folder, without scanning images or opening editors."""
    pages = [page for page in page_records if page.get('id') in page_ids]
    return _filter_results(_missing_page_results([], pages), scene, pages, **options)


def _make_result_filter(scene, page_records, *, text='', tag_expression='',
                    active_tags=(), min_rating=0, kind=None):
    """One predicate for counters and cards, using the existing tag rules."""
    from prism import tags
    from prism.search_candidates import matches_words, query_words
    words = query_words(text)
    active = set(active_tags or ())
    predicate = tags.parse_query(tag_expression) if tag_expression else None
    system = tags.tag_system(scene)
    pages = {page.get('id'): page for page in page_records or ()} if words else {}
    materials = ({str(record.get('id')): record for record in getattr(scene, 'material_records', ()) or ()}
                 if words else {})
    models = ({str(record.get('id') or record.get('model_id')): record
               for record in getattr(scene, 'model_records', ()) or ()} if words else {})
    wanted = 'asset' if kind in ('pixmap', 'image') else kind
    def matches(result):
        resource_kind = result.get('kind')
        if wanted not in (None, '', 'all') and resource_kind != wanted:
            return False
        index = tags.name_index(result.get('tags') or (), system) if (active or predicate is not None or words) else ()
        if active and not active.issubset(index):
            return False
        if predicate is not None and not predicate.matches(index):
            return False
        if resource_kind == 'asset' and min_rating and int(result.get('rating',
                getattr(result.get('item'), '_rating', 0)) or 0) < int(min_rating):
            return False
        if words:
            parts = [str(result.get(key) or '') for key in ('title', 'path', 'snippet', 'catalog_text')]
            parts.extend(index)
            if resource_kind in ('canvas', 'mindmap', 'document'):
                parts.append(_page_text(pages.get(result.get('page_id'), {})))
            elif resource_kind in ('material', 'model'):
                records = materials if resource_kind == 'material' else models
                record = records.get(_result_identity(result)[1], {})
                parts.extend(str(record.get(key) or '') for key in ('notes', 'format'))
                parts.extend(str(file.get('name') or '') for file in record.get('files', ())
                             if isinstance(file, dict))
            if not matches_words(' '.join(parts).casefold(), words):
                return False
        return True
    return matches


def _scope_predicate(scope, service, catalog):
    if scope == 'unclassified':
        owners, nodes = _page_owners(service), _nodes(service)
        known = catalog._known_canvas_ids() if catalog is not None else None
        return lambda result: _is_unclassified(result, owners, catalog, known, nodes)
    if scope == 'untagged':
        return lambda result: not _has_valid_tags(result.get('tags'))
    return lambda result: True


def _select(results, scope, service, catalog, recent, recent_limit):
    if scope == 'recent':
        stamps = _recent_stamps(recent)
        timed = [(_recent_stamp(result, stamps), position, result)
                 for position, result in enumerate(results)]
        timed = [entry for entry in timed if entry[0] > 0]
        timed.sort(key=lambda entry: (-entry[0], entry[1]))
        timed = timed[:min(PROJECT_LIMIT, max(0, int(recent_limit))) if recent_limit is not None else PROJECT_LIMIT]
        return [entry[2] for entry in timed]
    if scope not in ('unclassified', 'untagged'):
        return results
    matches = _scope_predicate(scope, service, catalog)
    return [result for result in results if matches(result)]


def _recent_image_count(scene, service, catalog, scope, filters, recent, page_records, **options):
    """SQL total plus only dirty/ledger rows; never construct every used image."""
    import json
    overlay, fresh = catalog._session_overlay()
    entries = _recent_for(scene, recent)
    ids = set(overlay)
    ids.update(str(entry.get('resource_id')) for entry in entries
               if entry.get('section') == 'asset')
    numeric_ids = [int(key) for key in ids if str(key).isdigit()]
    old = []
    for row in scope.card_rows(numeric_ids):
        tags = row.get('tags') or []
        if isinstance(tags, str):
            tags = json.loads(tags)
        old.append(SimpleNamespace(save_id=row['id'], TYPE=row['kind'],
            _canvas_id=row.get('canvas_id'), _tags=tags, _title=row.get('title', ''),
            filename=row.get('filename', ''), _notes=row.get('notes', ''),
            _rating=row.get('rating', 0), _deleted_at=row.get('deleted_at'),
            _last_used=row.get('last_used', 0)))
    old = [item for item in old if not catalog.is_annotation(item)]
    current = [item for item in old if str(item.save_id) not in overlay]
    current.extend(item for item in list(overlay.values()) + list(fresh)
                   if not catalog.is_annotation(item))
    arguments = dict(catalog=catalog, page_records=page_records, kind='asset', **options)
    before = len(collect(scene, service, 'recent', recent=[], asset_items=old, **arguments))
    after = len(collect(scene, service, 'recent', recent=entries, asset_items=current, **arguments))
    return scope.count(**filters) - before + after


def _recent_directory_rows(catalog, scope, signature, filters):
    """A page-use timestamp changes the join, not the persisted image rows.

    Keep one bounded recent query per catalog, guarded by the same source/cache
    signature as directory proxies. Dirty overlays and the user ledger are
    merged afresh by the caller and must never be cached here.
    """
    key = (signature, tuple(sorted(filters.items())))
    cached = getattr(catalog, '_resource_recent_rows', None)
    if cached is not None and cached[0] == key:
        return cached[1]
    rows = tuple(scope.iter_query(**filters))
    catalog._resource_recent_rows = (key, rows)
    return rows


def scope_counts(scene, service, *, recent=None, catalog=None, page_records=None,
                 asset_items=None, text='', tag_expression='', active_tags=(),
                 min_rating=0, kind=None, recent_limit=PROJECT_LIMIT, image_counts=None, domain_snapshot=None):
    """The same filtered resource sets used by collect, including recent's cap."""
    if page_records is None:
        page_records = getattr(scene, 'workspace_pages', ()) or ()
    if kind not in (None, '', 'all', 'asset', 'pixmap', 'image'):
        asset_items = []
    if asset_items is None and catalog is not None and (text.strip() or tag_expression or active_tags or min_rating):
        scope = catalog._catalog_scope()
        if scope is not None:
            from prism import tags
            overlay, fresh = catalog._session_overlay()
            signature = catalog._directory_signature(scope)
            options = dict(text=text, tag_expression=tag_expression, active_tags=active_tags,
                           min_rating=min_rating, kind=kind, recent_limit=recent_limit)
            counts = scope_counts(scene, service, recent=recent, catalog=catalog,
                page_records=page_records, asset_items=[], domain_snapshot=domain_snapshot, **options)
            predicates = (tags.parse_query(tag_expression),) if tag_expression else ()
            def filters_for(name):
                filters = dict(directory_scope_filters(catalog, name), text=text, min_rating=min_rating)
                deleted = name == 'trash'
                if active_tags or predicates:
                    filters['tag_sets'] = tags.matching_tag_sets(scope.tag_groups(include_deleted=deleted),
                        tags.tag_system(scene), active=active_tags, predicates=predicates)
                deleted_pages = catalog._deleted_page_ids()
                if name in ('all','untagged') and deleted_pages:
                    filters['canvas_ids'] = tuple({cid for cid,dead,_tagged,_count in scope.grouped_counts()
                                                  if not dead and cid not in deleted_pages})
                return filters
            # SQL answers totals; only the current session's overridden rows
            # need per-item predicates. Never install 90k proxies to count them.
            ids = list(overlay)
            import json
            old = [SimpleNamespace(save_id=row['id'], TYPE=row['kind'],
                       _canvas_id=row['canvas_id'], _tags=json.loads(row['tags'] or '[]'),
                       _title=row.get('title',''), filename=row.get('filename',''),
                       _notes=row.get('notes',''), _rating=row.get('rating',0),
                       _deleted_at=row['deleted_at'], _last_used=0)
                   for start in range(0,len(ids),1024) for row in scope.summaries_grouped(ids[start:start+1024])]
            old = [item for item in old if not catalog.is_annotation(item)]
            current = [item for item in list(overlay.values())+list(fresh) if not catalog.is_annotation(item)]
            image_options = dict(options, kind='asset')
            before = scope_counts(scene, service, recent=recent, catalog=catalog,
                page_records=page_records, asset_items=old, **image_options)
            after = scope_counts(scene, service, recent=recent, catalog=catalog,
                page_records=page_records, asset_items=current, **image_options)
            for name in ('all','unclassified','untagged','trash'):
                counts[name] += scope.count(**filters_for(name)) - before[name] + after[name]
            cap = None if recent_limit is None else recent_limit+len(overlay)+len(fresh)
            recent_filters = filters_for('recent')
            if recent_limit is None:
                counts['recent'] += _recent_image_count(scene, service, catalog, scope,
                    recent_filters, recent, page_records, text=text,
                    tag_expression=tag_expression, active_tags=active_tags, min_rating=min_rating)
                return counts
            if cap is not None:recent_filters['recent_limit'] = cap
            rows = {str(row['id']):row for row in _recent_directory_rows(catalog, scope, signature, recent_filters)}
            rows.update((str(row['id']),row) for row in recent_ledger_rows(scope,_recent_for(scene,recent)))
            candidates = [catalog._directory_item(row,scope,signature) for key,row in rows.items() if key not in overlay]
            candidates = [item for item in candidates if not catalog.is_annotation(item)]
            candidates.extend(current)
            counts['recent'] = len(collect(scene,service,'recent',recent=recent,catalog=catalog,
                page_records=page_records,asset_items=candidates,**options))
            return counts
    # Reuse the directory's aggregate counters and canonical dirty overlay.
    # Counting the sidebar must not materialize every asset before a scoped
    # list can open. Only recent candidates need individual metadata here.
    if (asset_items is None and catalog is not None and not
            (text.strip() or tag_expression or active_tags or min_rating)
            and kind in (None, '', 'all', 'asset', 'pixmap', 'image')):
        scope = catalog._catalog_scope()
        if scope is not None:
            images = image_counts if image_counts is not None else catalog._catalog_count_snapshot(scope)
            if images is None:
                from prism.project_catalog_index import CatalogNotReady
                raise CatalogNotReady('Directory counters are not ready')
            counts = scope_counts(scene, service, recent=recent, catalog=catalog,
                page_records=page_records, asset_items=[], kind=kind,
                recent_limit=recent_limit, domain_snapshot=domain_snapshot)
            for key, image_key in (('all', 'total'), ('unclassified', 'unclassified'),
                                   ('untagged', 'untagged')):
                counts[key] += images[image_key]
            counts['trash'] += catalog.trash_asset_count()
            overlay, fresh = catalog._session_overlay()
            cap = None if recent_limit is None else recent_limit + len(overlay) + len(fresh)
            signature = catalog._directory_signature(scope)
            filters = directory_scope_filters(catalog, 'recent', recent_limit=cap)
            if recent_limit is None:
                counts['recent'] += _recent_image_count(scene, service, catalog, scope,
                    filters, recent, page_records)
                return counts
            rows = {str(row['id']): row for row in _recent_directory_rows(catalog, scope, signature, filters)}
            rows.update((str(row['id']), row) for row in recent_ledger_rows(scope, _recent_for(scene, recent)))
            candidates = []
            for key, row in rows.items():
                if key not in overlay:
                    item = catalog._directory_item(row, scope, signature)
                    item._last_used = max(_timestamp(item._last_used), _timestamp(row.get('last_used')))
                    candidates.append(item)
            candidates.extend(overlay.values())
            candidates.extend(fresh)
            candidates = [item for item in candidates if not catalog.is_annotation(item)]
            counts['recent'] = len(collect(scene, service, 'recent', recent=recent,
                catalog=catalog, page_records=page_records, asset_items=candidates,
                kind=kind, recent_limit=recent_limit))
            return counts
    options = dict(text=text, tag_expression=tag_expression, active_tags=active_tags,
                   min_rating=min_rating, kind=kind)
    assets_only = kind in ('asset', 'pixmap', 'image')
    if domain_snapshot is not None and asset_items is not None and not asset_items and not assets_only:
        live, trash = domain_snapshot
    else:
        live = _live_results(scene, service, page_records, catalog, asset_items, assets_only=assets_only)
        trash = _trash_results(scene, service, catalog, page_records, asset_items, assets_only=assets_only)
    live = _filter_results(live, scene, page_records, **options)
    trash = _filter_results(trash, scene, page_records, **options)
    recent = _recent_for(scene, recent)
    return {scope: len(trash) if scope == 'trash' else
            len(_select(live, scope, service, catalog, recent, recent_limit))
            for scope in SCOPES}


def resource_domain_snapshot(scene, service, page_records):
    """Metadata only; the caller owns revision-based invalidation of this pair."""
    steps = resource_domain_snapshot_steps(scene, service, page_records)
    while True:
        try:
            next(steps)
        except StopIteration as complete:
            return complete.value


def resource_domain_snapshot_steps(scene, service, page_records):
    live = yield from _live_results_steps(scene, service, page_records, asset_items=[])
    deleted = yield from _trash_results_steps(scene, service, page_records=page_records, asset_items=[])
    return (tuple(live), tuple(deleted))


def collect_steps(scene, service, scope, *, asset_items, catalog=None, recent=None,
                  page_records=None, text='', tag_expression='', active_tags=(),
                  min_rating=0, kind=None, recent_limit=PROJECT_LIMIT, domain_snapshot=None):
    """Cooperative form of collect for the existing GUI installation loop.

    Shares the same predicates and scope rules. Only recent needs to retain
    result wrappers for its global timestamp ordering; other images return to
    their original objects as each row is accepted.
    """
    import time
    from itertools import chain
    if scope not in SCOPES:
        raise ValueError(f'Unknown resource scope: {scope}')
    if page_records is None:
        page_records = getattr(scene, 'workspace_pages', ()) or ()
    matches = _make_result_filter(scene, page_records, text=text, tag_expression=tag_expression,
        active_tags=active_tags, min_rating=min_rating, kind=kind)
    scope_matches = _scope_predicate(scope, service, catalog)
    deleted = _deleted_pages(service, page_records)
    if domain_snapshot is not None:
        others = domain_snapshot[1 if scope == 'trash' else 0]
    else:
        # Drive metadata search with the same budget as image filtering. A
        # generator around the final image loop alone leaves a synchronous
        # full-domain search before its first checkpoint.
        steps = (_trash_results_steps(scene, service, catalog, page_records, []) if scope == 'trash'
                 else _live_results_steps(scene, service, page_records, catalog, []))
        deadline = time.perf_counter() + .004
        metadata_rows = 0
        while True:
            try:
                next(steps)
            except StopIteration as complete:
                others = complete.value
                break
            metadata_rows += 1
            if metadata_rows % 64 == 0 or time.perf_counter() >= deadline:
                yield
                deadline = time.perf_counter() + .004
    yield
    source_images = asset_items if kind in (None, '', 'all', 'asset', 'pixmap', 'image') else ()
    images = (item for item in source_images
              if (getattr(item, '_deleted_at', None) is not None) == (scope == 'trash')
              and (scope == 'trash' or getattr(item, '_canvas_id', None) not in deleted))
    plain_images = (scope in ('all', 'untagged', 'trash')
                    and not (text.strip() or tag_expression or active_tags or min_rating))
    candidates = chain(others, (() if plain_images else
        (_asset_result(item, deleted=scope == 'trash', include_text=bool(text.strip()))
         for item in images)))
    accepted = []
    deadline = time.perf_counter() + .004
    for number, result in enumerate(candidates, 1):
        if matches(result) and scope_matches(result):
            accepted.append(result if scope == 'recent' or result.get('kind') != 'asset' else result['item'])
        if number % 256 == 0 or time.perf_counter() >= deadline:
            yield
            deadline = time.perf_counter() + .004
    if plain_images:
        # Already-owned image objects need no transient search dictionaries in
        # these scopes. In a 100k browser those wrappers add GC pressure even
        # though none survives the final list publication.
        for number, item in enumerate(images, 1):
            if scope != 'untagged' or not _has_valid_tags(getattr(item, '_tags', ())):
                accepted.append(item)
            if number % 256 == 0 or time.perf_counter() >= deadline:
                yield
                deadline = time.perf_counter() + .004
    if scope == 'recent':
        return _to_items(_select(accepted, scope, service, catalog, _recent_for(scene, recent), recent_limit))
    return accepted


def collect(scene, service, scope, *, recent=None, catalog=None, page_records=None,
            asset_items=None, text='', tag_expression='', active_tags=(),
            min_rating=0, kind=None, recent_limit=PROJECT_LIMIT):
    """Mixed resources under one scope; no directory truncation or image reads.

    asset_items is an optional complete image candidate set already obtained by
    the caller's directory pipeline. An explicit [] means other domains only.
    Omitting it uses catalog.query; a partial project never falls back to one
    loaded canvas and pretends that it is the whole project.
    """
    if scope not in SCOPES:
        raise ValueError(f'未知口径: {scope}')
    if page_records is None:
        page_records = getattr(scene, 'workspace_pages', ()) or ()
    assets_only = kind in ('asset', 'pixmap', 'image')
    results = (_trash_results(scene, service, catalog, page_records, asset_items, assets_only=assets_only)
               if scope == 'trash' else
               _live_results(scene, service, page_records, catalog, asset_items, assets_only=assets_only))
    results = _filter_results(results, scene, page_records, text=text,
                              tag_expression=tag_expression, active_tags=active_tags,
                              min_rating=min_rating, kind=kind)
    selected = _select(results, scope, service, catalog,
                       _recent_for(scene, recent), recent_limit)
    return _to_items(selected)
