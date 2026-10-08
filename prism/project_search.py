"""Project-wide search snapshots, separate from canvas visibility filters."""
from html.parser import HTMLParser
from prism import tags


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


def search_project(scene, service, query, panels=(), tag_expression='', page_records=None):
    """Return stable navigation targets; no scene/item visibility mutation."""
    system = tags.tag_system(scene)
    words = query.casefold().split()
    predicate = tags.parse_query(tag_expression) if tag_expression else None
    nodes = {n['id']: n for n in service.nodes if not n.get('deletedAt')}
    by_page = {n.get('pageId'): n for n in nodes.values() if n['nodeType'] == 'page'}
    deleted_pages = {n.get('pageId') for n in service.nodes
                     if n.get('deletedAt') and n.get('pageId')}
    pages = {p['id']: p for p in (page_records if page_records is not None else getattr(scene, 'workspace_pages', []))}
    live = {getattr(p, 'page_id', None) or ('default-document' if hasattr(p, '_html') else 'default-mindmap'): p for p in panels}
    results = []

    def path(node):
        parts, seen = [], set()
        while node and node['id'] not in seen:
            seen.add(node['id'])
            parts.append(node['title'])
            node = nodes.get(node.get('parentId'))
        return ' / '.join(reversed(parts))

    def add(kind, title, text='', tag_names=(), **target):
        tag_index = tags.name_index(tag_names, system)
        haystack = ' '.join([title, target.get('path', ''), text, *tag_index]).casefold()
        if predicate is not None:
            if not predicate.matches(tag_index):
                return
        elif not words or not all(word in haystack for word in words):
            return
        snippet = ' '.join(str(text).split())
        position = next((snippet.casefold().find(w) for w in words
                         if w in snippet.casefold()), 0)
        start = max(0, position - 24)
        results.append(dict(kind=kind, title=title, snippet=snippet[start:start + 100],
                            **target))

    for node in nodes.values():
        if node['nodeType'] == 'folder':
            add('folder', node['title'], node_id=node['id'], path=path(node))
    for name in sorted(tags.scene_tag_names(scene)):
        add('tag', name, tag_names=[name], tag=name, path='标签')
    for page_id, page in pages.items():
        node = by_page.get(page_id)
        if not node:
            continue
        kind = page['kind']
        common = dict(page_id=page_id, page_kind=kind, path=path(node))
        names = page.get('tags', [])
        add(kind, page['title'], tag_names=names, **common)
        panel = live.get(page_id)
        content = page.get('content')
        if kind == 'document':
            live_content = getattr(panel, '_html', None)
            content = live_content if live_content is not None else content
            if content is None and page_id == 'default-document':
                content = getattr(scene, 'note_html', '')
            text = plain_text(content)
            add('document_text', page['title'], text, names,
                match_text=query, **common)
        elif kind == 'mindmap':
            live_content = getattr(panel, '_tree', None)
            content = live_content if live_content is not None else content
            if content is None and page_id == 'default-mindmap':
                content = getattr(scene, 'mindmap_tree', None)
            def walk(tree, indices=()):
                if not isinstance(tree, dict):
                    return
                data = tree.get('data', {})
                text = plain_text(data.get('text') or tree.get('title') or tree.get('topic') or tree.get('text') or '')
                extra = plain_text(data.get('note', '')) + ' ' + str(data.get('hyperlink', ''))
                add('mindmap_node', text, extra, list(names) + list(data.get('tag', []) or []),
                    node_path=list(indices), node_uid=data.get('uid'), **common)
                for index, child in enumerate(tree.get('children', []) or []):
                    walk(child, indices + (index,))
            walk(content)
    for item in scene.items_for_save():
        canvas_id = getattr(item, '_canvas_id', None)
        if canvas_id in deleted_pages:
            continue
        node = by_page.get(canvas_id)
        title = str(getattr(item, '_title', '') or getattr(item, 'filename', '') or
                    getattr(item, 'toPlainText', lambda: '素材')())
        text = ' '.join([str(getattr(item, '_notes', '') or ''),
                         str(getattr(item, 'filename', '') or ''),
                         ' '.join(getattr(item, '_categories', []) or []),
                         str(getattr(item, 'toPlainText', lambda: '')())])
        add('asset', title, text, getattr(item, '_tags', []), item=item,
            page_id=canvas_id, page_kind='canvas', path=path(node) if node else '画布')
    return results


def text_of_tree(tree):
    if not isinstance(tree, dict):
        return ''
    data = tree.get('data', {})
    text = plain_text(data.get('text') or tree.get('title') or tree.get('topic') or tree.get('text') or '')
    return ' '.join([text] + [text_of_tree(child) for child in
                              (tree.get('children') or tree.get('nodes') or [])])
