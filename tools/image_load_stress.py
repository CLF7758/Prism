"""Real PNG/JPEG decoding and SQLite opening benchmark, isolated from user data.

The files contain generated patterned pixels; they are not a user's real library.
Use --source-root to compare a saved source tree with exactly the same inputs.
"""
import argparse
import hashlib
import inspect
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--items', type=int, default=500)
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.items <= 100000:
        parser.error('--items must be between 1 and 100000')
    sys.path.insert(0, str(args.source_root))
    from PyQt6 import QtCore, QtGui, QtWidgets
    from PIL import Image
    import numpy as np
    from prism import image_decode, lod
    from prism.fileio.sql import SQLiteIO
    from prism.items import PrismPixmapItem
    from prism.scene import PrismGraphicsScene

    app = QtWidgets.QApplication([])
    scene = PrismGraphicsScene(QtGui.QUndoStack())
    files, originals = [], []
    with tempfile.TemporaryDirectory(prefix='prism-real-image-bench-') as folder:
        folder = Path(folder)
        generator = np.random.default_rng(20261013)
        started = time.perf_counter()
        for index in range(args.items):
            pixels = generator.integers(0, 256, (48, 64, 3), dtype=np.uint8)
            image = Image.fromarray(pixels).resize((1024, 768))
            suffix = '.jpg' if index % 2 else '.png'
            if suffix == '.png':
                image = image.convert('RGBA')
                image.putalpha(128 if index % 4 == 0 else 255)
            path = folder/f'{index:06}{suffix}'
            image.save(path)
            files.append(path)
            originals.append(hashlib.sha256(path.read_bytes()).hexdigest())
        generated_seconds = time.perf_counter()-started

        scene.blockSignals(True)
        for index, path in enumerate(files):
            item = PrismPixmapItem(QtGui.QImage(), str(path))
            item._source_path = str(path)
            item._image_size = QtCore.QSize(1024, 768)
            item.reset_crop()
            item.setPos((index % 25)*1152, (index//25)*896)
            scene.addItem(item)
        scene.blockSignals(False)
        archive = folder/'generated.prism'
        writer = SQLiteIO(str(archive), scene, create_new=True)
        started = time.perf_counter()
        writer.write()
        archive_write_seconds = time.perf_counter()-started
        writer._close_connection()
        scene.clear()
        reader = SQLiteIO(str(archive), scene, readonly=True)
        started = time.perf_counter()
        reader.read()
        metadata_read_seconds = time.perf_counter()-started
        reader._close_connection()
        timed = 'time_budget_ms' in inspect.signature(scene.add_queued_items).parameters
        chunks = []
        while not scene.items_to_add.empty():
            started = time.perf_counter()
            if timed:
                scene.add_queued_items(max_items=128, time_budget_ms=6)
            else:
                scene.add_queued_items()
            chunks.append((time.perf_counter()-started)*1000)
            app.processEvents()
        items = list(scene.items_for_save())
        assert len(items) == args.items
        assert not any(item.has_full_pixmap() for item in items)

        decoder = image_decode.DisplayDecoder(app)
        seen, notifications, repaint_turns = set(), [], []
        store = lod.cache()
        store.changed.connect(lambda item: notifications.append(id(item)))
        class Owner(QtWidgets.QWidget):
            def isVisible(self):
                return True
            def needs_display(self, item):
                return True
            def update(self):
                repaint_turns.append(time.perf_counter())
                for item in items:
                    if id(item) in seen:
                        continue
                    if store.get(item, 192) is not None:
                        seen.add(id(item))
                    else:
                        decoder.request(item, 192, self)
        owner = Owner()
        pulse_times = []
        timer = QtCore.QTimer()
        timer.setInterval(5)
        timer.timeout.connect(lambda: pulse_times.append(time.perf_counter()))
        timer.start()
        started = time.perf_counter()
        owner.update()
        peak_pending = len(decoder.requests)
        deadline = started+120
        while len(seen) < len(items):
            app.processEvents()
            peak_pending = max(peak_pending, len(decoder.requests))
            if not decoder.requests:
                owner.update()
            if time.perf_counter() > deadline:
                raise TimeoutError('Display decode did not finish within 120 seconds')
            time.sleep(.001)
        decode_seconds = time.perf_counter()-started
        timer.stop()
        gaps = [(b-a)*1000 for a, b in zip(pulse_times, pulse_times[1:])]
        decoder.shutdown()
        assert store.bytes <= store.budget
        assert all(hashlib.sha256(path.read_bytes()).hexdigest() == digest
                   for path, digest in zip(files, originals))
        result = {
            'items': args.items, 'formats': ['PNG RGBA', 'JPEG RGB'],
            'dimensions': [1024, 768], 'platform': app.platformName(),
            'generated_seconds': round(generated_seconds, 3),
            'archive_bytes': archive.stat().st_size,
            'archive_write_seconds': round(archive_write_seconds, 3),
            'metadata_read_seconds': round(metadata_read_seconds, 3),
            'scene_install_total_ms': round(sum(chunks), 3),
            'scene_install_longest_turn_ms': round(max(chunks), 3),
            'scene_install_turns': len(chunks),
            'actual_archive_decode_seconds': round(decode_seconds, 3),
            'display_notifications': len(notifications),
            'repaint_retry_turns': len(repaint_turns),
            'peak_pending': peak_pending,
            'display_bytes': store.bytes, 'display_budget_bytes': store.budget,
            'heartbeat_max_gap_ms': round(max(gaps, default=0), 3),
            'heartbeat_median_gap_ms': round(statistics.median(gaps), 3) if gaps else 0,
            'original_files_unchanged': True,
            'note': 'Generated distinct real files and archive blobs; excludes user projects, 4K monitors and GPU validation.'
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(json.dumps(result, indent=2))
        scene.clear()


if __name__ == '__main__':
    main()
