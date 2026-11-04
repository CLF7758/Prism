"""Manual verification: video/GLB per-item metadata survives a save round trip.

Fixes A1 from coordination/AUDIT-FULL-20261023.md: ``get_extra_save_data()``
for video and GLB items used to omit tags/rating/notes/title/canvas, so the
labels vanished on save+reopen. This script proves the payload now carries
them end to end (construct -> label -> save payload -> reload -> label).

Not collected by pytest: constructing video items under pytest-qt crashes
natively (0xC0000409) on this machine, so the same assertions live here as
a standalone check, following the existing ``_verify_*.py`` pattern.
Run:  .venv\\Scripts\\python.exe tests\\_verify_video_glb_metadata.py
"""
import os
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6 import QtWidgets  # noqa: E402

app = QtWidgets.QApplication([])

from prism.items import PrismGlbItem, PrismVideoItem  # noqa: E402


def main():
    g = PrismGlbItem(filename='model.glb', glb_blob=b'glb-bytes')
    g.tags = ['vehicle', 'final']
    g.rating = 4
    g.notes = 'hero model note'
    g.title = 'Hero Model'
    g.canvas_id = 'canvas-7'
    g.categories = ['cat-1']
    saved = g.get_extra_save_data()
    assert saved['tags'] == ['vehicle', 'final'], saved
    assert saved['rating'] == 4, saved
    assert saved['notes'] == 'hero model note', saved
    assert saved['title'] == 'Hero Model', saved
    assert saved['canvasId'] == 'canvas-7', saved
    assert saved['categories'] == ['cat-1'], saved

    v = PrismVideoItem(filename='clip.mp4', video_blob=b'video-bytes')
    v.tags = ['ref']
    v.rating = 2
    v.notes = 'cut here'
    v.title = 'Clip 01'
    v.categories = ['cat-2']
    saved_v = v.get_extra_save_data()
    assert saved_v['tags'] == ['ref'], saved_v
    assert saved_v['rating'] == 2, saved_v
    assert saved_v['notes'] == 'cut here', saved_v
    assert saved_v['title'] == 'Clip 01', saved_v
    assert saved_v['categories'] == ['cat-2'], saved_v

    # Reload side: the stored payload feeds back the same metadata.
    # The real load path (scene.py:929) calls update_from_data(**row)
    # with the JSON payload nested under 'data', like a saved row.
    g2 = PrismGlbItem(filename='model.glb', glb_blob=b'glb-bytes')
    g2.update_from_data(data=saved)
    assert g2.tags == ['vehicle', 'final'], g2.tags
    assert g2.rating == 4
    assert g2.title == 'Hero Model'
    assert g2.canvas_id == 'canvas-7'

    v2 = PrismVideoItem(filename='clip.mp4', video_blob=b'video-bytes')
    v2.update_from_data(data=saved_v)
    assert v2.tags == ['ref'], v2.tags
    assert v2.rating == 2
    assert v2.title == 'Clip 01'

    print('VIDEO/GLB METADATA ROUND TRIP: PASS')


if __name__ == '__main__':
    main()
