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

"""Pictures that live inside a board file.

A board keeps its images in one attachment table (``scene.attachments``,
written to the ``attachments`` table of the .prism file) and refers to them
by the short URL ``prism://attach/N``.  Everything that stores or reads such
a reference goes through here, so the editors and the exporters cannot
disagree about the format.

Nothing in this module needs Qt: the importers and the exporters both use
it.
"""
import base64
import binascii

#: The URL every part of Prism recognises for a stored picture.
ATTACHMENT_SCHEME = 'prism'
ATTACHMENT_HOST = 'attach'
ATTACHMENT_PREFIX = f'{ATTACHMENT_SCHEME}://{ATTACHMENT_HOST}/'

#: Pictures are shrunk to this longest edge before they are stored.
MAX_ATTACHMENT_EDGE = 2000

#: What a picture blob is called, by mime type.
MIME_EXTENSIONS = {
    'image/png': '.png',
    'image/jpeg': '.jpg',
    'image/gif': '.gif',
    'image/webp': '.webp',
    'image/bmp': '.bmp',
    'image/svg+xml': '.svg',
}


def attachment_url(attachment_id):
    """The URL a node uses to point at a stored picture."""
    return f'{ATTACHMENT_PREFIX}{attachment_id}'


def attachment_id_of(url):
    """The reverse of :func:`attachment_url`, or None."""
    if not isinstance(url, str) or not url.startswith(ATTACHMENT_PREFIX):
        return None
    tail = url[len(ATTACHMENT_PREFIX):]
    return int(tail) if tail.isdigit() else None


def decode_data_url(url):
    """Split a ``data:`` URL into ``(mime, bytes)``."""
    if not isinstance(url, str) or not url.startswith('data:'):
        return None
    head, separator, payload = url.partition(',')
    if not separator or ';base64' not in head:
        return None
    mime = head[len('data:'):].split(';')[0] or 'image/png'
    try:
        return mime, base64.b64decode(payload)
    except (binascii.Error, ValueError):
        return None


def encode_data_url(mime, blob):
    """Build a ``data:`` URL for an image blob."""
    return f'data:{mime};base64,{base64.b64encode(blob).decode("ascii")}'


def extension_for(mime):
    return MIME_EXTENSIONS.get((mime or '').lower(), '.png')


def node_data(tree):
    """Yield the ``data`` dict of every node in a mind map tree."""
    stack = [tree]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        data = node.get('data')
        if isinstance(data, dict):
            yield data
        children = node.get('children')
        if isinstance(children, list):
            stack.extend(children)


def attachment_for(image, attachments):
    """The ``(name, mime, blob)`` a node's picture refers to, or None."""
    attachment_id = attachment_id_of(image)
    if attachment_id is None:
        decoded = decode_data_url(image)
        if decoded is None:
            return None
        mime, blob = decoded
        return f'image{extension_for(mime)}', mime, blob
    entry = (attachments or {}).get(attachment_id)
    if not entry or len(entry) != 3:
        return None
    name, mime, blob = entry
    return name, mime or 'image/png', blob
