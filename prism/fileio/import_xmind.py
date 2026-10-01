"""Read .xmind files into a simple-mind-map tree.

`.xmind` is a ZIP archive holding `content.json` (modern) or `content.xml`
(XMind 8 and earlier).  No code or asset from XMind is used.

XMind stores far more per topic than its text: images, styles, markers,
hyperlinks, notes, labels and free positions.  Reading only ``title`` and
``children`` was why an imported map looked nothing like the original, so
everything above is carried over here.

Two conventions matter for the numbers XMind writes:

* lengths are in the same unit as its 60pt body text, which the canvas
  shows at roughly 20px, so geometry is divided by
  :data:`XMIND_UNITS_PER_PX` to keep the original text/image proportions;
* a style at :data:`XMIND_DEFAULT_FONT_PT` is XMind's default and is left
  to the theme instead of being pinned on every node.
"""
import base64
import json
import logging
import re
import zipfile
from xml.etree import ElementTree

from .import_base import ImportFileError

logger = logging.getLogger(__name__)


#: XMind's structureClass -> the layout name simple-mind-map understands.
#: Without this every imported sheet came out as the default layout.
XMIND_LAYOUTS = {
    'org.xmind.ui.map.unbalanced': 'mindMap',
    'org.xmind.ui.map.clockwise': 'mindMap',
    'org.xmind.ui.map.anticlockwise': 'mindMap',
    'org.xmind.ui.logic.right': 'logicalStructure',
    'org.xmind.ui.logic.left': 'logicalStructure',
    'org.xmind.ui.logic.down': 'logicalStructure',
    'org.xmind.ui.logic.up': 'logicalStructure',
    'org.xmind.ui.org-chart.down': 'organizationStructure',
    'org.xmind.ui.org-chart.up': 'organizationStructure',
    'org.xmind.ui.tree.right': 'catalogOrganization',
    'org.xmind.ui.tree.left': 'catalogOrganization',
    'org.xmind.ui.timeline.horizontal': 'timeline',
    'org.xmind.ui.timeline.vertical': 'timeline',
    'org.xmind.ui.fishbone.leftHeaded': 'fishbone',
    'org.xmind.ui.fishbone.rightHeaded': 'fishbone',
}

#: XMind geometry unit -> pixels.  XMind's 60pt body text renders at about
#: 20px on the canvas, and lengths share that unit, so everything is
#: divided by three to keep a node's picture the size it was in XMind.
XMIND_UNITS_PER_PX = 3.0

#: XMind's default body size; a style at this size is not pinned.
XMIND_DEFAULT_FONT_PT = 60.0

#: XMind marker ids -> simple-mind-map's built-in icon names (``type_name``,
#: see ``MindMap.iconList``).  Markers without a counterpart are dropped.
XMIND_MARKERS = {
    'priority-1': 'priority_1',
    'priority-2': 'priority_2',
    'priority-3': 'priority_3',
    'priority-4': 'priority_4',
    'priority-5': 'priority_5',
    'priority-6': 'priority_6',
    'priority-7': 'priority_7',
    'priority-8': 'priority_8',
    'priority-9': 'priority_9',
    'task-start': 'progress_2',
    'task-quarter': 'progress_3',
    'task-half': 'progress_4',
    'task-3quar': 'progress_6',
    'task-done': 'progress_8',
}

_MIME_TYPES = {
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.webp': 'image/webp',
    '.bmp': 'image/bmp',
    '.svg': 'image/svg+xml',
}

_COLOUR_RE = re.compile(r'#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$')
_NAMED_COLOUR_RE = re.compile(r'[a-zA-Z]{3,20}$')
_NUMBER_RE = re.compile(r'-?\d+(?:\.\d+)?')


def xmind_layout_name(structure_class):
    """Translate an XMind structureClass into a simple-mind-map layout."""
    if not structure_class:
        return None
    if structure_class in XMIND_LAYOUTS:
        return XMIND_LAYOUTS[structure_class]
    # Fall back to matching the tail of the identifier, so layouts from a
    # newer XMind still land somewhere sensible.
    tail = str(structure_class).rsplit('.', 1)[-1].lower()
    if 'fishbone' in tail:
        return 'fishbone'
    if 'timeline' in tail:
        return 'timeline'
    if 'org-chart' in tail or 'organi' in tail:
        return 'organizationStructure'
    if 'tree' in tail or 'catalog' in tail:
        return 'catalogOrganization'
    if 'logic' in tail:
        return 'logicalStructure'
    if 'map' in tail:
        return 'mindMap'
    return None


def xmind_to_tree(path):
    """Read an .xmind file into a simple-mind-map tree.

    Images come back inline as ``data:`` URLs; the panel moves them into
    the board's attachment table before anything is written to disk, so
    the stored tree only keeps a short ``prism://`` reference.

    Only the first sheet is read - simple-mind-map has one board per page,
    not XMind's several sheets.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if 'content.json' in names:
                tree = _tree_from_json_sheets(
                    json.loads(archive.read('content.json')), archive)
            elif 'content.xml' in names:
                tree = _tree_from_legacy_xml(archive.read('content.xml'),
                                             archive)
            else:
                raise ImportFileError(
                    '这个 .xmind 文件里既没有 content.json 也没有 content.xml，'
                    '可能不是 XMind 文件。')
    except zipfile.BadZipFile as error:
        raise ImportFileError('这个文件不是有效的 .xmind（它应该是一个压缩包）。') \
            from error
    except (KeyError, ValueError, TypeError) as error:
        # ValueError covers the ElementTree parse errors too, which are
        # already reported with a clearer message from the helper.
        if isinstance(error, ImportFileError):
            raise
        raise ImportFileError(f'读取 .xmind 内容失败：{error}') from error

    topic_count, image_count = _tree_stats(tree)
    logger.info('Imported %s topic(s) and %s image(s) from %s',
                topic_count, image_count, path)
    return tree


def _tree_from_json_sheets(sheets, archive):
    """Turn the sheet list of a modern .xmind into a single tree."""
    if not isinstance(sheets, list) or not sheets:
        raise ImportFileError('这个 .xmind 文件里没有任何画布。')
    first = sheets[0] if isinstance(sheets[0], dict) else {}
    root = first.get('rootTopic')
    if not isinstance(root, dict):
        raise ImportFileError('这个 .xmind 文件里找不到中心主题。')
    tree = _tree_from_topic(root, archive)
    # XMind keeps the sheet layout on the root topic; simple-mind-map reads
    # it back off the root when the tree is loaded, so an imported sheet
    # keeps the structure it had in XMind instead of the default one.
    layout = xmind_layout_name(root.get('structureClass'))
    if layout:
        tree['_prism_layout'] = layout
    return tree


def _tree_from_topic(topic, archive=None):
    """Convert one XMind topic (and its children) into a mind map node."""
    text = topic.get('title')
    if text is None:
        text = topic.get('plainText') or ''
    node = {'data': {'text': str(text)}}
    data = node['data']

    _apply_notes(data, topic.get('notes'))
    _apply_labels(data, topic.get('labels'))
    _apply_hyperlink(data, topic)
    _apply_markers(data, topic.get('markers'))
    _apply_style(data, _style_properties(topic))
    _apply_position(data, topic.get('position'))
    _apply_collapsed(data, topic)
    if archive is not None:
        _apply_image(data, topic.get('image'), archive)

    children = topic.get('children')
    attached = []
    if isinstance(children, dict):
        for key in ('attached', 'detached', 'summary'):
            group = children.get(key)
            if isinstance(group, list):
                attached.extend(child for child in group
                                if isinstance(child, dict))
    node['children'] = [_tree_from_topic(child, archive)
                        for child in attached]
    return node


# ── Per-topic details ────────────────────────────────────────────

def _apply_notes(data, notes):
    if not isinstance(notes, dict):
        return
    plain = notes.get('plain')
    if isinstance(plain, dict) and plain.get('content'):
        data['note'] = str(plain['content'])
    elif isinstance(notes.get('content'), str):
        data['note'] = notes['content']


def _apply_labels(data, labels):
    if isinstance(labels, list) and labels:
        data['tag'] = [str(label) for label in labels]


def _apply_hyperlink(data, topic):
    href = topic.get('hyperlink')
    if not isinstance(href, str) or not href:
        return
    data['hyperlink'] = href
    title = topic.get('hyperlinkTitle')
    if isinstance(title, str) and title:
        data['hyperlinkTitle'] = title


def _apply_markers(data, markers):
    if not isinstance(markers, list):
        return
    icons = []
    for marker in markers:
        if not isinstance(marker, dict):
            continue
        name = XMIND_MARKERS.get(str(marker.get('markerId')))
        if name and name not in icons:
            icons.append(name)
    if icons:
        data['icon'] = icons


def _style_properties(topic):
    style = topic.get('style')
    if not isinstance(style, dict):
        return {}
    properties = style.get('properties')
    return properties if isinstance(properties, dict) else {}


def _apply_style(data, properties):
    """Map the XMind style properties simple-mind-map understands.

    XMind writes everything as text (``"60pt"``, ``"500"``), and the two
    apps share most names, so unknown keys are simply skipped: an unknown
    key written into the node would be rendered as-is by nobody and would
    just bloat the board file.
    """
    for key, value in (properties or {}).items():
        if key == 'fo:font-size':
            size = _font_size_px(value)
            if size:
                data['fontSize'] = size
        elif key == 'fo:font-weight':
            weight = _number(value)
            if weight is not None and weight >= 600:
                data['fontWeight'] = 'bold'
        elif key == 'fo:font-style':
            if str(value).strip().lower().startswith('italic'):
                data['fontStyle'] = 'italic'
        elif key == 'fo:font-family':
            family = str(value).strip()
            if family:
                data['fontFamily'] = family
        elif key == 'fo:color':
            colour = _colour(value)
            if colour:
                data['color'] = colour
        elif key == 'svg:fill':
            colour = _colour(value)
            if colour:
                data['fillColor'] = colour
        elif key == 'border-line-color':
            colour = _colour(value)
            if colour:
                data['borderColor'] = colour
        elif key == 'line-color':
            colour = _colour(value)
            if colour:
                data['lineColor'] = colour
        elif key == 'shape-class':
            shape = _shape_name(value)
            if shape:
                data['shape'] = shape


#: XMind shape classes -> simple-mind-map's node shapes.
XMIND_SHAPES = {
    'org.xmind.ui.roundedRect': 'roundedRectangle',
    'org.xmind.ui.rect': 'rectangle',
    'org.xmind.ui.ellipse': 'ellipse',
    'org.xmind.ui.diamond': 'diamond',
    'org.xmind.ui.parallelogram': 'parallelogram',
    'org.xmind.ui.underlined': None,
}


def _shape_name(value):
    text = str(value or '').strip()
    if not text:
        return None
    if text in XMIND_SHAPES:
        return XMIND_SHAPES[text]
    tail = text.rsplit('.', 1)[-1].lower()
    return XMIND_SHAPES.get('org.xmind.ui.' + tail)


def _apply_collapsed(data, topic):
    """把 XMind 的折叠状态带过来。

    XMind 用 `collapsed: true` 表示这个主题是收起来的；simple-mind-map
    用的字段是 **`expand`**（布尔，反过来）。不映射的话，用户在 XMind 里
    收好的分支导入之后会全部展开 —— 任务书 T4 把这叫做「静默丢失」并
    明确禁止：

        对 XMind 的图片、样式、折叠状态和视图位置分别建立测试样例；
        不支持的特性必须明确提示，而不是静默丢失。

    只写 `False`，不写 `True` —— 没标 `collapsed` 的主题让 simple-mind-map
    用它的默认值，别去覆盖。
    """
    if topic.get('collapsed'):
        data['expand'] = False


def _apply_position(data, position):
    """Keep XMind's free position without applying it.

    The coordinates belong to XMind's own layout engine: feeding them to
    simple-mind-map as ``customLeft``/``customTop`` moves nodes to places
    the automatic layout never intended, which looks worse than laying
    the map out again.  The numbers are kept so nothing is lost.
    """
    if not isinstance(position, dict):
        return
    x = _length_px(position.get('x'))
    y = _length_px(position.get('y'))
    if x is None or y is None:
        return
    data['_prism_position'] = [x, y]


def _apply_image(data, image, archive):
    if not isinstance(image, dict):
        return
    src = image.get('src')
    if not isinstance(src, str) or not src:
        return
    blob = _read_resource(archive, src)
    if blob is None:
        # Unknown resource: keep the reference so the node still says it
        # had a picture instead of silently dropping it.
        data['image'] = src
        return
    encoded = base64.b64encode(blob).decode('ascii')
    data['image'] = f'data:{_mime_type(src)};base64,{encoded}'
    data['imageTitle'] = ''
    width = _length_px(image.get('width'))
    height = _length_px(image.get('height'))
    if not width or not height:
        # Most pictures inside an .xmind carry no size at all; the canvas
        # needs one (it destructures imageSize when it draws a node image),
        # so the picture's own pixels are measured instead.
        width, height = _thumbnail_size(blob)
    if width and height:
        # XMind's own size, so the node looks the way it did there.
        data['imageSize'] = {'width': width, 'height': height,
                             'custom': True}


#: Pictures XMind stores without a size are shown at most this large, which
#: is the scale XMind itself uses for a node thumbnail.
_THUMBNAIL_EDGE_UNITS = 200.0


def _thumbnail_size(blob):
    """Measure an image and scale it to a node-picture size, in pixels."""
    native = image_size(blob)
    if not native:
        return 150, 100
    width, height = native
    longest = max(width, height)
    if longest > _THUMBNAIL_EDGE_UNITS:
        factor = _THUMBNAIL_EDGE_UNITS / float(longest)
        width, height = width * factor, height * factor
    return max(1, int(round(width / XMIND_UNITS_PER_PX))), \
        max(1, int(round(height / XMIND_UNITS_PER_PX)))


def image_size(blob):
    """Read an image's pixel size from its header, or None.

    Only the common formats are handled and nothing is decoded: a board
    import should not depend on an imaging library for this.
    """
    if not blob or len(blob) < 24:
        return None
    if blob[:8] == b'\x89PNG\r\n\x1a\n':
        return (int.from_bytes(blob[16:20], 'big'),
                int.from_bytes(blob[20:24], 'big'))
    if blob[:6] in (b'GIF87a', b'GIF89a'):
        return (int.from_bytes(blob[6:8], 'little'),
                int.from_bytes(blob[8:10], 'little'))
    if blob[:2] == b'BM':
        return (int.from_bytes(blob[18:22], 'little', signed=True),
                int.from_bytes(blob[22:26], 'little', signed=True))
    if blob[:4] == b'RIFF' and blob[8:12] == b'WEBP':
        return _webp_size(blob)
    if blob[:2] == b'\xff\xd8':
        return _jpeg_size(blob)
    return None


def _webp_size(blob):
    kind = blob[12:16]
    if kind == b'VP8 ' and len(blob) >= 30:
        return (int.from_bytes(blob[26:28], 'little') & 0x3FFF,
                int.from_bytes(blob[28:30], 'little') & 0x3FFF)
    if kind == b'VP8L' and len(blob) >= 25:
        bits = int.from_bytes(blob[21:25], 'little')
        return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    if kind == b'VP8X' and len(blob) >= 30:
        width = int.from_bytes(blob[24:27], 'little') + 1
        height = int.from_bytes(blob[27:30], 'little') + 1
        return width, height
    return None


def _jpeg_size(blob):
    position = 2
    total = len(blob)
    while position + 9 < total:
        if blob[position] != 0xFF:
            position += 1
            continue
        marker = blob[position + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            position += 2
            continue
        length = int.from_bytes(blob[position + 2:position + 4], 'big')
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            return (int.from_bytes(blob[position + 7:position + 9], 'big'),
                    int.from_bytes(blob[position + 5:position + 7], 'big'))
        position += 2 + max(length, 2)
    return None


def _read_resource(archive, src):
    name = src[4:] if src.startswith('xap:') else src
    try:
        return archive.read(name)
    except KeyError:
        logger.debug('XMind resource %s is missing from the archive', src)
        return None


def _mime_type(src):
    lowered = src.lower()
    for extension, mime in _MIME_TYPES.items():
        if lowered.endswith(extension):
            return mime
    return 'image/png'


# ── Numbers ──────────────────────────────────────────────────────

def _number(value):
    match = _NUMBER_RE.search(str(value))
    return float(match.group()) if match else None


def _font_size_px(value):
    """XMind font size -> pixels, or None to keep the theme's own size."""
    text = str(value).strip().lower()
    unit = 'pt'
    if text.endswith('pt'):
        text = text[:-2]
    elif text.endswith('px'):
        unit = 'px'
        text = text[:-2]
    size = _number(text)
    if size is None or size <= 0:
        return None
    if unit == 'pt':
        if abs(size - XMIND_DEFAULT_FONT_PT) < 0.5:
            return None
        size /= XMIND_UNITS_PER_PX
    size = int(round(size))
    return size if 9 <= size <= 96 else None


def _length_px(value):
    """XMind length -> the pixel size simple-mind-map lays out with."""
    size = _number(value)
    if size is None:
        return None
    return int(round(size / XMIND_UNITS_PER_PX))


def _colour(value):
    text = str(value or '').strip()
    if not text or text.lower() in ('none', 'transparent'):
        return None
    if _COLOUR_RE.match(text) or _NAMED_COLOUR_RE.match(text):
        return text
    return None


def _tree_stats(tree):
    total = 0
    with_image = 0
    stack = [tree]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        total += 1
        data = node.get('data')
        if isinstance(data, dict) and data.get('image'):
            with_image += 1
        children = node.get('children')
        if isinstance(children, list):
            stack.extend(children)
    return total, with_image


# ── XMind 8 and earlier ──────────────────────────────────────────

def _tree_from_legacy_xml(data, archive=None):
    """Read the content.xml used by XMind 8 and earlier."""
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as error:
        raise ImportFileError(f'解析 XMind content.xml 失败：{error}') from error

    topic = root.find('.//{*}sheet/{*}topic')
    if topic is None:
        topic = root.find('.//{*}topic')
    if topic is None:
        raise ImportFileError('这个 .xmind 文件里找不到中心主题。')
    return _tree_from_legacy_topic(topic, archive)


def _tree_from_legacy_topic(element, archive=None):
    title = element.findtext('{*}title') or ''
    node = {'data': {'text': title}}

    notes = element.findtext('.//{*}notes/{*}plain')
    if notes:
        node['data']['note'] = notes

    labels = [child.text for child in element.findall('{*}labels/{*}label')
              if child.text]
    if labels:
        node['data']['tag'] = labels

    hyperlink = element.find('{*}hyperlink')
    if hyperlink is not None:
        href = hyperlink.get('{http://www.w3.org/1999/xlink}href')
        if href:
            node['data']['hyperlink'] = href

    if archive is not None:
        image = element.find('{*}image')
        if image is not None:
            src = image.get('{http://www.w3.org/1999/xlink}src')
            _apply_image(node['data'], {'src': src}, archive)

    children = []
    for child in element.findall('{*}children/{*}topics/{*}topic'):
        children.append(_tree_from_legacy_topic(child, archive))
    node['children'] = children
    return node
