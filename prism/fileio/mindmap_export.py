# This file is part of Prism.
#
# Prism is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Prism is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Prism.  If not, see <https://www.gnu.org/licenses/>.

"""Export a board's mind map to the usual exchange formats.

The tree comes from simple-mind-map as JSON, so everything here works on
that structure: ``{data: {text, note, tag, hyperlink}, children: [...]}``.
Rich-text nodes explicitly mark their HTML with ``data.richText``. Plain
node titles and notes remain plain text when exported.

XMind files are a ZIP holding ``content.json``; that structure is public and
is written here directly, without any XMind code.
"""

import hashlib
import io
import json
import logging
import os
import re
import time
import uuid
import zipfile
from html.parser import HTMLParser

from prism.fileio.attachments import attachment_for, extension_for
from prism.fileio.import_xmind import XMIND_UNITS_PER_PX
from prism.i18n import _

logger = logging.getLogger(__name__)


SUPPORTED_FORMATS = ('xmind', 'mm', 'json', 'md', 'png', 'pdf')

#: simple-mind-map layout -> XMind structureClass, so exporting an imported
#: map keeps the structure it was drawn with.
LAYOUT_STRUCTURES = {
    'mindMap': 'org.xmind.ui.map.unbalanced',
    'logicalStructure': 'org.xmind.ui.logic.right',
    'organizationStructure': 'org.xmind.ui.org-chart.down',
    'catalogOrganization': 'org.xmind.ui.tree.right',
    'timeline': 'org.xmind.ui.timeline.horizontal',
    'timeline2': 'org.xmind.ui.timeline.horizontal',
    'verticalTimeline': 'org.xmind.ui.timeline.vertical',
    'fishbone': 'org.xmind.ui.fishbone.leftHeaded',
    'rightFishbone': 'org.xmind.ui.fishbone.rightHeaded',
}

#: Keys that describe this editor's own state rather than the map.
_PRIVATE_KEYS = ('_prism_view',)

class _RichText(HTMLParser):
    """Plain text from explicitly rich editor content, with block boundaries."""
    BLOCKS = frozenset(('p', 'div', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'))

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def _newline(self):
        if self.parts and not self.parts[-1].endswith('\n'):
            self.parts.append('\n')

    def handle_starttag(self, tag, attrs):
        if tag == 'br':
            self.parts.append('\n')
        elif tag in self.BLOCKS:
            self._newline()

    def handle_endtag(self, tag):
        if tag in self.BLOCKS:
            self._newline()

    def handle_data(self, data):
        self.parts.append(data)


class MindMapExportError(Exception):
    """Raised when the map cannot be written."""


def node_text(node):
    """Keep plain node text verbatim; decode HTML only for richText nodes."""
    if not isinstance(node, dict):
        return ''
    data = node.get('data', {})
    if not isinstance(data, dict):
        return ''
    raw = str(data.get('text', '') or '')
    if data.get('richText') is not True:
        return raw
    parser = _RichText()
    parser.feed(raw)
    parser.close()
    return ''.join(parser.parts).replace('\xa0', ' ').strip('\n')


def node_note(node):
    """Optional note attached to a node."""
    if not isinstance(node, dict):
        return ''
    data = node.get('data', {})
    # The editor's editNote uses a plain multiline text area. HTML-looking
    # examples and line breaks belong to the note and must not be discarded.
    return str(data.get('note', '') or '') if isinstance(data, dict) else ''


def children_of(node):
    children = node.get('children') if isinstance(node, dict) else None
    return [child for child in children if isinstance(child, dict)] \
        if isinstance(children, list) else []


def count_nodes(tree):
    """Total number of nodes, including the root."""
    if not isinstance(tree, dict):
        return 0
    return 1 + sum(count_nodes(child) for child in children_of(tree))


def export_mindmap(tree, path, fmt=None, attachments=None, *, cancel=None):
    """Write ``tree`` to ``path`` in the format named by ``fmt`` or suffix.

    ``attachments`` is the board's picture table; pass it and an XMind
    export carries the node images with it.

    :returns: the path that was written
    :raises MindMapExportError: on an unknown format or a write failure
    """
    if not isinstance(tree, dict):
        raise MindMapExportError(_('There is no mind map to export yet.'))
    fmt = (fmt or os.path.splitext(path)[1].lstrip('.')).lower()
    if fmt not in SUPPORTED_FORMATS:
        raise MindMapExportError(
            _('Unsupported mind map format: {fmt}').format(fmt=fmt))

    try:
        if fmt == 'json':
            _write_json(tree, path)
        elif fmt == 'md':
            _write_markdown(tree, path)
        elif fmt == 'mm':
            _write_freemind(tree, path)
        elif fmt == 'xmind':
            _write_xmind(tree, path, attachments)
        elif fmt == 'pdf':
            from prism.fileio.mindmap_pdf import write_mindmap_pdf
            write_mindmap_pdf(tree, path, attachments, cancel=cancel)
        else:
            raise MindMapExportError(
                _('Rendering {fmt} needs the mind map window.').format(fmt=fmt))
    except OSError as exc:
        raise MindMapExportError(
            _('Could not write {path}: {reason}')
            .format(path=path, reason=exc)) from exc
    logger.info('Exported mind map to %s (%s)', path, fmt)
    return path


# ── Plain formats ────────────────────────────────────────────────


def _write_json(tree, path):
    document = {key: value for key, value in tree.items()
                if key not in _PRIVATE_KEYS}
    with io.open(path, 'w', encoding='utf-8') as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)


def _write_markdown(tree, path):
    lines = []
    _markdown_lines(tree, lines, depth=0)
    with io.open(path, 'w', encoding='utf-8') as handle:
        handle.write('\n'.join(lines).rstrip() + '\n')


def _markdown_lines(node, lines, depth):
    text = node_text(node)
    if depth == 0:
        lines.append(f'# {text}')
    else:
        lines.append('  ' * (depth - 1) + f'- {text}')
    note = node_note(node)
    if note:
        lines.append('  ' * depth + f'  > {note}')
    for child in children_of(node):
        _markdown_lines(child, lines, depth + 1)
    if depth == 0:
        lines.append('')


def _write_freemind(tree, path):
    """FreeMind .mm is a small XML dialect every mind map tool can read."""
    lines = ['<map version="1.0.1">']
    _freemind_lines(tree, lines, depth=1)
    lines.append('</map>')
    with io.open(path, 'w', encoding='utf-8') as handle:
        handle.write('\n'.join(lines) + '\n')


def _freemind_lines(node, lines, depth):
    indent = '  ' * depth
    text = _xml_escape(node_text(node))
    note = node_note(node)
    children = children_of(node)
    attributes = f'TEXT="{text}"'
    if not children and not note:
        lines.append(f'{indent}<node {attributes}/>')
        return
    lines.append(f'{indent}<node {attributes}>')
    if note:
        lines.append(f'{indent}  <richcontent TYPE="NOTE">'
                     f'<html><body><p>{_xml_escape(note)}</p></body></html>'
                     f'</richcontent>')
    for child in children:
        _freemind_lines(child, lines, depth + 1)
    lines.append(f'{indent}</node>')


def _xml_escape(value):
    return (value.replace('&', '&amp;')
                 .replace('<', '&lt;')
                 .replace('>', '&gt;')
                 .replace('"', '&quot;'))


# ── XMind ────────────────────────────────────────────────────────


def _write_xmind(tree, path, attachments=None):
    """Write a modern XMind file: a ZIP with content.json inside.

    The container layout is documented by the format itself and is built
    here from scratch; no XMind code or asset is involved.

    Node pictures are written into ``resources/`` the way XMind does it,
    named after their own content so a picture used by several nodes is
    stored once.
    """
    now = int(time.time() * 1000)
    resources = {}
    topic = _xmind_topic(tree, attachments, resources)
    structure = LAYOUT_STRUCTURES.get(
        tree.get('_prism_layout') if isinstance(tree, dict) else None)
    if structure:
        topic['structureClass'] = structure
    content = [{
        'id': str(uuid.uuid4()),
        'class': 'sheet',
        'title': node_text(tree) or _('Mind Map'),
        'rootTopic': topic,
    }]
    metadata = {'creator': {'name': 'Prism', 'version': '0.3.4'}}
    manifest = {'file-entries': {'content.json': {},
                                 'metadata.json': {},
                                 'manifest.json': {}}}
    for name in resources:
        manifest['file-entries'][name] = {}

    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('content.json', json.dumps(
            content, ensure_ascii=False, indent=1))
        archive.writestr('metadata.json', json.dumps(
            metadata, ensure_ascii=False))
        for name, blob in resources.items():
            archive.writestr(name, blob)
        archive.writestr('manifest.json', json.dumps(
            manifest, ensure_ascii=False))
    logger.debug('Wrote XMind sheet with timestamp %s and %s image(s)',
                 now, len(resources))


def _xmind_topic(node, attachments=None, resources=None):
    topic = {
        'id': str(uuid.uuid4()),
        'class': 'topic',
        'title': node_text(node),
    }
    note = node_note(node)
    if note:
        topic['notes'] = {'plain': {'content': note}}
    data = node.get('data', {}) if isinstance(node, dict) else {}
    data = data if isinstance(data, dict) else {}
    hyperlink = data.get('hyperlink')
    if isinstance(hyperlink, str) and hyperlink:
        topic['hyperlink'] = hyperlink
        if isinstance(data.get('hyperlinkTitle'), str) and data['hyperlinkTitle']:
            topic['hyperlinkTitle'] = data['hyperlinkTitle']
    if data.get('expand') is False:
        topic['collapsed'] = True
    tags = data.get('tag')
    if isinstance(tags, list) and tags:
        topic['labels'] = [str(tag) for tag in tags]
    image = _xmind_image(node, attachments, resources)
    if image:
        topic['image'] = image
    children = children_of(node)
    if children:
        topic['children'] = {
            'attached': [_xmind_topic(child, attachments, resources)
                         for child in children]}
    return topic


def _xmind_image(node, attachments, resources):
    """The XMind ``image`` block for a node, storing the blob once."""
    data = node.get('data') if isinstance(node, dict) else None
    image = data.get('image') if isinstance(data, dict) else None
    if not isinstance(image, str) or not image:
        return None
    entry = attachment_for(image, attachments)
    if entry is None:
        raise MindMapExportError(
            '节点“{title}”的图片数据缺失，未写出 XMind 文件。'.format(title=node_text(node)))
    if resources is None:
        resources = {}
    _name, mime, blob = entry
    digest = hashlib.sha1(blob).hexdigest()
    resource = f'resources/{digest}{extension_for(mime)}'
    resources.setdefault(resource, blob)
    block = {'src': f'xap:{resource}', 'align': 'right'}
    size = data.get('imageSize')
    if isinstance(size, dict):
        width = size.get('width')
        height = size.get('height')
        if width and height:
            # Back to XMind's own unit, which is what it wrote when the map
            # was imported in the first place.
            block['width'] = round(float(width) * XMIND_UNITS_PER_PX, 2)
            block['height'] = round(float(height) * XMIND_UNITS_PER_PX, 2)
    return block


def suggested_filename(tree, extension):
    """A file name derived from the root node, safe on Windows."""
    title = node_text(tree) or _('Mind Map')
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', title).strip(' .')
    safe = safe[:60] or _('Mind Map')
    return f'{safe}.{extension}'
