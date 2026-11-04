"""Project-wide search snapshots, separate from canvas visibility filters."""
from html.parser import HTMLParser
from pathlib import Path
from prism import tags
from prism.project_open_scope import OpenScopeCancelled
from prism.search_candidates import (SearchCancelled, cached_trigram_status,
                                     catalog_tag_names, catalog_trigram_plan,
                                     iter_catalog_candidate_rows, matches_words,
                                     open_readonly_cache, query_words)
import hashlib
import json
import sqlite3


def node_path(node, nodes):
    """Resource-tree titles, shared by search references and the inspector."""
    parts, seen = [], set()
    while node and node['id'] not in seen:
        seen.add(node['id'])
        parts.append(node['title'])
        node = nodes.get(node.get('parentId'))
    return ' / '.join(reversed(parts))


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag in ('p', 'div', 'li', 'br', 'h1', 'h2', 'h3'):
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)
        if tag in ('p', 'div', 'li', 'br', 'h1', 'h2', 'h3'):
            self.parts.append(' ')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value):
    parser = _Text()
    parser.feed(str(value or ''))
    return ' '.join(''.join(parser.parts).split())


def asset_search_text(item):
    return ' '.join([str(getattr(item, '_title', '') or ''),
                     str(getattr(item, 'filename', '') or ''),
                     str(getattr(item, '_notes', '') or ''),
                     str(getattr(item, '_source_url', '') or ''),
                     ' '.join(getattr(item, '_categories', []) or []),
                     str(getattr(item, 'toPlainText', lambda: '')())])


def searchable_items(scene):
    # Metadata search does not need Qt stacking order. Include parked canonical
    # objects: their unsaved edits outrank old disk catalog summaries as well.
    iterate = getattr(scene, 'iter_assets', scene.items)
    items = [i for i in iterate() if hasattr(i, 'save_id') or getattr(i, '_asset_source', None)]
    session = getattr(scene, '_partial_project_session', None)
    residency = getattr(session, '_residency', None)
    seen = {id(item) for item in items}
    for item in getattr(residency, 'parked', {}).values():
        if id(item) not in seen:
            seen.add(id(item))
            items.append(item)
    return items


def item_key(item):
    key = getattr(item, 'save_id', None)
    if key is None:
        key = getattr(item, 'original_save_id', None)
    return str(id(item) if key is None else key)


def rank_result(result, needle):
    """Stable sort key shared by the in-memory and catalog search paths.

    Exact title matches rank first, then title substrings, then hits whose
    matches land in body/node/canvas_item fields, then everything else. The
    ``title``/``result_id`` tie-breakers keep the order deterministic across
    repeated queries and across the two candidate sources.
    """
    title = result['title'].casefold()
    fields = {m['field'] for m in result['matches']}
    score = (0 if title == needle else
             1 if needle and needle in title else
             2 if fields & {'body', 'node', 'canvas_item'} else 3)
    return score, title, result['result_id']


def _build_result(kind, title, fields, tag_names, system, words,
                  predicate=None, allow_empty=False, **target):
    """Build one search hit with the shared match/version semantics.

    ``fields`` entries carry ``field``/``value`` plus optional navigation keys
    (``item_id``/``node_path``/``node_uid``); ``target`` holds result-level keys
    (``result_id``/``page_id``/``page_kind``/``path``/...). Returns ``None``
    when the hit does not match the predicate or the AND words.

    This is the single point that turns raw fields into ``matches`` positions,
    ``snippet`` and ``version``, so the in-memory fast path and the catalog
    candidate path cannot drift apart.
    """
    index = tags.name_index(tag_names, system)
    haystack = ' '.join([title, target.get('path', ''), *index,
                         *(f['value'] for f in fields)]).casefold()
    if predicate is not None:
        if not predicate.matches(index):
            return None
    elif words and not matches_words(haystack, words):
        return None
    elif not words and not allow_empty:
        return None
    fields = list(fields) + [dict(field='title', value=title),
                             dict(field='path', value=target.get('path', '')),
                             dict(field='tags', value=' '.join(sorted(index)))]
    matches = []
    for field in fields:
        for word in words:
            hay = field['value'].casefold()
            position, occurrence = hay.find(word), 0
            while position >= 0:
                matches.append({k: v for k, v in field.items() if k != 'value'} |
                               {'text': word, 'position': position, 'occurrence': occurrence})
                occurrence += 1
                position = hay.find(word, position + len(word))
    snippet = next((f['value'] for f in fields
                    if any(w in f['value'].casefold() for w in words)), '')
    first = matches[0] if matches else {}
    position = first.get('position', 0)
    return dict(kind=kind, title=title,
                snippet=snippet[max(0, position-24):position+100],
                matches=matches, tags=list(tag_names),
                version=hashlib.sha256(haystack.encode('utf-8')).hexdigest(), **target)


def visible_models(scene):
    from prism.material_service import iter_visible_library_records
    return iter_visible_library_records(scene, getattr(scene, 'model_records', ()),
                                        folder_field='folder_id', deleted_field='deleted_at')


def search_project(scene, service, query, panels=(), tag_expression='', page_records=None, allow_empty=False):
    """Read-only query: one hit per page, with concrete content navigation targets."""
    steps = search_project_steps(scene, service, query, panels, tag_expression,
                                 page_records, allow_empty)
    while True:
        try:
            next(steps)
        except StopIteration as complete:
            return complete.value


def search_project_steps(scene, service, query, panels=(), tag_expression='', page_records=None, allow_empty=False):
    """The same search, with checkpoints between resource records.

    No background access to live scene objects. The caller owns scheduling
    and rejects a result if its project/metadata revision changed meanwhile.
    """
    system = tags.tag_system(scene)
    words = list(dict.fromkeys(query.casefold().split()))
    predicate = tags.parse_query(tag_expression) if tag_expression else None
    nodes = {n['id']: n for n in service.nodes if not n.get('deletedAt')}
    by_page = {n.get('pageId'): n for n in nodes.values() if n['nodeType'] == 'page'}
    deleted_pages = {n.get('pageId') for n in service.nodes if n.get('deletedAt') and n.get('pageId')}
    pages = page_records if page_records is not None else getattr(scene, 'workspace_pages', [])
    live = {getattr(p, 'page_id', None) or ('default-document' if hasattr(p, '_html') else 'default-mindmap'): p for p in panels}
    results = []

    def path(node):
        return node_path(node, nodes)

    def add(kind, title, fields=(), tag_names=(), **target):
        result = _build_result(kind, title, fields, tag_names, system, words,
                               predicate=predicate, allow_empty=allow_empty, **target)
        if result is None:
            return
        body_matches = [m for m in result['matches'] if m['field'] == 'body']
        node_matches = [m for m in result['matches'] if m['field'] == 'node']
        if kind == 'document' and body_matches:
            result.update(kind='document_text', match_text=body_matches[0]['text'])
        elif kind == 'mindmap' and node_matches:
            result.update(kind='mindmap_node', node_path=node_matches[0]['node_path'],
                          node_uid=node_matches[0]['node_uid'])
        results.append(result)

    items = [i for i in searchable_items(scene) if getattr(i, '_deleted_at', None) is None
             and getattr(i, '_canvas_id', None) not in deleted_pages]
    canvas_items = {}
    for item in items:
        canvas_items.setdefault(getattr(item, '_canvas_id', None) or 'default-canvas', []).append(item)
    for node in nodes.values():
        yield
        if node['nodeType'] == 'folder':
            add('folder', node['title'], node_id=node['id'], path=path(node), result_id='folder:'+node['id'])
    for name in sorted(tags.scene_tag_names(scene)):
        yield
        add('tag', name, tag_names=[name], tag=name, path='标签', result_id='tag:'+name)
    for page in pages:
        yield
        page_id = page['id']
        node = by_page.get(page_id)
        if not node or page_id in deleted_pages:
            continue
        kind = page['kind']
        panel = live.get(page_id)
        content = page.get('content')
        fields = []
        if kind == 'document':
            current = getattr(panel, '_html', None)
            content = current if current is not None else content
            if content is None and page_id == 'default-document':
                content = getattr(scene, 'note_html', '')
            fields.append({'field': 'body', 'value': plain_text(content)})
        elif kind == 'mindmap':
            current = getattr(panel, '_tree', None)
            content = current if current is not None else content
            if content is None and page_id == 'default-mindmap':
                content = getattr(scene, 'mindmap_tree', None)
            def walk(tree, indices=()):
                if not isinstance(tree, dict):
                    return
                data = tree.get('data', {})
                value = plain_text(data.get('text') or tree.get('title') or tree.get('topic') or tree.get('text'))
                value += ' ' + plain_text(data.get('note', '')) + ' ' + str(data.get('hyperlink', ''))
                node_tags = data.get('tag', []) or []
                value += ' ' + ' '.join([node_tags] if isinstance(node_tags, str) else node_tags)
                fields.append(dict(field='node', value=value, node_path=list(indices), node_uid=data.get('uid')))
                for index, child in enumerate(tree.get('children') or tree.get('nodes') or []):
                    walk(child, indices+(index,))
            walk(content)
        elif kind == 'canvas':
            for item in canvas_items.get(page_id, ()):
                fields.append(dict(field='canvas_item', value=asset_search_text(item) + ' ' +
                                   ' '.join(tags.name_index(getattr(item, '_tags', []), system)), item_id=item_key(item)))
        add(kind, page['title'], fields, page.get('tags', []), page_id=page_id,
            page_kind=kind, path=path(node), result_id='page:'+page_id)
    residency = getattr(getattr(scene, '_partial_project_session', None), '_residency', None)
    parked_ids = {id(item) for item in getattr(residency, 'parked', {}).values()}
    for item in items:
        yield
        if getattr(item, 'TYPE', '') in ('text', 'path'):
            continue
        canvas_id = getattr(item, '_canvas_id', None)
        node = by_page.get(canvas_id)
        title = str(getattr(item, '_title', '') or getattr(item, 'filename', '') or
                    getattr(item, 'toPlainText', lambda: '素材')())
        navigation = (dict(catalog=True, rating=int(getattr(item, '_rating', 0) or 0),
                           catalog_text=asset_search_text(item)) if id(item) in parked_ids else {})
        add('asset', title, [dict(field='asset', value=asset_search_text(item), item_id=item_key(item))],
            getattr(item, '_tags', []), item=item, item_id=item_key(item), result_id='asset:'+item_key(item),
            page_id=canvas_id, page_kind='canvas', path=path(node) if node else '画布', **navigation)
    from prism.material_service import iter_visible_materials
    for material in iter_visible_materials(scene):
        yield
        folder = material.get('folderId')
        fields = [dict(field='material', value=material.get('notes', '')),
                  dict(field='files', value=' '.join(file.get('name', '') for file in material.get('files', []))),
                  dict(field='engine', value=' '.join(str(package.get('engineVersion', ''))
                       for package in material.get('nativePackages', []) if isinstance(package, dict)))]
        add('material', material['name'], fields, material.get('tags', []),
            material_id=material['id'], folder_id=folder, page_kind='material',
            path='材质库' + (' / '+path(nodes[folder]) if folder in nodes else ''),
            result_id='material:'+material['id'])
    for model in visible_models(scene):
        yield
        folder = model.get('folder_id')
        model_id = model.get('id') or model['model_id']
        fields = [dict(field='notes', value=model.get('notes', '')),
                  dict(field='format', value=model.get('format', '')),
                  dict(field='files', value=' '.join(
                      file.get('relative_path', '') for file in model.get('files', ())))]
        add('model', model['name'], fields, model.get('tags', []),
            model_id=model_id, folder_id=folder, page_kind='model',
            path='模型库'+(' / '+path(nodes[folder]) if folder in nodes else ''),
            result_id='model:'+model_id)
    needle = query.casefold().strip()
    return sorted(results, key=lambda result: rank_result(result, needle))


def _parse_tags(raw):
    """Catalog tags column -> list of tag name strings; never raises."""
    return catalog_tag_names(raw)


def _catalog_cache_path(scope):
    """The catalog cache file behind a ready scope, or ``None``."""
    path = getattr(scope, 'cache_path', None)
    if path is None:
        path = getattr(getattr(scope, 'index', None), 'cache', None)
    return Path(path) if path else None


def _trigram_catalog_rows(scope, query, cancel=None):
    """Catalog rows shortlisted by the trigram index, or ``None``.

    ``None`` means "no usable trigram index": the caller then runs the exact
    scan. A query whose words are all shorter than three characters has no
    indexable anchor, so it always takes that fallback.

    Every failure to *read* the index is also a fallback, never an empty
    answer: a cache that cannot be opened or verified must not look like a
    project without matches.
    """
    path = _catalog_cache_path(scope)
    if path is None:
        return None
    if catalog_trigram_plan(query) is None:
        return None
    status = cached_trigram_status(path)
    if not status.usable:
        return None
    try:
        connection = open_readonly_cache(path)
    except (sqlite3.Error, OSError, ValueError):
        return None
    return _iter_trigram_rows(connection, query, status.fts_table, cancel)


def _iter_trigram_rows(connection, query, fts_table, cancel):
    """Yield trigram-shortlisted catalog rows; the connection closes with them.

    A cancelled scan is reported as the scope layer's cancellation, so a
    superseded query is dropped instead of showing an error.
    """
    try:
        yield from iter_catalog_candidate_rows(connection, query, fts_table=fts_table,
                                               cancel=cancel)
    except SearchCancelled:
        raise OpenScopeCancelled('Catalog candidate scan cancelled') from None
    finally:
        connection.close()


def _catalog_asset_rows(scope, query, cancel=None):
    """All matching rows, bounded by page size rather than result count.

    Never split by a hard-coded asset-kind list: plugins may add more kinds,
    and a single canvas can contain arbitrarily more than 1024 matches.

    The catalog's trigram index shortlists the rows when the cache has one
    that verifies; otherwise the exact scan runs. Both paths return every
    matching row, and both apply the same predicate - the index only rejects
    rows, the exact word filter still decides. That is why the two paths
    answer the same question with the same rows.

    ``limit`` is the **page size** of the keyset walk, not a cap on the number
    of results: :meth:`ProjectOpenScope.iter_query` yields every matching row
    and stops on a short page. So ``limit=1024`` only decides how many rows
    one round trip carries; the result count is decided by the query.
    """
    trigram = _trigram_catalog_rows(scope, query, cancel)
    if trigram is not None:
        produced = 0
        try:
            for row in trigram:
                produced += 1
                yield row
            return
        except sqlite3.DatabaseError:
            # The index was replaced between the verification and the query.
            # Falling back is only honest while nothing was published yet.
            if produced:
                raise
    yield from scope.iter_query(text=query, limit=1024, cancel=cancel)


def search_catalog_assets(scope, scene, service, query, *, loaded_keys=(), cancel=None):
    """Catalog-backed asset candidates for a partially loaded project.

    Reads whole-project asset rows (including assets not yet in the scene)
    through a ready :class:`~prism.project_open_scope.ProjectOpenScope` and
    returns them in the same result shape :func:`search_project` produces for
    assets, minus the live ``item`` (they are not in the scene). Rows whose id
    is in ``loaded_keys`` are skipped so the in-memory fast path — which owns
    the session's dirty edits — always wins for loaded assets.

    ``query`` is the plain-text half of the search box (a ``tags.parse_query``
    expression is handled by the in-memory path and never reaches here).

    The catalog stores only title/notes/filename/tags, so this haystack does
    not include a page's resource-tree path, categories, source URL or body
    text that the in-memory fast path would. That is the documented catalog
    limitation, not a truncation: every row the catalog can match is returned
    and filtered/ranked with the exact same semantics as the fast path.
    """
    if scope is None or not getattr(scope, 'ready', False):
        return []
    words = query_words(query)
    if not words:
        return []
    system = tags.tag_system(scene)
    nodes = {n['id']: n for n in service.nodes if not n.get('deletedAt')}
    by_page = {n.get('pageId'): n for n in nodes.values() if n['nodeType'] == 'page'}
    deleted_pages = {n.get('pageId') for n in service.nodes
                     if n.get('deletedAt') and n.get('pageId')}

    def path_for(canvas_id):
        node = by_page.get(canvas_id)
        if node is None:
            return '画布'
        return node_path(node, nodes)

    results = []
    for row in _catalog_asset_rows(scope, query, cancel):
        item_id = str(row['id'])
        if item_id in loaded_keys:
            continue
        canvas_id = row.get('canvas_id') or 'default-canvas'
        if canvas_id in deleted_pages:
            continue
        tag_names = _parse_tags(row.get('tags'))
        title = str(row.get('title') or row.get('filename') or '素材')
        path = path_for(canvas_id)
        asset_text = ' '.join(filter(None, [
            title,
            str(row.get('filename') or ''),
            str(row.get('notes') or ''),
            ' '.join(tag_names),
        ]))
        catalog_text = '\n'.join(filter(None, [str(row.get('notes') or ''),
                                               ', '.join(tag_names)]))
        result = _build_result(
            'asset', title, [dict(field='asset', value=asset_text, item_id=item_id)],
            tag_names, system, words,
            item_id=item_id, result_id='asset:'+item_id,
            page_id=canvas_id, page_kind='canvas', path=path,
            catalog=True, rating=int(row.get('rating') or 0),
            catalog_text=catalog_text)
        if result is not None:
            results.append(result)
    needle = query.casefold().strip()
    return sorted(results, key=lambda result: rank_result(result, needle))


def text_of_tree(tree):
    if not isinstance(tree, dict):
        return ''
    data = tree.get('data', {})
    text = plain_text(data.get('text') or tree.get('title') or tree.get('topic') or tree.get('text') or '')
    return ' '.join([text] + [text_of_tree(child) for child in
                              (tree.get('children') or tree.get('nodes') or [])])
