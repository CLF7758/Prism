"""Regenerate the built-in placeholder using the same shipped Babylon renderer."""
from pathlib import Path
from PyQt6 import QtGui
from prism.material_textures import resolved_properties
from prism.widgets.material_render import MaterialRender


def test_default_sphere_black_background(qtbot):
    viewer = MaterialRender()
    qtbot.addWidget(viewer)
    viewer.resize(256,256)
    viewer.show()
    viewer.set_shape('sphere')
    output, errors = [], []
    viewer.completed.connect(lambda signature, blob: output.append(blob))
    viewer.failed.connect(errors.append)
    viewer.load_material(dict(maps={}, recordId='builtin-default', signature='default-black-v2',
        properties=resolved_properties({}, {}, 'opaque', 'no alpha')), 'default')
    qtbot.waitUntil(lambda: bool(output) or bool(errors), timeout=30000)
    assert not errors
    image = QtGui.QImage.fromData(output[-1])
    for x,y in ((0,0),(0,255),(255,0),(255,255)):
        assert image.pixelColor(x,y).getRgb()[:3] == (0,0,0)
    path = Path(__file__).resolve().parents[1]/'artifacts/material-tests/default-sphere-black.png'
    path.write_bytes(output[-1])
