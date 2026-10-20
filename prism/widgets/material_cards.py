"""SurfaceStudio's original card renderer hosted by Prism's existing WebEngine."""
import json
import hashlib
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt
from prism.fileio.attachments import encode_data_url
from prism.performance_trace import measured, record as trace_record


ASSETS = Path(__file__).resolve().parents[1] / 'assets' / 'materials'


@lru_cache(maxsize=512)
def _wire_image_id(data_url):
    return hashlib.sha256(data_url.encode('utf-8')).hexdigest()


def _asset_image_key(asset):
    # Every lazy record initially displays the same white sphere. That image
    # is not proof that another record's actual thumbnail has been requested.
    if asset.get('thumbnailPending'):
        return 'pending:' + asset['id']
    return _wire_image_id(asset['thumbnail'])


def material_source_label(record):
    textures = bool(record.get('textures'))
    native = bool(record.get('nativePackages'))
    return ('两者都有' if textures and native else '含 UE 原件' if native else
            '只有贴图' if textures else '草稿 · 待添加贴图')


def material_version_label(record):
    versions = []
    for package in record.get('nativePackages', []):
        value = package.get('engineVersion') if isinstance(package, dict) else None
        if isinstance(value, str) and value and value not in versions:
            versions.append(value)
    return 'UE ' + ' / '.join(versions) if versions else 'UE 版本未提供'


@lru_cache(maxsize=2)
def _placeholder_default():
    default = ASSETS / 'default-sphere.png'
    if default.is_file():
        return encode_data_url('image/png', default.read_bytes())
    return None


def placeholder_image(record):
    default = _placeholder_default()
    if default:
        return default
    message = '等待生成材质球' if record.get('textures') else '待添加贴图'
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="480" height="355">'
           '<rect width="480" height="355" fill="#222225"/>'
           '<path d="M215 125h50v42h-50z m0 30 15-15 12 12 9-7 14 12" '
           'fill="none" stroke="#68717d" stroke-width="2"/>'
           f'<text x="240" y="205" text-anchor="middle" fill="#929ba8" '
           f'font-size="16" font-family="Microsoft YaHei,sans-serif">{message}</text></svg>')
    return 'data:image/svg+xml;charset=utf-8,' + quote(svg)


@measured('materials.thumbnail', minimum_ms=4)
def material_thumbnail(record, attachments, edge=480):
    """Read an already-generated thumbnail, never an arbitrary source path."""
    if record.get('preview', {}).get('discardedAdjustments'):
        return None
    attachment_id = record.get('preview', {}).get('thumbnailAttachmentId')
    if not isinstance(attachment_id, int):
        return None
    entry = attachments.get(attachment_id)
    if not entry or not isinstance(entry[2], bytes):
        return None
    image = QtGui.QImage.fromData(entry[2])
    if image.isNull():
        return None
    # Generated spheres are already 256px PNGs. Never upscale/recompress every
    # card to 480px just to send it through WebChannel again.
    if max(image.width(), image.height()) <= edge and entry[2].startswith(b'\x89PNG\r\n\x1a\n'):
        return encode_data_url('image/png', entry[2])
    image = image.scaled(edge, edge, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    buffer = QtCore.QBuffer()
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, 'PNG')
    return encode_data_url('image/png', bytes(buffer.data()))


class _ScrollPosition:
    """Expose the view's cached pixel position to the existing navigation state."""
    def __init__(self, grid):
        self.grid = grid

    def value(self):
        return self.grid.scroll_position

    def setValue(self, value):
        self.grid.set_scroll_position(value)


class _CardBridge(QtCore.QObject):
    def __init__(self, grid):
        super().__init__(grid)
        self.grid = grid

    @QtCore.pyqtSlot()
    def ready(self):
        self.grid.ready = True
        self.grid._push()

    @QtCore.pyqtSlot(str)
    def metrics(self, payload):
        if len(payload) <= 8192:
            try:
                trace_record('material_browser', surface='cards', metrics=json.loads(payload))
            except (ValueError, TypeError):
                pass

    @QtCore.pyqtSlot(str, int)
    def imagesRequested(self, payload, generation):
        if generation != self.grid.generation or len(payload) > 4096:
            return
        try:
            ids = json.loads(payload)
        except ValueError:
            return
        if not isinstance(ids, list):
            return
        self.grid._send_images(set(value for value in ids[:16] if isinstance(value, str)))

    @QtCore.pyqtSlot(str, int)
    def choose(self, record_id, generation):
        if generation == self.grid.generation:
            self.grid.select_record(self.grid._dom_to_id.get(record_id))

    @QtCore.pyqtSlot(str, int, bool)
    def chooseMany(self, record_id, generation, additive):
        if generation == self.grid.generation:
            self.grid.select_record(self.grid._dom_to_id.get(record_id),additive)

    @QtCore.pyqtSlot(str, int)
    def dragTextures(self, record_id, generation):
        if generation != self.grid.generation:
            return
        actual = self.grid._dom_to_id.get(record_id)
        if actual not in self.grid._record_ids:
            return
        if actual not in self.grid.selected_ids:
            self.grid.select_record(actual)
        ids = [key for key in self.grid._record_ids if key in self.grid.selected_ids]
        self.grid.textures_drag_requested.emit(ids)

    @QtCore.pyqtSlot(int, int)
    def scrolled(self, position, generation):
        if generation == self.grid.generation:
            self.grid.scroll_position = max(0, position)
            self.grid.scroll_changed.emit()

    @QtCore.pyqtSlot(str, int)
    def open(self, record_id, generation):
        if generation == self.grid.generation:
            actual = self.grid._dom_to_id.get(record_id)
            if actual:
                self.grid.open_requested.emit(actual)

    @QtCore.pyqtSlot(str, int)
    def export(self, record_id, generation):
        if generation == self.grid.generation:
            actual = self.grid._dom_to_id.get(record_id)
            if actual:
                if actual in self.grid.selected_ids and len(self.grid.selected_ids)>1:
                    self.grid.batch_export_requested.emit(self.grid.selected_ids)
                else:
                    self.grid.export_requested.emit(actual)


class MaterialCards(QtWidgets.QWidget):
    selection_changed = QtCore.pyqtSignal(object)
    open_requested = QtCore.pyqtSignal(str)
    export_requested = QtCore.pyqtSignal(str)
    batch_export_requested = QtCore.pyqtSignal(object)
    scroll_changed = QtCore.pyqtSignal()
    thumbnail_requested = QtCore.pyqtSignal(str)
    textures_drag_requested = QtCore.pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.assets = []
        self._record_ids = []
        self._dom_to_id = {}
        self.selected_id = None
        self.selected_ids = set()
        self.scroll_position = 0
        self.generation = 0
        self.ready = False
        self.web = None
        self.mode = 'grid'
        self.card_size = 220
        self._signature = None
        self._context = None
        self.mouse_sequence = 0
        QtWidgets.QApplication.instance().installEventFilter(self)
        self._scroll = _ScrollPosition(self)
        self._push_timer = QtCore.QTimer(self)
        self._push_timer.setSingleShot(True)
        self._push_timer.timeout.connect(self._push)
        self._layout = QtWidgets.QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        # Create the shared WebEngine surface before the top-level is shown;
        # late creation otherwise changes Windows composition on the first tab.
        self._ensure_web()

    def eventFilter(self, watched, event):
        if event.type() in (QtCore.QEvent.Type.MouseButtonPress,QtCore.QEvent.Type.MouseButtonRelease):
            self.mouse_sequence += 1
        return False

    def showEvent(self, event):
        super().showEvent(event)
        if self.web is None:
            self._ensure_web()

    def _ensure_web(self):
        from PyQt6 import QtWebChannel, QtWebEngineCore, QtWebEngineWidgets
        self.web = QtWebEngineWidgets.QWebEngineView(self)
        self.web.renderProcessTerminated.connect(lambda status, code:
            trace_record('material_browser_process_exit', surface='cards', status=status.name, code=code))
        self.web.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.web.page().setBackgroundColor(QtGui.QColor('#1c1c1e'))
        self.web.settings().setAttribute(
            QtWebEngineCore.QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        self.channel = QtWebChannel.QWebChannel(self.web.page())
        self.bridge = _CardBridge(self)
        self.channel.registerObject('materialCards', self.bridge)
        self.web.page().setWebChannel(self.channel)
        self._layout.addWidget(self.web)
        self.web.load(QtCore.QUrl.fromLocalFile(str(ASSETS / 'cards.html')))

    @measured('materials.cards.set_records')
    def set_records(self, records, selected_id=None, thumbnail_for=None):
        assets = []
        self._record_ids = [r['id'] for r in records]
        self._dom_to_id = {self._dom_id(r['id']): r['id'] for r in records}
        for record in records:
            thumbnail = thumbnail_for(record) if thumbnail_for else None
            textures = record.get('textures', {})
            dimensions = [max(t.get('width', 0), t.get('height', 0))
                          for t in textures.values() if isinstance(t, dict)]
            edge = max(dimensions, default=0)
            resolution = (f'{edge // 1024}K' if edge and edge % 1024 == 0 else f'{edge}px') if edge else '草稿'
            assets.append(dict(
                id=self._dom_id(record['id']), name=record['name'], tags=record.get('tags', []),
                thumbnail=thumbnail or placeholder_image(record),
                thumbnailPending=not thumbnail and isinstance(record.get('preview', {}).get('thumbnailAttachmentId'),int)
                    and not record.get('preview', {}).get('discardedAdjustments'),
                sourceLabel=material_source_label(record), versionLabel=material_version_label(record),
                categoryZh='', resolution=resolution, normalOnly=False,
                fileCount=len(record.get('files', textures)),
                maps=[{'channel': key} for key in textures]))
        ids = set(self._record_ids)
        self.selected_ids &= ids
        self.selected_id = selected_id if selected_id in ids else None
        if self.selected_id and self.selected_id not in self.selected_ids:
            self.selected_ids = {self.selected_id}
        # Compare references/metadata, rather than allocating a second JSON
        # copy of all base64 thumbnails for every search/filter refresh.
        signature = tuple((asset['id'], asset['name'], tuple(asset['tags']), asset['thumbnail'],
                           asset['sourceLabel'], asset['versionLabel'], asset['resolution'],
                           asset['fileCount'], tuple(m['channel'] for m in asset['maps'])) for asset in assets)
        if self._signature != signature:
            self._signature = signature
            self.assets = assets
            self.generation += 1
            self._schedule_push()
        elif self.ready:
            self.web.page().runJavaScript('prismCards.setSelection(' + json.dumps(self._dom_id(self.selected_id)) + ',' + json.dumps([self._dom_id(i) for i in self.selected_ids]) + ')')

    def update_thumbnail(self, record, thumbnail):
        """Send only the finished card, never hundreds of unchanged data URLs."""
        dom_id = self._dom_id(record['id'])
        asset = next((asset for asset in self.assets if asset['id'] == dom_id), None)
        if asset is None:
            return
        asset.update(thumbnail=thumbnail or placeholder_image(record))
        asset['thumbnailPending'] = False
        self._signature = None
        if self.ready:
            key = _asset_image_key(asset)
            script = 'prismCards.updateAsset(' + json.dumps(dict(asset, thumbnail=key), ensure_ascii=False)
            script += ',' + json.dumps({key:asset['thumbnail']}) + ')'
            trace_record('material_cards_patch', material=record['id'], bytes=len(script.encode('utf-8')))
            self.web.page().runJavaScript(script)

    def set_context(self, context):
        if context != self._context:
            self._context = context
            self.generation += 1
            self._signature = None
            self._dom_to_id = {}
            self.selected_ids.clear()

    @staticmethod
    def _dom_id(record_id):
        # Upstream uses IDs directly in HTML. Keep them safe even in old files.
        return 'm_' + hashlib.sha256(record_id.encode('utf-8')).hexdigest() if record_id else None

    def _schedule_push(self):
        # Several service signals may arrive while a form is being flushed.
        self._push_timer.start(0)

    @measured('materials.cards.push')
    def _push(self):
        if not self.ready:
            return
        assets = []
        for asset in self.assets:
            key = _asset_image_key(asset)
            assets.append(dict(asset, thumbnail=key))
        data = dict(assets=assets, selected=self._dom_id(self.selected_id),
                    selectedIds=[self._dom_id(i) for i in self.selected_ids],
                    scroll=self.scroll_position, generation=self.generation)
        script = 'prismCards.setData(' + json.dumps(data, ensure_ascii=False) + ')'
        trace_record('material_cards_payload', records=len(self.assets), bytes=len(script.encode('utf-8')))
        self.web.page().runJavaScript(script)
        self.set_layout(self.mode, self.card_size)

    @measured('materials.cards.images')
    def _send_images(self, ids):
        for asset in self.assets:
            if asset['id'] in ids and asset.get('thumbnailPending'):
                actual = self._dom_to_id.get(asset['id'])
                if actual:
                    self.thumbnail_requested.emit(actual)
        # Acknowledge each pending record without retransmitting the same white
        # placeholder PNG hundreds of times. The worker patches its real image.
        images = {_asset_image_key(asset):('' if asset.get('thumbnailPending') else asset['thumbnail'])
                  for asset in self.assets if asset['id'] in ids}
        script = 'prismCards.receiveImages('+json.dumps(images)+','+str(self.generation)+')'
        trace_record('material_cards_images', images=len(images), bytes=len(script.encode('utf-8')))
        self.web.page().runJavaScript(script)

    def set_layout(self, mode, size):
        self.mode = 'list' if mode == 'list' else 'grid'
        self.card_size = max(160, min(320, int(size)))
        if self.ready:
            self.web.page().runJavaScript(
                'prismCards.setLayout(' + json.dumps(self.mode) + ',' + str(self.card_size) + ')')

    def select_record(self, record_id, additive=False):
        if record_id not in self._record_ids:
            return
        if additive:
            if record_id in self.selected_ids:
                self.selected_ids.remove(record_id)
                record_id = next(iter(self.selected_ids),None)
            else:
                self.selected_ids.add(record_id)
        else:
            self.selected_ids = {record_id}
        self.selected_id = record_id
        self.selection_changed.emit(record_id)

    def show_selection(self, record_id):
        self.selected_id = record_id if record_id in self._record_ids else None
        if self.selected_id and self.selected_id not in self.selected_ids:
            self.selected_ids = {self.selected_id}
        if self.ready:
            self.web.page().runJavaScript('prismCards.setSelection(' + json.dumps(self._dom_id(self.selected_id))
                + ',' + json.dumps([self._dom_id(i) for i in self.selected_ids]) + ')')

    def reveal_record(self, record_id):
        if self.ready:
            # A folder refresh may still have a queued WebChannel data push.
            self._push_timer.stop()
            self._push()
            self.web.page().runJavaScript('prismCards.reveal('+json.dumps(self._dom_id(record_id))+')')

    def setCurrentRow(self, row):
        if 0 <= row < len(self.assets):
            self.select_record(self._record_ids[row])

    def currentRow(self):
        return self._record_ids.index(self.selected_id) if self.selected_id in self._record_ids else -1

    def count(self):
        return len(self.assets)

    def verticalScrollBar(self):
        return self._scroll

    def set_scroll_position(self, value):
        self.scroll_position = max(0, int(value))
        if self.ready:
            self.web.page().runJavaScript('prismCards.scrollTo(' + str(self.scroll_position) + ')')
