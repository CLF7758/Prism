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
Node text arrives as HTML (the editor supports rich text), so it is run
through :func:`node_text` before it lands in a plain-text format.

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

from prism.fileio.attachments import attachment_for, extension_for
from prism.fileio.import_xmind import XMIND_UNITS_PER_PX
from prism.i18n import _

logger = logging.getLogger(__name__)


SUPPORTED_FORMATS = ('xmind', 'mm', 'json', 'md', 'png')

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

_TAG_RE = re.compile(r'<[^>]+>')
_BLOCK_TAG_RE = re.compile(r'</(p|div|h[1-6]|li)>|<br\s*/?>', re.IGNORECASE)


class MindMapExportError(Exception):
    """Raised when the map cannot be written."""


def node_text(node):
    """Plain text of a node, with the HTML stripped and entities resolved."""
    if not isinstance(node, dict):
        return ''
    raw = node.get('data', {}).get('text', '') or ''
    # Block boundaries become real line breaks, then tags are removed.
    text = _BLOCK_TAG_RE.sub('\n', str(raw))
    text = _TAG_RE.sub('', text)
    text = (text.replace('&nbsp;', ' ')
                .replace('&lt;', '<')
                .replace('&gt;', '>')
                .replace('&quot;', '"')
                .replace('&#39;', "'")
                .replace('&amp;', '&'))
    return ' '.join(text.split())


def node_note(node):
    """Optional note attached to a node."""
    if not isinstance(node, dict):
        return ''
    raw = node.get('data', {}).get('note', '') or ''
    return ' '.join(_TAG_RE.sub('', str(raw)).split())


def children_of(node):
    children = node.get('children') if isinstance(node, dict) else None
    return [child for child in children if isinstance(child, dict)] \
        if isinstance(children, list) else []


def count_nodes(tree):
    """Total number of nodes, including the root."""
    if not isinstance(tree, dict):
        return 0
    return 1 + sum(count_nodes(child) for child in children_of(tree))


def export_mindmap(tree, path, fmt=None, attachments=None):
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
    tags = node.get('data', {}).get('tag') if isinstance(node, dict) else None
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
        logger.debug('Skipping picture %s with no data behind it', image)
        return None
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
