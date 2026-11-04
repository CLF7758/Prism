"""Best-effort evidence beside the asset renderer, never a replacement renderer.

Counts are content draw operations, NOT model rows. Cached images without a
bounded diagnostic manifest have unknown content coverage. Geometry is in
physical paint-device pixels. Non-axis-aligned transforms deliberately degrade
to unknown; their bounding boxes are not advertised as exact coverage.
"""
from contextlib import contextmanager
import math
import time
import uuid
from PyQt6 import QtCore, QtGui
from prism.presentation_evidence import model_item_id


# Cooperative limits, not a hard latency guarantee for an individual Qt call.
MAX_ALPHA_PIXELS = 65536
MAX_FRAME_ENTRIES = 4096
FINISH_OPERATIONS = 20000
FINISH_SECONDS = .025
REGION_CELL = 64


class WorkBudget:
    def __init__(self):
        self.started = time.perf_counter()
        self.operations = 0
        self.exhausted = False

    def take(self, count=1):
        if (self.exhausted or self.operations + count > FINISH_OPERATIONS
                or time.perf_counter() - self.started >= FINISH_SECONDS):
            self.exhausted = True
            return False
        self.operations += count
        return True


def alpha_kind(image, source):
    """Prove uniform alpha only; never call a target rectangle opaque by fiat."""
    source = QtCore.QRectF(source)
    if source.isEmpty():
        return 'transparent'
    if not QtCore.QRectF(image.rect()).contains(source):
        return 'unknown'
    if not image.hasAlphaChannel():
        return 'opaque'
    # Include filter neighbours; inspecting a cropped rectangle alone can
    # miss adjacent alpha sampled by SmoothPixmapTransform.
    rect = source.adjusted(-1, -1, 1, 1).toAlignedRect().intersected(image.rect())
    # Conversion and alpha inspection are bounded by the whole source size.
    if image.width() * image.height() > MAX_ALPHA_PIXELS:
        return 'unknown'
    if isinstance(image, QtGui.QPixmap):
        image = image.toImage()
    image = image.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
    bits = image.constBits()
    bits.setsize(image.sizeInBytes())
    data = bytes(bits)
    low, high = 255, 0
    stride = image.bytesPerLine()
    for y in range(rect.top(), rect.bottom()+1):
        alpha = data[y*stride+rect.left()*4+3:y*stride+(rect.right()+1)*4:4]
        low, high = min(low, min(alpha)), max(high, max(alpha))
    return 'transparent' if high == 0 else 'opaque' if low == 255 else 'partial'


def content_target(card, item):
    """Independent delegate alignment + shared renderer fit, not observed slivers.

    Flip is about the fitted target's centre, so it leaves this region unchanged.
    Scene/layer transforms and viewport DPR are applied by the caller.
    """
    rect = QtCore.QRectF(card.boundingRect().toAlignedRect())
    crop = getattr(item, 'crop', QtCore.QRectF())
    size = QtCore.QSizeF(crop.size() if not crop.isEmpty() else
                        getattr(item, '_image_size', QtCore.QSize()))
    if size.isEmpty():
        pm = getattr(item, '_preview_pixmap', None)
        size = QtCore.QSizeF(pm.size()) if pm is not None else QtCore.QSizeF(1, 1)
    target = QtCore.QRectF(QtCore.QPointF(), size.scaled(
        rect.size(), QtCore.Qt.AspectRatioMode.KeepAspectRatio))
    target.moveCenter(rect.center())
    return target.translated(card._x, card._y)


def safe_record(evidence, event, **values):
    try:
        if evidence is not None and evidence.active:
            # Installation final=True must never turn a render into a terminal.
            values['final'] = False
            evidence.record(event, **dict(evidence.fields(), **values))
    except Exception:
        pass


def evidence_for(host):
    try:
        evidence = getattr(host.model(), 'presentation_evidence', None)
        return evidence if evidence is not None and evidence.active else None
    except Exception:
        return None


def rect_values(rect):
    return [rect.x(), rect.y(), rect.width(), rect.height()]


def transform_values(t):
    return [t.m11(), t.m12(), t.m13(), t.m21(), t.m22(), t.m23(),
            t.m31(), t.m32(), t.m33()]


def axis_aligned(t):
    return (t.isAffine() and abs(t.m12()) < 1e-9 and abs(t.m21()) < 1e-9
            and abs(t.m11()) > 1e-12 and abs(t.m22()) > 1e-12)


def path_rect(rect):
    path = QtGui.QPainterPath()
    path.addRect(QtCore.QRectF(rect))
    return path


def region_path(region, scale=1.):
    path = QtGui.QPainterPath()
    # QRegion's path conversion preserves disjoint paint-event rectangles.
    path.addRegion(region)
    return QtGui.QTransform.fromScale(scale, scale).map(path)


def path_values(path):
    return [[[p.x(), p.y()] for p in polygon]
            for polygon in path.toFillPolygons()]


def _geometry(painter, target, sink):
    t = painter.combinedTransform()
    if (not axis_aligned(t) or painter.compositionMode()
            != QtGui.QPainter.CompositionMode.CompositionMode_SourceOver):
        return None, t
    path = t.map(path_rect(target))
    if painter.hasClipping():
        path = path.intersected(t.map(painter.clipPath()))
    path = path.intersected(sink.clip)
    return path, t


class Resource:
    def __init__(self, host, evidence, image, kind):
        self.evidence = evidence
        self.identity = evidence.fields()
        key = tuple(self.identity.get(k) for k in ('session_id', 'query_id', 'source_revision'))
        pool = getattr(host, '_asset_resource_manifest_budget', None)
        if pool is None or pool['key'] != key:
            pool = dict(key=key, used=0)
            host._asset_resource_manifest_budget = pool
        self.pool = pool
        self.kind = kind
        self.resource_id = uuid.uuid4().hex
        self.clip = path_rect(image.rect())
        self.diagnostic = bool(evidence.diagnostic)
        self.budget = max(0, int(evidence.id_budget))
        self.entries = []
        self.background = None
        self.generated = 0
        self.unknown = 0
        self.incomplete = False
        self.complete = False
        if self.diagnostic and kind == 'tile':
            if any(item is not host._layer and item.isVisible() for item in host.scene().items()):
                self.unknown += 1
                self.incomplete = True

    def accept(self, path, values):
        if path is None:
            self.unknown += 1
            self.incomplete = True
            return
        if path.isEmpty():
            return
        self.generated += int(values['kind'] == 'content' and values.get('nonempty_contribution', True))
        if self.diagnostic:
            if self.pool['used'] < self.budget and len(self.entries) < MAX_FRAME_ENTRIES:
                self.entries.append((QtGui.QPainterPath(path), dict(values)))
                self.pool['used'] += 1
            else:
                self.incomplete = True


def resource_for(host, image, kind='tile'):
    evidence = evidence_for(host)
    if evidence is None:
        return None
    resource = getattr(image, '_asset_paint_resource', None)
    if resource is None:
        resource = Resource(host, evidence, image, kind)
        image._asset_paint_resource = resource
    return resource


@contextmanager
def resource_scope(host, image, kind='tile'):
    previous = getattr(host, '_asset_paint_sink', None)
    resource = None
    try:
        resource = resource_for(host, image, kind)
    except Exception:
        pass
    # Even an inactive/failed evidence scope must suppress viewport attribution
    # while Qt renders offscreen during an active viewport paint.
    host._asset_paint_sink = resource
    try:
        yield resource
    finally:
        host._asset_paint_sink = previous


def observe_background(host, image, brush):
    """Hook AFTER a real full-buffer fill, including DestinationOver fills.

    A solid opaque background is a bottom layer, not asset content. Transparent
    initial buffers have no such layer. Never change the actual renderer fill.
    """
    try:
        resource = resource_for(host, image)
        if resource is None:
            return
        brush = QtGui.QBrush(brush)
        if (brush.style() == QtCore.Qt.BrushStyle.SolidPattern
                and brush.color().alpha() == 255):
            resource.background = QtGui.QPainterPath(resource.clip)
        elif (brush.style() != QtCore.Qt.BrushStyle.NoBrush
              and (brush.style() != QtCore.Qt.BrushStyle.SolidPattern or brush.color().alpha() != 0)):
            resource.unknown += 1
            resource.incomplete = True
    except Exception:
        pass


def generated(host, image, *, complete=False):
    try:
        resource = resource_for(host, image)
        if resource is None:
            return
        resource.complete = complete
        safe_record(resource.evidence, 'asset_resource_generated',
                    resource_id=resource.resource_id, resource_kind=resource.kind,
                    generated_content_operations=resource.generated,
                    manifest_entries=len(resource.entries), complete=complete,
                    incomplete=resource.incomplete, viewport_drawn=0,
                    resource_source_revision=resource.identity.get('source_revision'),
                    resource_query_id=resource.identity.get('query_id'))
    except Exception:
        pass


def installed(host, image, **fields):
    try:
        resource = getattr(image, '_asset_paint_resource', None)
        safe_record(evidence_for(host), 'asset_cache_installed',
                    resource_id=resource.resource_id if resource else None,
                    resource_source_revision=resource.identity.get('source_revision') if resource else None,
                    resource_query_id=resource.identity.get('query_id') if resource else None,
                    viewport_drawn=0, **fields)
    except Exception:
        pass


@contextmanager
def delegate_scope(host, card, painter=None):
    previous = getattr(host, '_asset_paint_row', None)
    host._asset_paint_row = card.row
    sink = getattr(host, '_asset_paint_sink', None)
    before = getattr(host, '_asset_paint_observations', 0)
    if isinstance(sink, Frame):
        sink.paint_calls += 1
    try:
        yield
    finally:
        # A delegate without a LOD hook (text, custom, or placeholder) cannot
        # prove image content. Do not infer success from its return value.
        try:
            if sink is not None and before == getattr(host, '_asset_paint_observations', 0):
                sink.unknown += 1
                sink.incomplete = True
                if isinstance(sink, Frame):
                    sink.drawn_ids_incomplete = True
        except Exception:
            pass
        host._asset_paint_row = previous


def entry_current(host, entry, identity):
    try:
        item = host.model().items[entry['row']]
        return (entry.get('actual_source_revision') is not None
                and entry.get('actual_source_revision') == getattr(item, '_display_source_revision', None)
                and entry.get('query_id') == identity.get('query_id')
                and entry.get('source_revision') == identity.get('source_revision')
                and entry.get('item_identity') == repr(host._row_identity(item))
                and entry.get('item_geometry') == list(host._source_geometry(item)))
    except Exception:
        return False


def observe_pixmap(host, painter, item, target, pixmap, source, pixels,
                   actual_source_revision=None):
    """Called after the actual drawPixmap, with its actual painter state."""
    try:
        sink = getattr(host, '_asset_paint_sink', None)
        if sink is None or evidence_for(host) is not sink.evidence:
            return
        host._asset_paint_observations = getattr(host, '_asset_paint_observations', 0) + 1
        if painter.opacity() <= 0:
            return
        path, t = _geometry(painter, target, sink)
        alpha = alpha_kind(pixmap, source)
        if alpha == 'transparent':
            return  # SourceOver with zero alpha contributes and occludes nothing.
        if alpha in ('partial', 'unknown') or painter.opacity() < 1.-1e-6:
            sink.unknown += 1
            sink.incomplete = True
            if isinstance(sink, Frame) and alpha in ('partial', 'unknown'):
                sink.drawn_ids_incomplete = True
        current = getattr(item, '_display_source_revision', None)
        size = getattr(item, '_image_size', QtCore.QSize())
        crop = getattr(item, 'crop', QtCore.QRectF())
        original = crop.size() if not crop.isEmpty() else size
        required = [min(abs(t.m11())*target.width(), original.width()),
                    min(abs(t.m22())*target.height(), original.height())]
        ready = (path is not None and alpha == 'opaque'
                 and painter.opacity() >= 1.-1e-6 and current is not None
                 and actual_source_revision == current
                 and original.width() > 0 and original.height() > 0
                 and source.width()+1e-6 >= required[0]
                 and source.height()+1e-6 >= required[1])
        values = dict(kind='content', row=getattr(host, '_asset_paint_row', None),
                      ready=ready, revision_current=(current is not None and actual_source_revision == current),
                      category='direct',
                      query_id=sink.identity.get('query_id'),
                      source_revision=sink.identity.get('source_revision'),
                      actual_source_revision=actual_source_revision,
                      expected_item_revision=current, pixmap_size=[pixmap.width(), pixmap.height()],
                      required_pixels=pixels, required_crop_pixels=required,
                      source_crop=rect_values(source), target=rect_values(target),
                      opacity=painter.opacity(), transform=transform_values(t),
                      alpha_coverage=alpha, nonempty_contribution=alpha == 'opaque',
                      occludes=alpha == 'opaque' and painter.opacity() >= 1.-1e-6,
                      geometry_exact=path is not None and alpha == 'opaque')
        if sink.diagnostic:
            values['original_asset_id'] = model_item_id(item)
            values['item_identity'] = repr(host._row_identity(item))
            values['item_geometry'] = list(host._source_geometry(item))
        sink.accept(path, values)
    except Exception:
        pass


def observe_missing(host, painter, target):
    try:
        sink = getattr(host, '_asset_paint_sink', None)
        if sink is None or evidence_for(host) is not sink.evidence:
            return
        host._asset_paint_observations = getattr(host, '_asset_paint_observations', 0) + 1
        path, t = _geometry(painter, target, sink)
        sink.accept(path, dict(kind='placeholder', row=getattr(host, '_asset_paint_row', None),
                              ready=False, category='placeholder', transform=transform_values(t)))
    except Exception:
        pass


def observe_image(host, painter, target, image, source=None, *, category='tile', stale=False,
                  render_revision=None, force_unknown_geometry=False):
    """Replay only generated manifest regions, never the whole transparent buffer."""
    try:
        sink = getattr(host, '_asset_paint_sink', None)
        if sink is None or evidence_for(host) is not sink.evidence:
            return
        if isinstance(sink, Frame):
            sink.paint_calls += 1
            sink.cache_draw_calls += 1
        if painter.opacity() <= 0:
            return
        path, t = _geometry(painter, target, sink)
        if force_unknown_geometry:
            path = None
        resource = getattr(image, '_asset_paint_resource', None)
        if path is not None and path.isEmpty():
            return
        source = QtCore.QRectF(image.rect()) if source is None else source
        if source.isEmpty() or target.isEmpty():
            return
        mapping = QtGui.QTransform()
        mapping.translate(target.x(), target.y())
        mapping.scale(target.width()/source.width(), target.height()/source.height())
        mapping.translate(-source.x(), -source.y())
        if path is not None and resource is not None and resource.background is not None:
            background = t.map(mapping.map(resource.background.intersected(path_rect(source)))).intersected(path)
            sink.accept(background, dict(kind='occlusion', row=None, category='background',
                ready=False, revision_current=False, opacity=painter.opacity(),
                occludes=painter.opacity() >= 1.-1e-6, resource_id=resource.resource_id))
        if (path is None or resource is None or not resource.diagnostic
                or resource.incomplete):
            sink.unknown += 1
            sink.incomplete = True
            if isinstance(sink, Frame):
                sink.drawn_ids_incomplete = True
                sink.categories[category] = sink.categories.get(category, 0)+1
                sink.fallback += int(stale or category in ('ancestor', 'child', 'old_tile', 'group'))
            return
        outdated = (stale or resource.identity.get('query_id') != sink.identity.get('query_id')
                    or resource.identity.get('source_revision') != sink.identity.get('source_revision'))
        # Tile/composite resolution must also satisfy the real destination.
        sufficient = (source.width()+1e-6 >= abs(t.m11())*target.width()
                      and source.height()+1e-6 >= abs(t.m22())*target.height())
        replay_budget = WorkBudget()
        for area, entry in resource.entries:
            if not replay_budget.take():
                sink.unknown += 1
                sink.incomplete = True
                if isinstance(sink, Frame):
                    sink.drawn_ids_incomplete = True
                break
            clipped = t.map(mapping.map(area.intersected(path_rect(source)))).intersected(path)
            if clipped.isEmpty():
                continue
            values = dict(entry)
            entry_outdated = outdated or not entry_current(host, entry, sink.identity)
            inherited = entry.get('category', 'direct')
            values['category'] = inherited if category in ('tile', 'incremental') and inherited != 'direct' else category
            values['ready'] = (entry.get('ready', False) and not entry_outdated and sufficient
                               and painter.opacity() >= 1.-1e-6
                               and values['category'] not in ('ancestor', 'old_tile', 'group'))
            values['stale'] = entry_outdated
            values['revision_current'] = not entry_outdated
            values['render_revision'] = render_revision
            values['resource_id'] = resource.resource_id
            values['resource_complete'] = resource.complete
            values['resource_query_id'] = resource.identity.get('query_id')
            values['resource_source_revision'] = resource.identity.get('source_revision')
            values['resource_crop'] = rect_values(source)
            values['resource_transform'] = transform_values(t)
            values['opacity'] = entry.get('opacity', 1.) * painter.opacity()
            values['occludes'] = entry.get('occludes', False) and painter.opacity() >= 1.-1e-6
            if values['opacity'] < 1.-1e-6:
                values['ready'] = False
                sink.incomplete = True
            sink.accept(clipped, values)
    except Exception:
        pass


class Frame:
    def __init__(self, host, evidence, event):
        self.host, self.evidence = host, evidence
        self.identity = evidence.fields()
        self.frame_id = uuid.uuid4().hex
        self.diagnostic = bool(evidence.diagnostic)
        self.budget = min(MAX_FRAME_ENTRIES, max(0, int(evidence.id_budget)))
        self.drawn_ids_incomplete = False
        self.viewport = path_rect(QtCore.QRectF(host.viewport().rect()))
        dpr = host.viewport().devicePixelRatioF()
        self.viewport = QtGui.QTransform.fromScale(dpr, dpr).map(self.viewport)
        self.clip = region_path(event.region(), dpr).intersected(self.viewport)
        self.full = self.viewport.subtracted(self.clip).isEmpty()
        self.paint_calls = self.cache_draw_calls = self.drawn = self.ready = self.current = 0
        self.fallback = self.placeholder = self.unknown = 0
        self.incomplete = False
        self.categories = {}
        self.actual_revisions = {}
        self.actual_revisions_complete = True
        self.entries = []
        self.candidates = None
        self.viewport_candidates = None
        self.candidate_census_complete = False
        if self.diagnostic and any(item is not host._layer and item.isVisible()
                                   for item in host.scene().items()):
            self.unknown += 1
            self.incomplete = True  # Scene annotations are not instrumented content.
            self.drawn_ids_incomplete = True
        if self.diagnostic and axis_aligned(host.viewportTransform()):
            candidates = host.visible_cards(host.mapToScene(host.viewport().rect()).boundingRect(),
                                            limit=self.budget+1)
            if len(candidates) > self.budget:
                self.incomplete = True
            else:
                self.candidate_census_complete = True
                self.viewport_candidates = len(candidates)
                transform = QtGui.QTransform(host.viewportTransform())
                transform = transform * QtGui.QTransform.fromScale(dpr, dpr)
                self.candidates = sum(not transform.map(path_rect(c.sceneBoundingRect())).intersected(self.clip).isEmpty()
                                      for c in candidates)

    def accept(self, path, values):
        if path is None:
            self.unknown += 1
            self.incomplete = True
            self.drawn_ids_incomplete |= values.get('kind') == 'content'
            return
        if path.isEmpty():
            return
        kind = values['kind']
        category = values.get('category', 'direct')
        self.categories[category] = self.categories.get(category, 0)+1
        if kind == 'content' and values.get('nonempty_contribution', True):
            try:
                # No IDs are created or held by production. Resource.accept
                # never reaches this viewport/query-lifetime counter.
                asset_id = values.get('original_asset_id') if self.diagnostic else None
                self.evidence.observe('drawn', (asset_id,))
                if self.diagnostic and asset_id is None:
                    self.drawn_ids_incomplete = True
            except Exception:
                self.drawn_ids_incomplete = True
            self.drawn += 1
            self.ready += int(values.get('ready', False))
            self.current += int(values.get('revision_current', False))
            self.fallback += int(category in ('ancestor', 'child', 'old_tile', 'group') or values.get('stale', False))
            self.unknown += int(values.get('actual_source_revision') is None)
            revision = str(values.get('actual_source_revision'))
            if revision in self.actual_revisions or len(self.actual_revisions) < 32:
                self.actual_revisions[revision] = self.actual_revisions.get(revision, 0)+1
            else:
                self.actual_revisions_complete = False
        elif kind == 'placeholder':
            self.placeholder += 1
        if self.diagnostic:
            if len(self.entries) < self.budget:
                self.entries.append((QtGui.QPainterPath(path), dict(values)))
            else:
                self.incomplete = True

    def finish(self):
        if any(self.identity.get(k) != self.evidence.fields().get(k)
               for k in ('session_id', 'query_id', 'source_revision')):
            return
        work = WorkBudget()
        query_key = self.identity.get('session_id'), self.identity.get('query_id')
        lifetime = getattr(self.host, '_asset_drawn_id_state', None)
        if lifetime is None or lifetime['key'] != query_key:
            lifetime = dict(key=query_key, incomplete=False)
        lifetime['incomplete'] |= self.drawn_ids_incomplete
        self.host._asset_drawn_id_state = lifetime
        try:
            drawn_ids = self.evidence.snapshot('drawn')
        except Exception:
            drawn_ids = {}
        ids_complete = bool(drawn_ids.get('unique_rows_complete')) and not lifetime['incomplete']
        values = dict(ever_drawn_unique_ids=drawn_ids.get('unique_rows'),
                      ever_drawn_unique_ids_complete=ids_complete,
                      ever_drawn_unique_ids_budget_exhausted=drawn_ids.get('id_budget_exhausted', False),
                      ever_drawn_unique_ids_incomplete=not ids_complete,
                      ever_drawn_unique_ids_basis=('exact' if ids_complete else 'lower_bound'
                          if self.diagnostic else 'not_collected'),
                      ever_drawn_unique_ids_units='stable_asset_ids',
                      ever_drawn_unique_ids_scope='query_lifetime_actual_viewport_contributions',
                      model_rows=len(self.host.cards), viewport_candidates=self.candidates,
                      candidate_census_complete=self.candidate_census_complete,
                      candidate_units='model_rows', paint_calls=self.paint_calls,
                      viewport_drawn=self.drawn, current_revision_coverage=self.current,
                      content_ready=self.ready, fallback=self.fallback,
                      placeholder=self.placeholder, unknown=self.unknown,
                      count_units='content_draw_operations', counts_are_lower_bounds=True,
                      frame_id=self.frame_id,
                      capture_id=self.frame_id, capture_boundary='qt_paint_event_completed',
                      display_confirmed=False, coverage_scope='paint_clip',
                      visibility_basis='observed_qt_draw_operations',
                      full_viewport_repaint=self.full, incomplete=self.incomplete,
                      categories=self.categories, cache_draw_calls=self.cache_draw_calls,
                      actual_source_revision_histogram=self.actual_revisions,
                      actual_source_revision_histogram_complete=self.actual_revisions_complete,
                      category_units='content_operations_or_unknown_cache_draw_calls',
                      placeholder_basis='missing_content_slot_or_layout_placeholder',
                      effective_clip=path_values(self.clip), geometry_units='device_pixels',
                      viewport_transform=transform_values(self.host.viewportTransform()))
        self.host._asset_paint_capture_id = self.frame_id
        snapshot = dict(values, coverage_scope='current_viewport',
                        viewport_candidates=self.viewport_candidates,
                        frame_viewport_drawn=self.drawn, viewport_drawn=None,
                        frame_counts=dict(viewport_drawn=self.drawn, content_ready=self.ready,
                                          current_revision_coverage=self.current, fallback=self.fallback,
                                          placeholder=self.placeholder, unknown=self.unknown,
                                          count_units='content_draw_operations'),
                        content_ready=None, current_revision_coverage=None,
                        fallback=None, placeholder=None, unknown=None,
                        count_units='unique_model_rows',
                        accumulated=False, current_viewport_exact=False)
        if self.diagnostic:
            key = (self.identity.get('session_id'), self.identity.get('query_id'),
                   self.identity.get('source_revision'), tuple(transform_values(self.host.viewportTransform())),
                   tuple(transform_values(self.host._layer.sceneTransform())),
                   tuple(rect_values(self.host.viewport().rect())))
            state = getattr(self.host, '_asset_visibility_state', None)
            if state is None or state['key'] != key or self.full:
                state = dict(key=key, entries=[], incomplete=not self.full)
            retained = []
            for area, entry in state['entries']:
                if not work.take():
                    break
                area = area.subtracted(self.clip)
                if not area.isEmpty():
                    retained.append((area, entry))
            retained.extend(self.entries)
            overflow = len(retained) > self.budget
            state['entries'] = retained[:self.budget]
            state['incomplete'] |= self.incomplete or overflow or bool(self.unknown) or work.exhausted
            rows, fallback_rows, placeholder_rows = set(), set(), set()
            for area, entry in state['entries']:
                if not work.take():
                    break
                if entry['kind'] == 'content':
                    if not entry_current(self.host, entry, self.identity):
                        entry['ready'] = False
                        entry['stale'] = True
                    if entry.get('row') is not None and entry.get('nonempty_contribution', True):
                        rows.add(entry['row'])
                if entry.get('row') is not None:
                    if entry.get('stale') or entry.get('category') in ('ancestor', 'child', 'old_tile', 'group'):
                        fallback_rows.add(entry['row'])
                    if entry['kind'] == 'placeholder':
                        placeholder_rows.add(entry['row'])
            state['incomplete'] |= work.exhausted
            ready_rows, current_rows = self._coverage(state, rows, work)
            diagnostic_regions = []
            for area, entry in state['entries']:
                if not work.take(max(1, area.elementCount())):
                    break
                diagnostic_regions.append(dict(entry, effective_clip=path_values(area),
                    ready_basis='draw_operation_not_final_visibility',
                    current_viewport_row_ready=entry.get('row') in ready_rows))
            state['incomplete'] |= work.exhausted
            self.host._asset_visibility_state = state
            snapshot.update(accumulated=True, incomplete=state['incomplete'],
                            current_viewport_exact=not state['incomplete'],
                            coverage_basis='lower_bound' if state['incomplete'] else 'exact',
                            viewport_drawn=len(rows), content_ready=len(ready_rows),
                            current_revision_coverage=len(current_rows),
                            fallback=len(fallback_rows), placeholder=len(placeholder_rows),
                            unknown=None if state['incomplete'] else 0,
                            unique_viewport_drawn=len(rows), unique_content_ready=len(ready_rows),
                            diagnostic_ids=sorted(rows), diagnostic_regions=diagnostic_regions,
                            diagnostic_regions_complete=len(diagnostic_regions) == len(state['entries']))
        closeout = dict(closeout_operations=work.operations,
                        closeout_operation_budget=FINISH_OPERATIONS,
                        closeout_budget_exhausted=work.exhausted,
                        closeout_elapsed_ms=(time.perf_counter()-work.started)*1000,
                        closeout_budget_basis='cooperative_not_hard_deadline')
        values.update(closeout, incomplete=self.incomplete or work.exhausted)
        snapshot.update(closeout)
        safe_record(self.evidence, 'asset_paint_frame', **values)
        safe_record(self.evidence, 'asset_visibility_snapshot', **snapshot)
        # Reuse the bounded renderer-side census, never queue/cache readiness.
        # Production without this census deliberately has no whole-view proof.
        if (self.diagnostic and snapshot['current_viewport_exact']
                and self.candidate_census_complete and not work.exhausted):
            index = self.host.currentIndex()
            focus_row = index.row() if index.isValid() else None
            return dict(visible=len(rows), covered=len(rows),
                        detailed=len(ready_rows), total=self.viewport_candidates,
                        focus_ready=(focus_row in ready_rows if focus_row in rows else None),
                        full_viewport_repaint=self.full,
                        capture_id=self.frame_id, basis='actual_current_viewport_coverage')

    def _coverage(self, state, rows, work):
        """Region-indexed painter-order coverage with bounded work, not rows x entries."""
        ready_rows, current_rows = set(), set()
        layer = self.host._layer.sceneTransform()
        view = self.host.viewportTransform()
        if state['incomplete']:
            return ready_rows, current_rows
        if not axis_aligned(view) or not axis_aligned(layer):
            state['incomplete'] = True
            return ready_rows, current_rows
        transform = layer * view * QtGui.QTransform.fromScale(self.host.viewport().devicePixelRatioF(),
                                                              self.host.viewport().devicePixelRatioF())
        grid = {}

        def cells(rect):
            left, right = math.floor(rect.left()/REGION_CELL), math.floor(rect.right()/REGION_CELL)
            top, bottom = math.floor(rect.top()/REGION_CELL), math.floor(rect.bottom()/REGION_CELL)
            count = (right-left+1)*(bottom-top+1)
            if not work.take(count):
                return ()
            return ((x, y) for y in range(top, bottom+1) for x in range(left, right+1))

        for index, (area, entry) in enumerate(state['entries']):
            if not work.take():
                break
            for cell in cells(area.boundingRect()):
                grid.setdefault(cell, []).append(index)
        for row in sorted(rows):
            if not work.take() or work.exhausted:
                break
            item = self.host.model().items[row]
            needed = transform.map(path_rect(content_target(self.host.cards[row], item))).intersected(self.viewport)
            if needed.isEmpty():
                continue
            relevant = set()
            for cell in cells(needed.boundingRect()):
                indices = grid.get(cell, ())
                if not work.take(len(indices)):
                    break
                relevant.update(indices)
            if work.exhausted:
                break
            coverage, current_coverage = QtGui.QPainterPath(), QtGui.QPainterPath()
            complete_row = True
            for index in sorted(relevant):
                if not work.take(3):
                    complete_row = False
                    break
                area, entry = state['entries'][index]
                area = area.intersected(needed)
                if area.isEmpty():
                    continue
                opaque = entry.get('occludes', False)
                current = (entry['kind'] == 'content' and entry.get('row') == row
                           and entry.get('revision_current') and not entry.get('stale')
                           and opaque)
                if current:
                    current_coverage = current_coverage.united(area)
                elif opaque:
                    current_coverage = current_coverage.subtracted(area)
                if current and entry.get('ready'):
                    coverage = coverage.united(area)
                elif opaque:
                    coverage = coverage.subtracted(area)
            if complete_row and work.take(2):
                if needed.subtracted(coverage).isEmpty():
                    ready_rows.add(row)
                if needed.subtracted(current_coverage).isEmpty():
                    current_rows.add(row)
        state['incomplete'] |= work.exhausted
        return ready_rows, current_rows


def begin_frame(host, event):
    try:
        evidence = evidence_for(host)
        frame = Frame(host, evidence, event) if evidence is not None else None
        host._asset_paint_sink = frame
        return frame
    except Exception:
        host._asset_paint_sink = None
        return None


def finish_frame(host, frame):
    host._asset_paint_sink = None
    try:
        if frame is not None and evidence_for(host) is frame.evidence:
            return frame.finish()
    except Exception:
        pass
