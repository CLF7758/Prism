"""Synthetic canvas check; does not modify a project or user settings.

Run: python tools/canvas_stress.py --items 10000 --output result.json
"""
import argparse
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PyQt6 import QtCore, QtGui, QtWidgets
from prism import image_decode, lod
from prism.canvas_overview import CanvasOverviewRenderer
from prism.canvas_navigation import CanvasNavigationMixin
from prism.items import PrismPixmapItem
from prism.scene import PrismGraphicsScene


class StressView(CanvasNavigationMixin, QtWidgets.QGraphicsView):
    def __init__(self, scene):
        super().__init__(scene)
        self.scene = scene
        self._cached_scene_bounds = None
        self.previous_transform = None
        self.resize(900, 700)
        self._recalc_timer = QtCore.QTimer(self)
        self._interaction_timer = QtCore.QTimer(self)
        self._interaction_timer.setSingleShot(True)
        self._interaction_timer.setInterval(120)
        self._interaction_timer.timeout.connect(self._end_interaction)
        self._overview_renderer = CanvasOverviewRenderer(self)

    def paintEvent(self, event):
        self.scene._viewport_items.sync(self)
        if self._overview_renderer.eligible():
            painter = QtGui.QPainter(self.viewport())
            try:
                self._overview_renderer.paint(painter)
            finally:
                painter.end()
        else:
            super().paintEvent(event)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--items', type=int, default=10000)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--image-side', type=int, default=1600)
    parser.add_argument('--scale', type=float, default=0.003)
    args = parser.parse_args()
    if args.items <= 0 or args.image_side <= 0 or args.scale <= 0:
        parser.error('--items, --image-side and --scale must be positive')
    app = QtWidgets.QApplication([])
    scene = PrismGraphicsScene(QtGui.QUndoStack())
    view = StressView(scene)
    preview_side = min(192, args.image_side)
    preview = QtGui.QImage(preview_side, max(1, round(preview_side*9/16)), QtGui.QImage.Format.Format_RGB32)
    preview.fill(QtGui.QColor('#405080'))
    columns = 150
    created = []
    requested = []
    image_decode.request_display = lambda *a, **kw: requested.append(kw)
    with tempfile.TemporaryDirectory(prefix='prism-stress-') as folder:
        filename = str(Path(folder) / 'original.png')
        assert preview.save(filename)
        scene.blockSignals(True)
        start = time.perf_counter()
        for index in range(args.items):
            item = PrismPixmapItem(QtGui.QImage())
            item._source_path = filename
            item._image_size = QtCore.QSize(args.image_side, max(1, round(args.image_side*9/16)))
            item.reset_crop()
            item._preview_pixmap = QtGui.QPixmap.fromImage(preview)
            item.setPos((index % columns)*args.image_side*1.125,
                        (index // columns)*args.image_side*0.6875)
            scene.addItem(item)
            created.append(item)
        setup_seconds = time.perf_counter() - start
        scene.blockSignals(False)
        bounds = scene.itemsBoundingRect()
        scene.setSceneRect(bounds)
        view.setSceneRect(bounds)
        view.setTransform(QtGui.QTransform.fromScale(args.scale, args.scale))
        view.centerOn(bounds.center())
        start = time.perf_counter()
        view.show()
        app.processEvents()
        first_frame_ms = (time.perf_counter()-start)*1000
        frames = []
        for _ in range(10):
            start = time.perf_counter()
            view.viewport().repaint()
            app.processEvents()
            frames.append((time.perf_counter() - start)*1000)
        pan_frames = []
        for _ in range(10):
            start = time.perf_counter()
            view.pan(QtCore.QPointF(8, 0))
            view.viewport().repaint()
            app.processEvents()
            pan_frames.append((time.perf_counter()-start)*1000)
        view._end_interaction()
        view.viewport().repaint()
        app.processEvents()
        edits = []
        for item in created[:10]:
            start = time.perf_counter()
            old = item.sceneBoundingRect()
            item.moveBy(args.image_side/16, args.image_side/16)
            scene.changed.emit([old, item.sceneBoundingRect()])
            view.viewport().repaint()
            app.processEvents()
            edits.append((time.perf_counter()-start)*1000)
        scans = []
        for _ in range(5):
            start = time.perf_counter()
            scene.itemsBoundingRect()
            scans.append((time.perf_counter() - start)*1000)
        view._content_bounds()
        start = time.perf_counter()
        for _ in range(1000):
            view._content_bounds()
        cache_ms = (time.perf_counter()-start)*1000
        retained = sum(item._preview_pixmap is not None for item in created)
        result = {
            'items': args.items,
            'image_side': args.image_side,
            'scale': args.scale,
            'setup_seconds': round(setup_seconds, 3),
            'display_cache_bytes': lod.cache().bytes,
            'display_budget_bytes': lod.cache().budget,
            'retained_previews': retained,
            'evicted_previews': args.items-retained,
            'decode_requests_during_overview': len(requested),
            'overview_first_frame_ms': round(first_frame_ms, 3),
            'overview_cache_bytes': view._overview_renderer.frame[0].sizeInBytes() if view._overview_renderer.frame else 0,
            'overview_frame_median_ms': round(statistics.median(frames), 3),
            'overview_frame_max_ms': round(max(frames), 3),
            'pan_frame_median_ms': round(statistics.median(pan_frames), 3),
            'dirty_update_median_ms': round(statistics.median(edits), 3),
            'bounds_scan_median_ms': round(statistics.median(scans), 3),
            '1000_cached_bounds_calls_ms': round(cache_ms, 3),
            'platform': QtWidgets.QApplication.platformName(),
            'note': 'Synthetic metadata and solid previews; excludes disk decode, GPU and real project load.'
        }
        assert result['display_cache_bytes'] <= result['display_budget_bytes']
        assert not requested, 'Tiny overview unexpectedly requested decoding'
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(json.dumps(result, indent=2))
        view.hide()
        scene.clear()


if __name__ == '__main__':
    main()
