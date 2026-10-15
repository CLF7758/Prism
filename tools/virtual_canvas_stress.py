"""Read a generated large SQLite board and measure actual editing-object residency."""
import argparse
import faulthandler
import base64
import json
from pathlib import Path
import sqlite3
import statistics
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--items', type=int, default=30000)
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    faulthandler.dump_traceback_later(45, repeat=True)
    if args.items <= 0:
        parser.error('--items must be positive')
    sys.path.insert(0, str(args.source_root))
    from PyQt6 import QtCore, QtGui, QtWidgets
    from prism import image_decode, lod
    from prism.canvas_navigation import CanvasNavigationMixin
    from prism.canvas_overview import CanvasOverviewRenderer
    from prism.config import PrismSettings
    from prism.fileio.sql import SQLiteIO
    from prism.fileio.schema import SCHEMA, USER_VERSION, APPLICATION_ID
    from prism.scene import PrismGraphicsScene

    class View(CanvasNavigationMixin, QtWidgets.QGraphicsView):
        def __init__(self, scene):
            super().__init__(scene)
            self.scene = scene
            self._cached_scene_bounds = None
            self.previous_transform = None
            self.resize(900, 700)
            self._recalc_timer = QtCore.QTimer(self)
            self._interaction_timer = QtCore.QTimer(self)
            self._interaction_timer.setSingleShot(True)
            self._interaction_timer.timeout.connect(self._end_interaction)
            self._overview_renderer = CanvasOverviewRenderer(self)
        def paintEvent(self, event):
            manager = getattr(self.scene, '_viewport_items', None)
            if manager is not None:
                manager.sync(self)
            if self._overview_renderer.eligible():
                painter = QtGui.QPainter(self.viewport())
                try:
                    self._overview_renderer.paint(painter)
                finally:
                    painter.end()
            else:
                super().paintEvent(event)

    app = QtWidgets.QApplication([])
    with tempfile.TemporaryDirectory(prefix='prism-virtual-stress-') as folder:
        folder = Path(folder)
        PrismSettings.get_settings_dir = staticmethod(lambda: str(folder/'settings'))
        scene = PrismGraphicsScene(QtGui.QUndoStack())
        source = QtGui.QImage(1600, 900, QtGui.QImage.Format.Format_ARGB32)
        source.fill(QtGui.QColor('#405080'))
        buffer = QtCore.QBuffer(); buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
        assert source.save(buffer, 'PNG')
        blob = bytes(buffer.data())
        data = json.dumps({'imageWidth': 1600, 'imageHeight': 900, 'filename': 'generated.png',
            'displayOverview': [8, 4, base64.b64encode(bytes([64, 80, 128, 255])*32).decode('ascii')]})
        archive = folder/'generated.prism'
        connection = sqlite3.connect(archive)
        for schema in SCHEMA:
            connection.execute(schema)
        connection.execute(f'PRAGMA user_version={USER_VERSION}')
        connection.execute(f'PRAGMA application_id={APPLICATION_ID}')
        connection.executemany('INSERT INTO items(id,type,x,y,z,scale,rotation,flip,data) VALUES (?,"pixmap",?,?,0,1,0,1,?)',
            ((i+1, (i%150)*1800, (i//150)*1100, data) for i in range(args.items)))
        connection.executemany('INSERT INTO sqlar(name,item_id,mode,sz,data) VALUES (?,?,420,?,?)',
            ((str(i+1)+'.png', i+1, len(blob), blob) for i in range(args.items)))
        connection.commit(); connection.close()
        print('Generated archive', flush=True)
        reader = SQLiteIO(str(archive), scene, readonly=True)
        started = time.perf_counter()
        reader.read(); reader._close_connection()
        print('Read metadata', flush=True)
        read_seconds = time.perf_counter()-started
        chunks = []
        while not scene.items_to_add.empty():
            started = time.perf_counter()
            scene.add_queued_items(max_items=128, time_budget_ms=6)
            chunks.append((time.perf_counter()-started)*1000)
            app.processEvents()
        manager = getattr(scene, '_viewport_items', None)
        print('Installed logical assets', flush=True)
        view = View(scene)
        bounds = scene.itemsBoundingRect()
        view.setSceneRect(bounds.adjusted(-1000, -1000, 1000, 1000))
        view.setTransform(QtGui.QTransform.fromScale(.003, .003))
        view.centerOn(bounds.center())
        started = time.perf_counter()
        view.show(); app.processEvents()
        print('Painted overview', flush=True)
        cold_ms = (time.perf_counter()-started)*1000
        warm = []
        for _ in range(10):
            started = time.perf_counter()
            view.viewport().repaint(); app.processEvents()
            warm.append((time.perf_counter()-started)*1000)
        overview_objects = len(manager.materialized) if manager is not None else scene._item_count
        image_decode.request_display = lambda *a, **kw: None
        view.setTransform(QtGui.QTransform())
        view.centerOn(QtCore.QPointF(800, 450))
        started = time.perf_counter()
        for _ in range(100):
            if manager is not None:
                manager.sync(view)
            app.processEvents()
            if manager is None or any(r._graphics is not None for r in manager.records.values()):
                break
        near_ms = (time.perf_counter()-started)*1000
        print('Materialized first viewport', flush=True)
        near_ids = {id(r) for r in manager.materialized} if manager is not None else set()
        view.centerOn(QtCore.QPointF(bounds.right()-800, bounds.bottom()-450))
        for _ in range(5):
            if manager is not None:
                manager.sync(view)
            app.processEvents()
        recycled = len(near_ids-{id(r) for r in manager.materialized}) if manager is not None else 0
        logical_count = len(list(scene.items_for_save()))
        native_count = len(QtWidgets.QGraphicsScene.items(scene))
        result = {'items': args.items, 'archive_bytes': archive.stat().st_size,
            'metadata_read_seconds': round(read_seconds, 3),
            'install_total_ms': round(sum(chunks), 3), 'install_max_turn_ms': round(max(chunks), 3),
            'logical_asset_count': logical_count, 'far_editing_objects': overview_objects,
            'near_editing_objects': len(manager.materialized) if manager is not None else scene._item_count,
            'total_native_objects_after_pan': native_count, 'recycled_near_objects': recycled,
            'overview_cold_ms': round(cold_ms, 3), 'overview_warm_median_ms': round(statistics.median(warm), 3),
            'first_near_materialize_ms': round(near_ms, 3), 'display_cache_bytes': lod.cache().bytes,
            'platform': app.platformName(),
            'note': 'Generated SQLite board with repeated valid source PNG and 8px previews; not 30000 distinct real images, full main-window latency or GPU FPS.'}
        assert logical_count == args.items
        if manager is not None:
            assert overview_objects == 0
            assert len(manager.materialized) <= 512
            assert native_count <= 1030
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(json.dumps(result, indent=2))
        view.hide(); scene.clear()


if __name__ == '__main__':
    main()
