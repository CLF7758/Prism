"""SurfaceStudio's original card renderer hosted by Prism's existing WebEngine."""
import json
import hashlib
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from PyQt6 import QtCore, QtGui, QtWidgets, sip
from PyQt6.QtCore import Qt
from prism.fileio.attachments import encode_data_url
from prism.performance_trace import measured, operation, record as trace_record


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


def model_card_fields(model_format=''):
    """One model card presentation for its library and mixed-resource lists."""
    return dict(assetKind='model', resolution=(model_format or '').upper(),
                sourceLabel='', versionLabel='', fileCount=0, maps=[], metaLabel='',
                thumbnail=placeholder_image({'assetKind':'model'}), thumbnailPending=True)


def material_card_fields(record):
    """Shared native-card metadata for the library and mixed-resource view."""
    textures = record.get('textures', {})
    edge = max((max(texture.get('width', 0), texture.get('height', 0))
                for texture in textures.values() if isinstance(texture, dict)), default=0)
    resolution = (f'{edge // 1024}K' if edge and edge % 1024 == 0 else f'{edge}px') if edge else '草稿'
    return dict(sourceLabel=material_source_label(record), versionLabel=material_version_label(record),
                categoryZh='', resolution=resolution, normalOnly=False,
                fileCount=record.get('fileCount', len(record.get('files', textures))),
                maps=[{'channel': key} for key in textures])


@lru_cache(maxsize=2)
def _placeholder_default():
    default = ASSETS / 'default-sphere.png'
    if default.is_file():
        return encode_data_url('image/png', default.read_bytes())
    return None


def placeholder_image(record):
    default = _placeholder_default()
    if record.get('assetKind') == 'model':
        # A model without a cover gets a neutral model marker, never a
        # material sphere: the sphere would claim a material the model does
        # not have.
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="480" height="355">'
               '<rect width="480" height="355" fill="#222225"/>'
               '<path d="M196 118h88v74h-88z m0 74 22-26 22 21 16-13 28 24" '
               'fill="none" stroke="#68717d" stroke-width="2"/>'
               '<text x="240" y="245" text-anchor="middle" fill="#929ba8" '
               'font-size="16" font-family="Microsoft YaHei,sans-serif">'
               '模型封面待生成</text></svg>')
        return 'data:image/svg+xml;charset=utf-8,' + quote(svg)
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
    # One read, five outcomes. "The picture is unavailable" is not a diagnosis;
    # which of these five it is tells the user what to do next.
    material = str(record.get('id') or record.get('materialId') or '')

    def _note(outcome, **extra):
        trace_record('material_thumbnail_read', outcome=outcome,
                     material=material, edge=edge, **extra)

    if record.get('preview', {}).get('discardedAdjustments'):
        _note('discarded')
        return None
    attachment_id = record.get('preview', {}).get('thumbnailAttachmentId')
    if not isinstance(attachment_id, int):
        _note('no_thumbnail_record')
        return None
    entry = attachments.get(attachment_id)
    if not entry or not isinstance(entry[2], bytes):
        _note('missing_attachment', attachment=attachment_id)
        return None
    image = QtGui.QImage.fromData(entry[2])
    if image.isNull():
        _note('decode_failed', attachment=attachment_id, source_bytes=len(entry[2]))
        return None
    # Generated spheres are already 256px PNGs. Never upscale/recompress every
    # card to 480px just to send it through WebChannel again.
    if max(image.width(), image.height()) <= edge and entry[2].startswith(b'\x89PNG\r\n\x1a\n'):
        _note('ready', attachment=attachment_id, source_bytes=len(entry[2]),
              passthrough=True)
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

    @QtCore.pyqtSlot(str, int)
    def rename(self, record_id, generation):
        if generation == self.grid.generation:
            actual = self.grid._dom_to_id.get(record_id)
            if actual:
                self.grid.rename_requested.emit(actual)

    @QtCore.pyqtSlot(str, str, int)
    def renameInline(self, record_id, name, generation):
        if generation == self.grid.generation:
            actual = self.grid._dom_to_id.get(record_id)
            if actual in self.grid._record_ids and name.strip() and len(name.strip()) <= 160:
                self.grid.rename_committed.emit(actual, name.strip())

    @QtCore.pyqtSlot(str, int, bool)
    def chooseRange(self, record_id, generation, additive):
        if generation != self.grid.generation:return
        grid=self.grid
        actual=grid._dom_to_id.get(record_id)
        ids=grid._record_ids
        anchor=getattr(grid,'_selection_anchor',grid.selected_id)
        if actual not in ids:return
        if anchor not in ids:anchor=actual
        a,b=sorted((ids.index(anchor),ids.index(actual)))
        selected=set(ids[a:b+1]) | (grid.selected_ids if additive else set())
        # Existing controllers consume the same additive selection contract.
        grid.selected_ids = selected
        grid.selected_id = actual
        grid.selection_replaced.emit(tuple(key for key in ids if key in selected), actual, bool(additive))
        grid.selection_changed.emit(grid.selected_id)
        grid._selection_anchor=anchor

    @QtCore.pyqtSlot(int)
    def deleteSelected(self, generation):
        if generation == self.grid.generation:
            self.grid.delete_requested.emit()

    @QtCore.pyqtSlot(str, int)
    def restore(self, record_id, generation):
        if generation != self.grid.generation:
            return
        actual = self.grid._dom_to_id.get(record_id)
        if actual not in self.grid._record_ids:
            return
        if actual not in self.grid.selected_ids:
            self.grid.select_record(actual)
        self.grid.restore_requested.emit(tuple(self.grid.selected_ids))

    @QtCore.pyqtSlot()
    def ready(self):
        self.grid.ready = True
        self.grid._push()
        self.grid.content_ready.emit()

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
        # Return from the WebChannel slot before entering Qt's native drag
        # loop; Chromium must finish its dragstart acknowledgement first.
        QtCore.QTimer.singleShot(0, lambda: self.grid.textures_drag_requested.emit(ids)
                                if not sip.isdeleted(self.grid) and generation == self.grid.generation else None)

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

    @QtCore.pyqtSlot(int)
    def needsMore(self, generation):
        """The surface reached the end of the installed window."""
        if generation == self.grid.generation:
            self.grid.more_requested.emit()


class MaterialCards(QtWidgets.QWidget):
    restore_requested = QtCore.pyqtSignal(object)
    rename_requested = QtCore.pyqtSignal(str)
    rename_committed = QtCore.pyqtSignal(str, str)
    card_size_requested = QtCore.pyqtSignal(int)
    delete_requested = QtCore.pyqtSignal()
    selection_changed = QtCore.pyqtSignal(object)
    #: The user clicked a card. Payload is (record id, additive).
    #: Carries the exact click, including a record being toggled off. A host
    #: can mirror its authoritative selection back during the callback.
    selection_toggled = QtCore.pyqtSignal(str, bool)
    selection_replaced = QtCore.pyqtSignal(object, object, bool)
    open_requested = QtCore.pyqtSignal(str)
    export_requested = QtCore.pyqtSignal(str)
    batch_export_requested = QtCore.pyqtSignal(object)
    scroll_changed = QtCore.pyqtSignal()
    thumbnail_requested = QtCore.pyqtSignal(str)
    textures_drag_requested = QtCore.pyqtSignal(object)
    #: The surface reached the end of the installed window and needs the
    #: next page. Only emitted in bounded mode.
    more_requested = QtCore.pyqtSignal()
    #: The web document loaded and can take its first payload. Before this the
    #: surface is a blank rectangle, so a host that shows it too early shows
    #: an empty panel first and the content afterwards.
    content_ready = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        from .widget_construction import finish_construction
        finish_construction(self._construct(parent))

    def _construct(self, parent=None):
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
        self._bounded = False
        self._virtual = False
        self.mouse_sequence = 0
        self._external_drop_accepts = None
        self._external_drop_handler = None
        QtWidgets.QApplication.instance().installEventFilter(self)
        self._scroll = _ScrollPosition(self)
        self._push_timer = QtCore.QTimer(self)
        self._push_timer.setSingleShot(True)
        self._push_timer.timeout.connect(self._push)
        self._layout = QtWidgets.QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        # Construction is not native activation. Keep separate trace boundaries
        # for creating the surface and its first native show/resize below.
        yield
        self._ensure_web()
        yield

    def eventFilter(self, watched, event):
        if (event.type()==QtCore.QEvent.Type.Wheel and event.modifiers() & Qt.KeyboardModifier.ControlModifier
                and self.web is not None and isinstance(watched,QtWidgets.QWidget)
                and (watched is self.web or self.web.isAncestorOf(watched))):
            delta=event.angleDelta().y() or event.pixelDelta().y()
            if delta:self.card_size_requested.emit(self.card_size+(12 if delta>0 else -12))
            event.accept();return True

        if (event.type() in (QtCore.QEvent.Type.DragEnter, QtCore.QEvent.Type.DragMove,
                             QtCore.QEvent.Type.Drop)
                and self._external_drop_accepts is not None
                and self.web is not None and isinstance(watched, QtWidgets.QWidget)
                and (watched is self.web or self.web.isAncestorOf(watched))):
            mime = event.mimeData()
            # Internal library moves retain their existing owner and semantics.
            if mime.hasUrls() and not mime.hasFormat('application/x-prism-library-assets'):
                accepted = self._external_drop_accepts(mime)
                if accepted and event.type() == QtCore.QEvent.Type.Drop:
                    self._external_drop_handler(mime)
                if accepted:
                    event.setDropAction(Qt.DropAction.CopyAction)
                    event.accept()
                else:
                    event.ignore()
                # Never let Chromium navigate away from the card page on a file drop.
                return True
        if (event.type() == QtCore.QEvent.Type.ShortcutOverride
                and event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_F2)
                and self.isVisible() and self.web is not None
                and isinstance(watched, QtWidgets.QWidget)
                and (watched is self.web or self.web.isAncestorOf(watched))):
            # Canvas Delete must never consume a key intended for library cards.
            # Accepting the override delivers the key as a normal key press
            # to the focus widget (the card page) and keeps the window scoped
            # canvas action from claiming it (QEvent::ShortcutOverride).
            event.accept()
            return True
        if event.type() in (QtCore.QEvent.Type.MouseButtonPress,QtCore.QEvent.Type.MouseButtonRelease):
            self.mouse_sequence += 1
        return False

    def set_external_drop_handler(self, accepts, handler):
        """Opt in to local-file import; other card surfaces remain unchanged."""
        if not callable(accepts) or not callable(handler):
            raise TypeError('External drops require acceptance and import callbacks')
        self._external_drop_accepts = accepts
        self._external_drop_handler = handler

    def showEvent(self, event):
        super().showEvent(event)
        if self.web is None:
            self._ensure_web()

    @measured('materials.cards.create_surface')
    def _ensure_web(self):
        if self.web is not None:
            return
        from PyQt6 import QtWebChannel, QtWebEngineCore, QtWebEngineWidgets

        class CardWebView(QtWebEngineWidgets.QWebEngineView):
            # Only the first event is traced: normal window resizing must not
            # produce a per-frame diagnostic stream. These spans measure the
            # Python-visible event handler, not all Chromium/GPU work.
            def showEvent(surface, event):
                if getattr(surface, '_first_show_seen', False):
                    return super().showEvent(event)
                surface._first_show_seen = True
                with operation('materials.cards.first_native_show'):
                    return super().showEvent(event)

            def resizeEvent(surface, event):
                if getattr(surface, '_first_resize_seen', False):
                    return super().resizeEvent(event)
                surface._first_resize_seen = True
                with operation('materials.cards.first_native_resize',
                               width=event.size().width(), height=event.size().height()):
                    return super().resizeEvent(event)

        self.web = CardWebView(self)
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
        from .widget_construction import finish_construction
        finish_construction(self.set_records_steps(records, selected_id, thumbnail_for))

    def set_records_steps(self, records, selected_id=None, thumbnail_for=None, *, valid=None):
        """Prepare privately in bounded batches; publish only one complete list."""
        assets = []
        record_ids, dom_to_id, signature = [], {}, []
        for index, record in enumerate(records, 1):
            record_id, dom_id = record['id'], self._dom_id(record['id'])
            record_ids.append(record_id)
            dom_to_id[dom_id] = record_id
            thumbnail = thumbnail_for(record) if thumbnail_for else None
            assets.append(dict(
                id=dom_id, name=record['name'], tags=record.get('tags', []),
                thumbnail=thumbnail or placeholder_image(record),
                thumbnailPending=not thumbnail and isinstance(record.get('preview', {}).get('thumbnailAttachmentId'),int)
                    and not record.get('preview', {}).get('discardedAdjustments'),
                **material_card_fields(record)))
            asset = assets[-1]
            signature.append((asset['id'], asset['name'], tuple(asset['tags']), asset['thumbnail'],
                              asset['sourceLabel'], asset['versionLabel'], asset['resolution'],
                              asset['fileCount'], tuple(m['channel'] for m in asset['maps'])))
            if index % 64 == 0:
                yield
                if valid is not None and not valid():
                    return False
        if valid is not None and not valid():
            return False
        self._record_ids, self._dom_to_id = record_ids, dom_to_id
        ids = set(record_ids)
        self.selected_ids &= ids
        self.selected_id = selected_id if selected_id in ids else None
        if self.selected_id and self.selected_id not in self.selected_ids:
            self.selected_ids = {self.selected_id}
        # Compare references/metadata, rather than allocating a second JSON
        # copy of all base64 thumbnails for every search/filter refresh.
        signature = tuple(signature)
        if self._signature != signature:
            self._signature = signature
            self.assets = assets
            self.generation += 1
            self._schedule_push()
        elif self.ready:
            self.web.page().runJavaScript('prismCards.setSelection(' + json.dumps(self._dom_id(self.selected_id)) + ',' + json.dumps([self._dom_id(i) for i in self.selected_ids]) + ')')
        return True

    def set_display_assets(self, assets, selected_id=None, selected_ids=None,
                            bounded=False, virtual=False):
        """Install already-prepared card summaries, without material semantics.

        Presentation-only entry point for the model-library adapter. Callers
        provide a bounded page and cached images; this method never loads files.
        Existing material callers continue using set_records unchanged.

        The selection is NOT intersected with the current page. The model
        library pages through a larger library and keeps its selection in a
        separate controller; silently dropping the picks made on other pages
        here would lose the user's batch. ``selected_ids`` lets the owner
        mirror the part of its selection that is visible on this page.

        ``bounded`` tells the surface that the installed list is a window:
        it must ask for more instead of growing the DOM without limit.
        """
        self._record_ids = [asset['id'] for asset in assets]
        self._dom_to_id = {self._dom_id(key): key for key in self._record_ids}
        self.assets = [
            dict(asset, id=self._dom_id(asset['id'])) for asset in assets]
        ids = set(self._record_ids)
        if selected_ids is None:
            self.selected_ids = set()
        else:
            self.selected_ids = {key for key in selected_ids if key in ids}
        self.selected_id = selected_id if selected_id in ids else None
        if self.selected_id and self.selected_id not in self.selected_ids:
            self.selected_ids.add(self.selected_id)
        self._bounded = bool(bounded)
        self._virtual = bool(virtual)
        self._signature = None
        self.generation += 1
        self._schedule_push()

    def set_selection(self, record_ids, selected_id=None):
        """Mirror an externally owned selection onto the visible cards.

        Used by the model library, whose selection controller spans pages.
        Only the ids that exist on the current page are painted; the rest of
        the selection stays in the controller.
        """
        ids = set(self._record_ids)
        self.selected_ids = {key for key in (record_ids or ()) if key in ids}
        if selected_id in self.selected_ids:
            self.selected_id = selected_id
        elif self.selected_ids:
            self.selected_id = next(iter(self.selected_ids))
        else:
            self.selected_id = None
        if self.ready:
            self.web.page().runJavaScript(
                'prismCards.setSelection(' + json.dumps(self._dom_id(self.selected_id))
                + ',' + json.dumps([self._dom_id(i) for i in self.selected_ids]) + ')')

    def append_display_assets(self, assets):
        """Append streamed summaries to a virtual surface without a full resend."""
        prepared = [dict(asset, id=self._dom_id(asset['id'])) for asset in assets]
        for asset, display in zip(assets, prepared):
            self._record_ids.append(asset['id'])
            self._dom_to_id[display['id']] = asset['id']
        self.assets.extend(prepared)
        if not self.ready or not self._virtual:
            self._schedule_push()
            return
        self.generation += 1
        self._asset_lookup.update((asset['id'], asset) for asset in prepared)
        data = dict(assets=[dict(asset, thumbnail=_asset_image_key(asset)) for asset in prepared],
                    generation=self.generation)
        self.web.page().runJavaScript('prismCards.appendData(' + json.dumps(data, ensure_ascii=False) + ')')

    def update_thumbnail(self, record, thumbnail):
        """Send only the finished card, never hundreds of unchanged data URLs."""
        dom_id = self._dom_id(record['id'])
        asset = getattr(self, '_asset_lookup', {}).get(dom_id)
        if asset is None:
            asset = next((asset for asset in self.assets if asset['id'] == dom_id), None)
        if asset is None:
            return
        merged = dict(asset)
        merged.update({key: value for key, value in record.items() if key != 'id'})
        # Cleared either way, so this flag alone cannot tell "has a picture"
        # from "fell back to the placeholder sphere" -- which is exactly the
        # symptom "cards stay on the white ball forever".
        real_image = bool(thumbnail)
        asset.update(thumbnail=thumbnail or placeholder_image(merged))
        asset['thumbnailPending'] = False
        trace_record('material_thumbnail_stored', material=record['id'],
                     real=real_image, dom=dom_id, surface_ready=bool(self.ready))
        self._signature = None
        if self.ready:
            key = _asset_image_key(asset)
            script = 'prismCards.updateAsset(' + json.dumps(dict(asset, thumbnail=key), ensure_ascii=False)
            script += ',' + json.dumps({key:asset['thumbnail']}) + ')'
            trace_record('material_cards_patch', material=record['id'], bytes=len(script.encode('utf-8')))
            self.web.page().runJavaScript(script)
            if self._virtual:
                # The virtual DOM keeps a bounded visible-image cache. Retain
                # metadata here; the existing thumbnail provider can reload it.
                asset['thumbnail'] = ''
                asset['thumbnailPending'] = True

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
        self._asset_lookup = {asset['id']: asset for asset in self.assets}
        for asset in self.assets:
            key = _asset_image_key(asset)
            assets.append(dict(asset, thumbnail=key))
        data = dict(assets=assets, selected=self._dom_id(self.selected_id),
                    selectedIds=[self._dom_id(i) for i in self.selected_ids],
                    scroll=self.scroll_position, generation=self.generation,
                    bounded=self._bounded)
        data['virtual'] = self._virtual
        script = 'prismCards.setData(' + json.dumps(data, ensure_ascii=False) + ')'
        trace_record('material_cards_payload', records=len(self.assets), bytes=len(script.encode('utf-8')))
        self.web.page().runJavaScript(script)
        self.set_layout(self.mode, self.card_size)

    @measured('materials.cards.images')
    def _send_images(self, ids):
        pending = 0
        lookup = getattr(self, '_asset_lookup', {})
        requested = [lookup[key] for key in ids if key in lookup]
        for asset in requested:
            if asset.get('thumbnailPending'):
                pending += 1
                actual = self._dom_to_id.get(asset['id'])
                if actual:
                    self.thumbnail_requested.emit(actual)
        # Acknowledge each pending record without retransmitting the same white
        # placeholder PNG hundreds of times. The worker patches its real image.
        images = {_asset_image_key(asset):('' if asset.get('thumbnailPending') else asset['thumbnail'])
                  for asset in requested}
        script = 'prismCards.receiveImages('+json.dumps(images)+','+str(self.generation)+')'
        trace_record('material_cards_images', images=len(images),
                     requested=len(ids), pending=pending,
                     ready=len(images) - pending,
                     bytes=len(script.encode('utf-8')), generation=self.generation)
        self.web.page().runJavaScript(script)

    def set_layout(self, mode, size):
        self.mode = 'list' if mode == 'list' else 'grid'
        self.card_size = max(96, min(320, int(size)))
        if self.ready:
            self.web.page().runJavaScript(
                'prismCards.setLayout(' + json.dumps(self.mode) + ',' + str(self.card_size) + ')')

    def select_record(self, record_id, additive=False):
        if record_id not in self._record_ids:
            return
        self._selection_anchor = record_id
        clicked_id = record_id
        if additive:
            if record_id in self.selected_ids:
                self.selected_ids.remove(record_id)
                record_id = next(iter(self.selected_ids),None)
            else:
                self.selected_ids.add(record_id)
        else:
            self.selected_ids = {record_id}
        self.selected_id = record_id
        # Finish the local toggle first. The model library mirrors its
        # cross-page selection synchronously from this signal; toggling again
        # afterwards would remove the just-added model from the visible set.
        self.selection_toggled.emit(clicked_id, bool(additive))
        self.selection_changed.emit(self.selected_id)

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
