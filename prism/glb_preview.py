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

"""GLB/glTF 3D model preview.

The GLB container (12 byte header, JSON chunk, BIN chunk) and the JSON
``.gltf`` variant are parsed with the standard library only - no third party
3D library is involved. Only triangle meshes are extracted (positions and
indices), which is all the preview needs.

Rendering has two paths:

* :func:`render_mesh` rasterises the mesh with QPainter. Scene items,
  thumbnails and exports use it, so a model stays visible even when no
  OpenGL context can be created (headless runs, remote sessions, missing
  drivers).
* :class:`GLBPreviewSurface` hosts an interactive canvas for the preview
  window. It uses PyQt6's QtOpenGLWidgets (``QOpenGLWidget``) whenever an
  OpenGL context can be created and falls back to a plain QPainter canvas
  otherwise.
"""

import base64
import json
import logging
import math
import os
import struct
import urllib.parse

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt

try:
    from PyQt6 import QtOpenGL, QtOpenGLWidgets
    _HAVE_OPENGL_MODULES = True
except ImportError:  # pragma: no cover - depends on the PyQt6 build
    QtOpenGL = None
    QtOpenGLWidgets = None
    _HAVE_OPENGL_MODULES = False

from prism.i18n import _


logger = logging.getLogger(__name__)


GLB_MAGIC = 0x46546C67          # 'glTF'
JSON_CHUNK_TYPE = 0x4E4F534A    # 'JSON'
BIN_CHUNK_TYPE = 0x004E4942     # 'BIN\0'

GLB_EXTENSIONS = {'.glb', '.gltf'}

#: Files larger than this cannot be previewed (keeps parsing bounded).
MAX_FILE_BYTES = 256 * 1024 * 1024
#: Guard against absurd accessor sizes (3 million floats = 1M vertices).
MAX_ACCESSOR_VALUES = 3 * 1024 * 1024
#: Guard against absurd index counts.
MAX_TRIANGLES = 600_000

TRIANGLES_MODE = 4
_COMPONENT_TYPES = {
    5120: ('b', 1), 5121: ('B', 1), 5122: ('h', 2),
    5123: ('H', 2), 5125: ('I', 4), 5126: ('f', 4),
}
_TYPE_NAMES = {1: 'SCALAR', 2: 'VEC2', 3: 'VEC3', 4: 'VEC4'}

DEFAULT_YAW = -35.0
DEFAULT_PITCH = 22.0
DEFAULT_ZOOM = 1.0
DEFAULT_BACKGROUND = (0.13, 0.14, 0.16)

#: Triangle budget for the full quality preview.
FULL_QUALITY_TRIANGLES = 20000
#: Triangle budget while the camera is moving.
FAST_QUALITY_TRIANGLES = 2500

_LIGHT_DIRECTION = (0.35, 0.5, 0.8)
_BASE_COLOR = (0.63, 0.68, 0.76)

_opengl_available = None


def _clamp(value, low, high):
    return max(low, min(high, value))


class GLBError(Exception):
    """Raised when a GLB/glTF model cannot be read or parsed."""


def is_glb_file(path):
    """Whether the path points to a GLB/glTF model."""

    return os.path.splitext(str(path))[1].lower() in GLB_EXTENSIONS


class Mesh:
    """Triangle geometry extracted from a glTF/GLB document."""

    __slots__ = ('positions', 'indices', '_bounds')

    def __init__(self, positions, indices):
        #: Flat [x, y, z, ...] vertex data.
        self.positions = positions
        #: Flat triangle index data.
        self.indices = indices
        self._bounds = None

    def __repr__(self):
        return (f'Mesh({self.vertex_count} vertices, '
                f'{self.triangle_count} triangles)')

    @property
    def vertex_count(self):
        return len(self.positions) // 3

    @property
    def triangle_count(self):
        return len(self.indices) // 3

    def bounds(self):
        """Returns (min_x, min_y, min_z, max_x, max_y, max_z)."""

        if self._bounds is None:
            values = self.positions
            iterator = iter(values)
            min_x = min_y = min_z = float('inf')
            max_x = max_y = max_z = float('-inf')
            for x, y, z in zip(iterator, iterator, iterator):
                if x < min_x:
                    min_x = x
                if y < min_y:
                    min_y = y
                if z < min_z:
                    min_z = z
                if x > max_x:
                    max_x = x
                if y > max_y:
                    max_y = y
                if z > max_z:
                    max_z = z
            self._bounds = (min_x, min_y, min_z, max_x, max_y, max_z)
        return self._bounds

    def center(self):
        """Returns the center of the bounding box."""

        min_x, min_y, min_z, max_x, max_y, max_z = self.bounds()
        return ((min_x + max_x) / 2.0,
                (min_y + max_y) / 2.0,
                (min_z + max_z) / 2.0)

    def radius(self):
        """Returns the radius of the bounding sphere (never zero)."""

        min_x, min_y, min_z, max_x, max_y, max_z = self.bounds()
        dx = (max_x - min_x) / 2.0
        dy = (max_y - min_y) / 2.0
        dz = (max_z - min_z) / 2.0
        radius = math.sqrt(dx * dx + dy * dy + dz * dz)
        return radius if radius > 1e-9 else 1.0


# ── Parsing ──────────────────────────────────────────────────────────


def _decode_data_uri(uri):
    header, _, payload = uri.partition(',')
    if ';base64' in header:
        try:
            return base64.b64decode(payload)
        except (ValueError, TypeError) as exc:
            raise GLBError(f'invalid base64 buffer: {exc}') from exc
    try:
        return urllib.parse.unquote_to_bytes(payload)
    except (TypeError, ValueError) as exc:
        raise GLBError(f'invalid data uri: {exc}') from exc


def _read_buffers(document, binary, base_dir):
    """Resolve all buffers of a glTF document to bytes."""

    specs = document.get('buffers')
    if not isinstance(specs, list) or not specs:
        raise GLBError('no buffers in document')
    buffers = []
    for spec in specs:
        if not isinstance(spec, dict):
            raise GLBError('invalid buffer entry')
        uri = spec.get('uri')
        if not uri:
            buffers.append(binary)
        elif uri.startswith('data:'):
            buffers.append(_decode_data_uri(uri))
        elif '://' in uri:
            raise GLBError('remote buffers are not supported')
        elif base_dir:
            path = os.path.join(base_dir, uri.replace('\\', os.sep))
            try:
                with open(path, 'rb') as handle:
                    buffers.append(handle.read())
            except OSError as exc:
                raise GLBError(f'external buffer not found: {uri}') from exc
        else:
            raise GLBError(f'external buffer not found: {uri}')
    return buffers


def _buffer_view(document, buffers, view_index):
    views = document.get('bufferViews')
    if not isinstance(views, list) or not (0 <= view_index < len(views)):
        raise GLBError('invalid bufferView index')
    view = views[view_index]
    if not isinstance(view, dict):
        raise GLBError('invalid bufferView')
    buffer_index = int(view.get('buffer', 0) or 0)
    if not (0 <= buffer_index < len(buffers)):
        raise GLBError('invalid buffer index')
    data = buffers[buffer_index]
    offset = int(view.get('byteOffset', 0) or 0)
    length = view.get('byteLength')
    length = len(data) - offset if length is None else int(length)
    if offset < 0 or length < 0 or offset + length > len(data):
        raise GLBError('bufferView outside of buffer')
    return data, offset, length, view


def _read_accessor(document, buffers, accessor_index, components):
    """Read an accessor into a flat list of numbers."""

    accessors = document.get('accessors')
    if (not isinstance(accessors, list)
            or not (0 <= accessor_index < len(accessors))):
        raise GLBError('invalid accessor index')
    accessor = accessors[accessor_index]
    if not isinstance(accessor, dict):
        raise GLBError('invalid accessor')
    expected_type = _TYPE_NAMES.get(components)
    if accessor.get('type') != expected_type:
        raise GLBError(f'expected a {expected_type} accessor')
    component_type = accessor.get('componentType')
    if component_type not in _COMPONENT_TYPES:
        raise GLBError(f'unsupported component type: {component_type}')
    if components == 3 and component_type != 5126:
        raise GLBError('vertex positions must be floats')
    format_char, value_size = _COMPONENT_TYPES[component_type]
    count = int(accessor.get('count', 0) or 0)
    if count <= 0:
        raise GLBError('empty accessor')
    if count * components > MAX_ACCESSOR_VALUES:
        raise GLBError('model too large to preview')
    view_index = accessor.get('bufferView')
    if view_index is None:
        raise GLBError('accessor without bufferView')
    data, view_offset, view_length, view = _buffer_view(
        document, buffers, int(view_index))
    item_size = components * value_size
    stride = int(view.get('byteStride') or item_size)
    if stride < item_size:
        raise GLBError('invalid accessor stride')
    offset = view_offset + int(accessor.get('byteOffset', 0) or 0)
    if offset + (count - 1) * stride + item_size > view_offset + view_length:
        raise GLBError('accessor outside of bufferView')
    unpack = struct.Struct('<' + format_char * components).unpack_from
    values = []
    extend = values.extend
    for i in range(count):
        extend(unpack(data, offset + i * stride))
    return values


def parse_gltf_document(document, binary=b'', base_dir=None):
    """Extract triangle geometry from a decoded glTF document."""

    if not isinstance(document, dict):
        raise GLBError('invalid glTF document')
    buffers = _read_buffers(document, binary, base_dir)
    meshes = document.get('meshes')
    if not isinstance(meshes, list) or not meshes:
        raise GLBError('no meshes in document')
    positions = []
    indices = []
    for mesh in meshes:
        if not isinstance(mesh, dict):
            continue
        primitives = mesh.get('primitives')
        if not isinstance(primitives, list):
            continue
        for primitive in primitives:
            if not isinstance(primitive, dict):
                continue
            # mode 0 (POINTS) is falsy, so it must not be folded into the
            # default with ``or``: that would silently misread point clouds
            # as triangle soups and draw nonsense.
            mode = primitive.get('mode')
            mode = TRIANGLES_MODE if mode is None else int(mode)
            if mode != TRIANGLES_MODE:
                # Only triangles can be previewed; skip points and lines.
                continue
            attributes = primitive.get('attributes') or {}
            position_index = attributes.get('POSITION')
            if position_index is None:
                continue
            vertex_data = _read_accessor(document, buffers,
                                         int(position_index), 3)
            count = len(vertex_data) // 3
            if not count:
                continue
            base = len(positions) // 3
            positions.extend(vertex_data)
            index_accessor = primitive.get('indices')
            if index_accessor is None:
                indices.extend(range(base, base + count))
            else:
                for value in _read_accessor(document, buffers,
                                            int(index_accessor), 1):
                    value = int(value)
                    # Out of range indices would crash the renderer;
                    # clamp them to a valid vertex instead.
                    indices.append(base + value if 0 <= value < count else base)
            if len(indices) // 3 > MAX_TRIANGLES:
                raise GLBError('model too large to preview')
    if not positions or not indices:
        raise GLBError('no triangle geometry found')
    return Mesh(positions, indices)


def _decode_json_payload(payload):
    try:
        return json.loads(payload.rstrip(b'\x00 ').decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as exc:
        raise GLBError(f'invalid JSON chunk: {exc}') from exc


def parse_glb(data):
    """Parse a binary GLB container into a :class:`Mesh`."""

    if not isinstance(data, (bytes, bytearray)) or len(data) < 12:
        raise GLBError('file too short for a GLB header')
    data = bytes(data)
    magic, version, declared_length = struct.unpack_from('<III', data, 0)
    if magic != GLB_MAGIC:
        raise GLBError('not a binary glTF file')
    if version != 2:
        raise GLBError(f'unsupported GLB version: {version}')
    limit = len(data)
    if 12 <= declared_length <= limit:
        limit = declared_length
    offset = 12
    document = None
    binary = b''
    while offset + 8 <= limit:
        chunk_length, chunk_type = struct.unpack_from('<II', data, offset)
        offset += 8
        if chunk_length > limit - offset:
            raise GLBError('truncated GLB chunk')
        payload = data[offset:offset + chunk_length]
        offset += chunk_length
        if chunk_type == JSON_CHUNK_TYPE and document is None:
            document = _decode_json_payload(payload)
        elif chunk_type == BIN_CHUNK_TYPE and not binary:
            binary = payload
    if document is None:
        raise GLBError('GLB file has no JSON chunk')
    return parse_gltf_document(document, binary)


def parse_gltf(data, base_dir=None):
    """Parse a JSON ``.gltf`` document into a :class:`Mesh`."""

    if isinstance(data, (bytes, bytearray)):
        try:
            document = json.loads(bytes(data).decode('utf-8'))
        except (UnicodeDecodeError, ValueError) as exc:
            raise GLBError(f'invalid glTF JSON: {exc}') from exc
    else:
        document = data
    return parse_gltf_document(document, b'', base_dir)


def load_mesh(path, max_bytes=MAX_FILE_BYTES):
    """Load a GLB/glTF file from disk. Raises :class:`GLBError`."""

    path = os.fspath(path)
    extension = os.path.splitext(path)[1].lower()
    if extension not in GLB_EXTENSIONS:
        raise GLBError('not a GLB/glTF file')
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise GLBError(f'cannot read file: {exc.strerror or exc}') from exc
    if size > max_bytes:
        raise GLBError('model file is too large to preview')
    try:
        with open(path, 'rb') as handle:
            data = handle.read()
    except OSError as exc:
        raise GLBError(f'cannot read file: {exc.strerror or exc}') from exc
    if extension == '.glb':
        return parse_glb(data)
    return parse_gltf(data, os.path.dirname(os.path.abspath(path)))


# ── Software rendering ───────────────────────────────────────────────


def _camera_matrix(yaw, pitch):
    """Rotation matrix (row major, 3x3) of the orbit camera."""

    yaw = math.radians(yaw)
    pitch = math.radians(pitch)
    cos_yaw, sin_yaw = math.cos(yaw), math.sin(yaw)
    cos_pitch, sin_pitch = math.cos(pitch), math.sin(pitch)
    return (
        cos_yaw, 0.0, sin_yaw,
        sin_pitch * sin_yaw, cos_pitch, -sin_pitch * cos_yaw,
        -cos_pitch * sin_yaw, sin_pitch, cos_pitch * cos_yaw,
    )


def _paint_triangles(painter, mesh, width, height, yaw, pitch, zoom,
                     max_triangles, antialiasing=True):
    positions = mesh.positions
    indices = mesh.indices
    vertex_count = len(positions) // 3
    triangle_count = len(indices) // 3
    if not vertex_count or not triangle_count:
        return

    matrix = _camera_matrix(yaw, pitch)
    center_x, center_y, center_z = mesh.center()
    scale = 0.42 * min(width, height) * zoom / mesh.radius()
    half_width = width / 2.0
    half_height = height / 2.0

    # Project every vertex once; the triangles reference them by index.
    m0, m1, m2, m3, m4, m5, m6, m7, m8 = matrix
    projected = [None] * vertex_count
    for index in range(vertex_count):
        x = positions[index * 3] - center_x
        y = positions[index * 3 + 1] - center_y
        z = positions[index * 3 + 2] - center_z
        view_x = m0 * x + m1 * y + m2 * z
        view_y = m3 * x + m4 * y + m5 * z
        view_z = m6 * x + m7 * y + m8 * z
        projected[index] = (half_width + view_x * scale,
                            half_height - view_y * scale,
                            view_z)

    light_x, light_y, light_z = _LIGHT_DIRECTION
    base_r, base_g, base_b = _BASE_COLOR
    step = 1
    if max_triangles and triangle_count > max_triangles:
        step = int(math.ceil(triangle_count / float(max_triangles)))

    faces = []
    append = faces.append
    for triangle in range(0, triangle_count, step):
        i0 = indices[triangle * 3]
        i1 = indices[triangle * 3 + 1]
        i2 = indices[triangle * 3 + 2]
        if not (0 <= i0 < vertex_count and 0 <= i1 < vertex_count
                and 0 <= i2 < vertex_count):
            continue
        x0, y0, z0 = projected[i0]
        x1, y1, z1 = projected[i1]
        x2, y2, z2 = projected[i2]

        # View space edge vectors (undo the screen scaling).
        e1x, e1y, e1z = (x1 - x0) / scale, (y0 - y1) / scale, z1 - z0
        e2x, e2y, e2z = (x2 - x0) / scale, (y0 - y2) / scale, z2 - z0
        normal_x = e1y * e2z - e1z * e2y
        normal_y = e1z * e2x - e1x * e2z
        normal_z = e1x * e2y - e1y * e2x
        length = math.sqrt(normal_x * normal_x
                           + normal_y * normal_y
                           + normal_z * normal_z)
        if length < 1e-12:
            continue    # degenerate triangle
        # Two sided shading: never turn a triangle black just because of
        # its winding order.
        light = abs((normal_x * light_x + normal_y * light_y
                     + normal_z * light_z) / length)
        shade = 0.22 + 0.78 * light
        color = QtGui.QColor(int(255 * base_r * shade),
                             int(255 * base_g * shade),
                             int(255 * base_b * shade))
        append(((z0 + z1 + z2) / 3.0, color,
                (x0, y0, x1, y1, x2, y2)))

    # Painter's algorithm: far triangles first.
    faces.sort(key=lambda face: face[0])
    set_brush = painter.setBrush
    draw_polygon = painter.drawPolygon
    point = QtCore.QPointF
    polygon = QtGui.QPolygonF
    for depth, color, points in faces:
        set_brush(color)
        draw_polygon(polygon([point(points[0], points[1]),
                              point(points[2], points[3]),
                              point(points[4], points[5])]))


def render_mesh(mesh, size, yaw=DEFAULT_YAW, pitch=DEFAULT_PITCH,
                zoom=DEFAULT_ZOOM, background=DEFAULT_BACKGROUND,
                max_triangles=FULL_QUALITY_TRIANGLES, antialiasing=True):
    """Rasterise ``mesh`` into a flat shaded :class:`QtGui.QImage`.

    This renderer only needs QPainter, which keeps previews, thumbnails and
    scene exports working without an OpenGL context.
    """

    if isinstance(size, QtCore.QSize):
        width, height = size.width(), size.height()
    else:
        width, height = int(size[0]), int(size[1])
    width = max(1, int(width))
    height = max(1, int(height))
    image = QtGui.QImage(width, height, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor.fromRgbF(*background))
    if mesh is None or not mesh.indices:
        return image

    painter = QtGui.QPainter(image)
    try:
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing,
                              antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        _paint_triangles(painter, mesh, width, height, yaw, pitch, zoom,
                         max_triangles, antialiasing)
    finally:
        painter.end()
    return image


def _flat_vertex_data(mesh):
    """Expand a mesh into flat [x, y, z, nx, ny, nz] triangles.

    OpenGL needs one normal per vertex, so the indexed mesh is expanded and
    every triangle carries its own flat normal.
    """

    positions = mesh.positions
    indices = mesh.indices
    vertex_count = len(positions) // 3
    data = []
    append = data.append
    for triangle in range(len(indices) // 3):
        i0 = indices[triangle * 3]
        i1 = indices[triangle * 3 + 1]
        i2 = indices[triangle * 3 + 2]
        if not (0 <= i0 < vertex_count and 0 <= i1 < vertex_count
                and 0 <= i2 < vertex_count):
            continue
        x0, y0, z0 = (positions[i0 * 3], positions[i0 * 3 + 1],
                      positions[i0 * 3 + 2])
        x1, y1, z1 = (positions[i1 * 3], positions[i1 * 3 + 1],
                      positions[i1 * 3 + 2])
        x2, y2, z2 = (positions[i2 * 3], positions[i2 * 3 + 1],
                      positions[i2 * 3 + 2])
        normal_x = (y1 - y0) * (z2 - z0) - (z1 - z0) * (y2 - y0)
        normal_y = (z1 - z0) * (x2 - x0) - (x1 - x0) * (z2 - z0)
        normal_z = (x1 - x0) * (y2 - y0) - (y1 - y0) * (x2 - x0)
        length = math.sqrt(normal_x * normal_x
                           + normal_y * normal_y
                           + normal_z * normal_z)
        if length > 1e-12:
            normal_x /= length
            normal_y /= length
            normal_z /= length
        for x, y, z in ((x0, y0, z0), (x1, y1, z1), (x2, y2, z2)):
            append(x)
            append(y)
            append(z)
            append(normal_x)
            append(normal_y)
            append(normal_z)
    return data


# ── Interactive canvases ─────────────────────────────────────────────


class _OrbitCanvas:
    """Camera state and mouse interaction shared by the preview canvases."""

    #: degrees per pixel of mouse movement
    ORBIT_SPEED = 0.4
    ZOOM_STEP = 1.15
    MIN_ZOOM = 0.15
    MAX_ZOOM = 12.0
    RENDER_SIZE = 512

    def __init__(self, mesh=None, parent=None):
        super().__init__(parent)
        self.mesh = mesh
        self.yaw = DEFAULT_YAW
        self.pitch = DEFAULT_PITCH
        self.zoom = DEFAULT_ZOOM
        self._drag_position = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    # ── camera ───────────────────────────────────────────────────────

    def set_mesh(self, mesh, reset_view=True):
        self.mesh = mesh
        if reset_view:
            self.reset_view()
        else:
            self.update()

    def reset_view(self):
        self.yaw = DEFAULT_YAW
        self.pitch = DEFAULT_PITCH
        self.zoom = DEFAULT_ZOOM
        self.update()

    def orbit_by(self, dx, dy):
        """Orbit the camera. ``dx``/``dy`` are mouse pixels."""

        self.yaw = (self.yaw + dx * self.ORBIT_SPEED) % 360.0
        self.pitch = _clamp(self.pitch + dy * self.ORBIT_SPEED, -89.0, 89.0)

    def zoom_by(self, steps):
        """Zoom the model by ``steps`` wheel notches."""

        self.zoom = _clamp(self.zoom * (self.ZOOM_STEP ** steps),
                           self.MIN_ZOOM, self.MAX_ZOOM)

    def current_image(self, size=None, fast=False):
        if size is None:
            size = QtCore.QSize(max(1, self.width()), max(1, self.height()))
        return render_mesh(
            self.mesh, size, self.yaw, self.pitch, self.zoom,
            max_triangles=(FAST_QUALITY_TRIANGLES if fast
                           else FULL_QUALITY_TRIANGLES),
            antialiasing=not fast)

    # ── interaction ──────────────────────────────────────────────────

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_position = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_position is not None:
            position = event.position()
            delta = position - self._drag_position
            self._drag_position = position
            self.orbit_by(delta.x(), delta.y())
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if (self._drag_position is not None
                and event.button() == Qt.MouseButton.LeftButton):
            self._drag_position = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        steps = event.angleDelta().y() / 120.0
        if steps:
            self.zoom_by(steps)
            self.update()
            event.accept()
            return
        super().wheelEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Home:
            self.reset_view()
            event.accept()
            return
        super().keyPressEvent(event)

    def sizeHint(self):
        return QtCore.QSize(self.RENDER_SIZE, self.RENDER_SIZE)


class GLBCanvasSoftware(_OrbitCanvas, QtWidgets.QWidget):
    """QPainter canvas, used when OpenGL is not available."""

    def paintEvent(self, event):
        image = self.current_image()
        painter = QtGui.QPainter(self)
        try:
            painter.drawImage(0, 0, image)
        finally:
            painter.end()


if _HAVE_OPENGL_MODULES:

    _GL_TRIANGLES = 0x0004
    _GL_COLOR_BUFFER_BIT = 0x00004000
    _GL_DEPTH_BUFFER_BIT = 0x00000100
    _GL_DEPTH_TEST = 0x0B71

    class GLBCanvasOpenGL(_OrbitCanvas, QtOpenGLWidgets.QOpenGLWidget):
        """OpenGL canvas: flat shaded triangles through a shader program.

        If a shader cannot be compiled or a GL function is missing, the
        canvas quietly falls back to the QPainter renderer instead of
        raising.
        """

        VERTEX_SHADER = """
            attribute vec3 position;
            attribute vec3 normal;
            uniform mat4 transform;
            varying float v_light;
            void main() {
                vec3 light = normalize(vec3(0.35, 0.5, 0.8));
                v_light = 0.22 + 0.78 * abs(dot(normalize(normal), light));
                gl_Position = transform * vec4(position, 1.0);
            }
        """

        FRAGMENT_SHADER = """
            varying float v_light;
            uniform vec3 base_color;
            void main() {
                gl_FragColor = vec4(base_color * v_light, 1.0);
            }
        """

        def __init__(self, mesh=None, parent=None):
            super().__init__(mesh, parent)
            self._program = None
            self._buffer = None
            self._functions = None
            self._vertex_count = 0
            self._gl_ready = False
            self._uploaded_mesh = None

        # ── OpenGL setup ─────────────────────────────────────────────

        def initializeGL(self):
            self._gl_ready = False
            program = None
            try:
                program = QtOpenGL.QOpenGLShaderProgram(self)
                program.addShaderFromSourceCode(
                    QtOpenGL.QOpenGLShader.ShaderTypeBit.Vertex,
                    self.VERTEX_SHADER)
                program.addShaderFromSourceCode(
                    QtOpenGL.QOpenGLShader.ShaderTypeBit.Fragment,
                    self.FRAGMENT_SHADER)
                if not program.link():
                    raise RuntimeError(program.log())
                context = self.context()
                if context is None:
                    raise RuntimeError('no OpenGL context')
                self._functions = _gl_version_functions(context)
                if self._functions is None:
                    raise RuntimeError('no OpenGL 2.0+ functions')
                buffer = QtOpenGL.QOpenGLBuffer(
                    QtOpenGL.QOpenGLBuffer.Type.VertexBuffer)
                if not buffer.create():
                    raise RuntimeError('could not create a vertex buffer')
                self._program = program
                self._buffer = buffer
                self._gl_ready = True
            except Exception:
                logger.exception(
                    'OpenGL initialisation failed, using software rendering')
                self._program = None
                self._buffer = None
                self._gl_ready = False
            self._upload_mesh()

        def set_mesh(self, mesh, reset_view=True):
            super().set_mesh(mesh, reset_view=reset_view)
            self._upload_mesh()

        def _upload_mesh(self):
            self._vertex_count = 0
            self._uploaded_mesh = None
            if not self._gl_ready or self.mesh is None or not self._buffer:
                return
            try:
                import array
                values = _flat_vertex_data(self.mesh)
                payload = array.array('f', values).tobytes()
                self._buffer.bind()
                self._buffer.allocate(payload, len(payload))
                self._buffer.release()
                self._vertex_count = len(values) // 6
                self._uploaded_mesh = self.mesh
            except Exception:
                logger.exception('Uploading mesh to OpenGL failed')
                self._gl_ready = False

        # ── drawing ──────────────────────────────────────────────────

        def _transform_matrix(self):
            widget_size = QtCore.QSize(max(1, self.width()),
                                       max(1, self.height()))
            matrix = QtGui.QMatrix4x4()
            projection = QtGui.QMatrix4x4()
            projection.ortho(-1.0, 1.0, -1.0, 1.0, -1.0, 1.0)
            aspect = (widget_size.width() / float(widget_size.height())
                      if widget_size.height() else 1.0)
            scale = 0.42 * self.zoom
            center = self.mesh.center()
            view = QtGui.QMatrix4x4()
            view.rotate(self.pitch, 1.0, 0.0, 0.0)
            view.rotate(self.yaw, 0.0, 1.0, 0.0)
            view.scale(scale, scale, scale)
            view.translate(-center[0], -center[1], -center[2])
            if aspect >= 1.0:
                projection.scale(1.0 / aspect, 1.0, 1.0)
            else:
                projection.scale(1.0, aspect, 1.0)
            matrix = projection * view
            return matrix

        def paintGL(self):
            if (not self._gl_ready or self.mesh is None
                    or not self._vertex_count
                    or self._uploaded_mesh is not self.mesh):
                self._paint_fallback()
                return
            try:
                self._paint_gl()
            except Exception:
                logger.exception(
                    'OpenGL rendering failed, using software rendering')
                self._gl_ready = False
                self._paint_fallback()

        def _paint_gl(self):
            functions = self._functions
            draw = getattr(functions, 'glDrawArrays', None)
            if draw is None:
                raise RuntimeError('glDrawArrays is not available')
            functions.glViewport(0, 0, max(1, self.width()),
                                 max(1, self.height()))
            functions.glClearColor(*DEFAULT_BACKGROUND, 1.0)
            functions.glClear(_GL_COLOR_BUFFER_BIT | _GL_DEPTH_BUFFER_BIT)
            functions.glEnable(_GL_DEPTH_TEST)
            program = self._program
            program.bind()
            program.enableAttributeArray('position')
            program.enableAttributeArray('normal')
            stride = 6 * 4
            program.setAttributeBuffer('position', 0x1406, 0, 3, stride)
            program.setAttributeBuffer('normal', 0x1406, 3 * 4, 3, stride)
            program.setUniformValue('transform', self._transform_matrix())
            program.setUniformValue('base_color',
                                    QtGui.QVector3D(*_BASE_COLOR))
            self._buffer.bind()
            draw(_GL_TRIANGLES, 0, self._vertex_count)
            self._buffer.release()
            program.disableAttributeArray('position')
            program.disableAttributeArray('normal')
            program.release()

        def _paint_fallback(self):
            """Draw with QPainter when GL is unavailable or broken."""
            painter = QtGui.QPainter(self)
            try:
                painter.drawImage(0, 0, self.current_image())
            finally:
                painter.end()

    def _gl_version_functions(context):
        """Return GL 2.1/2.0 function wrappers, or None."""

        for functions_class, version in (
                (QtOpenGL.QOpenGLFunctions_2_1, (2, 1)),
                (QtOpenGL.QOpenGLFunctions_2_0, (2, 0))):
            profile = QtOpenGL.QOpenGLVersionProfile()
            profile.setVersion(*version)
            try:
                functions = QtOpenGL.QOpenGLVersionFunctionsFactory.get(
                    profile, context)
            except Exception:
                logger.debug('Could not obtain OpenGL functions',
                             exc_info=True)
                continue
            if functions is not None and hasattr(functions, 'glClear'):
                return functions
        return None

else:  # pragma: no cover - PyQt6 builds without QtOpenGL

    GLBCanvasOpenGL = None

    def _gl_version_functions(context):
        return None


def _detect_opengl():
    if not _HAVE_OPENGL_MODULES:
        return False
    application = QtWidgets.QApplication.instance()
    if application is None:
        return False
    platform = (application.platformName() or '').lower()
    if platform in ('offscreen', 'minimal', 'minimalegl', 'vnc'):
        # These platforms never provide a usable OpenGL context.
        return False
    try:
        context = QtGui.QOpenGLContext()
        return bool(context.create())
    except Exception:  # pragma: no cover - driver dependent
        logger.debug('OpenGL context creation failed', exc_info=True)
        return False


def opengl_available():
    """Whether an OpenGL context can be created in this process.

    The result is cached: creating probe contexts is not free and the answer
    cannot change while the application is running.
    """

    global _opengl_available
    if _opengl_available is None:
        _opengl_available = _detect_opengl()
    return _opengl_available


class GLBPreviewSurface(QtWidgets.QWidget):
    """Interactive 3D canvas, backed by OpenGL when possible."""

    def __init__(self, mesh=None, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        if opengl_available() and GLBCanvasOpenGL is not None:
            canvas = GLBCanvasOpenGL(mesh, self)
        else:
            canvas = GLBCanvasSoftware(mesh, self)
        self._canvas = canvas
        layout.addWidget(canvas)

    @property
    def canvas(self):
        return self._canvas

    @property
    def software_rendering(self):
        """True when the preview runs on the QPainter fallback."""

        return not isinstance(self._canvas, QtOpenGLWidgets.QOpenGLWidget)

    @property
    def mesh(self):
        return self._canvas.mesh

    def set_mesh(self, mesh, reset_view=True):
        self._canvas.set_mesh(mesh, reset_view=reset_view)

    def reset_view(self):
        self._canvas.reset_view()

    def current_image(self, size=None):
        return self._canvas.current_image(size)


class GLBPreviewWindow(QtWidgets.QDialog):
    """Window with an interactive preview of a GLB/glTF model."""

    def __init__(self, mesh=None, title='', parent=None):
        super().__init__(parent)
        self.setWindowTitle(title or _('3D preview'))
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.resize(720, 560)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.surface = GLBPreviewSurface(mesh, self)
        layout.addWidget(self.surface, 1)
        hint = QtWidgets.QLabel(
            _('Drag to orbit the model, scroll to zoom, Home to reset'))
        hint.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        hint.setMargin(4)
        layout.addWidget(hint, 0)
