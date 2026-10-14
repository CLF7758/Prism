"""Project-wide search snapshots, separate from canvas visibility filters."""
from html.parser import HTMLParser
from prism import tags
import hashlib


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
    return [i for i in scene.items() if hasattr(i, 'save_id') or getattr(i, '_asset_source', None)]


def item_key(item):
    return str(getattr(item, 'save_id', None) or getattr(item, 'original_save_id', None) or id(item))


def search_project(scene, service, query, panels=(), tag_expression='', page_records=None, allow_empty=False):
    """Read-only query: one hit per page, with concrete content navigation targets."""
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
        parts, seen = [], set()
        while node and node['id'] not in seen:
            seen.add(node['id'])
            parts.append(node['title'])
            node = nodes.get(node.get('parentId'))
        return ' / '.join(reversed(parts))

    def add(kind, title, fields=(), tag_names=(), **target):
        index = tags.name_index(tag_names, system)
        haystack = ' '.join([title, target.get('path', ''), *index,
                             *(f['value'] for f in fields)]).casefold()
        if predicate is not None:
            if not predicate.matches(index):
                return
        elif words and not all(word in haystack for word in words):
            return
        elif not words and not allow_empty:
            return
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
        snippet = next((f['value'] for f in fields if any(w in f['value'].casefold() for w in words)), '')
        first = matches[0] if matches else {}
        position = first.get('position', 0)
        result = dict(kind=kind, title=title, snippet=snippet[max(0, position-24):position+100],
                      matches=matches, tags=list(tag_names),
                      version=hashlib.sha256(haystack.encode('utf-8')).hexdigest(), **target)
        body_matches = [m for m in matches if m['field'] == 'body']
        node_matches = [m for m in matches if m['field'] == 'node']
        if kind == 'document' and body_matches:
            result.update(kind='document_text', match_text=body_matches[0]['text'])
        elif kind == 'mindmap' and node_matches:
            result.update(kind='mindmap_node', node_path=node_matches[0]['node_path'], node_uid=node_matches[0]['node_uid'])
        results.append(result)

    items = [i for i in searchable_items(scene) if getattr(i, '_deleted_at', None) is None
             and getattr(i, '_canvas_id', None) not in deleted_pages]
    canvas_items = {}
    for item in items:
        canvas_items.setdefault(getattr(item, '_canvas_id', None) or 'default-canvas', []).append(item)
    for node in nodes.values():
        if node['nodeType'] == 'folder':
            add('folder', node['title'], node_id=node['id'], path=path(node), result_id='folder:'+node['id'])
    for name in sorted(tags.scene_tag_names(scene)):
        add('tag', name, tag_names=[name], tag=name, path='标签', result_id='tag:'+name)
    for page in pages:
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
    for item in items:
        if getattr(item, 'TYPE', '') in ('text', 'path'):
            continue
        canvas_id = getattr(item, '_canvas_id', None)
        node = by_page.get(canvas_id)
        title = str(getattr(item, '_title', '') or getattr(item, 'filename', '') or
                    getattr(item, 'toPlainText', lambda: '素材')())
        add('asset', title, [dict(field='asset', value=asset_search_text(item), item_id=item_key(item))],
            getattr(item, '_tags', []), item=item, item_id=item_key(item), result_id='asset:'+item_key(item),
            page_id=canvas_id, page_kind='canvas', path=path(node) if node else '画布')
    needle = query.casefold().strip()
    def rank(result):
        title = result['title'].casefold()
        fields = {m['field'] for m in result['matches']}
        score = 0 if title == needle else 1 if needle and needle in title else 2 if fields & {'body', 'node', 'canvas_item'} else 3
        return score, title, result['result_id']
    return sorted(results, key=rank)


def text_of_tree(tree):
    if not isinstance(tree, dict):
        return ''
    data = tree.get('data', {})
    text = plain_text(data.get('text') or tree.get('title') or tree.get('topic') or tree.get('text') or '')
    return ' '.join([text] + [text_of_tree(child) for child in
                              (tree.get('children') or tree.get('nodes') or [])])
