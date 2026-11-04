"""T09/T10: passive proxies do not wait for cyclic GC or old geometry aliases."""
import gc
import weakref

from prism.asset_records import ImageRecord
from prism.catalog_browser import CatalogAssetProxy
from prism.item_lifecycle import logical_item


class State:
    project_id = 'project'


def test_20000_passive_proxies_release_without_cyclic_collection(qapp):
    scene, reader = State(), State()
    enabled = gc.isenabled()
    # Diagnostic only: prove immediate refcount release without invoking GC.
    # Production code never changes the collector's mode or thresholds.
    gc.disable()
    try:
        records = [CatalogAssetProxy({'id': i, 'width': 100, 'height': 80},
                                    'isolated.prism', scene, reader) for i in range(20000)]
        references = [weakref.ref(record) for record in records]
        assert records[0]._asset_record is records[0]
        assert '_asset_record' not in records[0].__dict__
        del records
        assert all(reference() is None for reference in references)
    finally:
        if enabled:
            gc.enable()


def test_materialized_shared_state_keeps_logical_identity(view):
    record = ImageRecord({'data': {'imageWidth': 100, 'imageHeight': 80}}, None)
    view.scene.addItem(record)
    item = record.graphics_item()
    assert logical_item(item) is record
    assert item._asset_record is record
    item.setPos(12, 24)
    assert record.pos() == item.pos()
    view.scene._viewport_items.recycle(record)
    assert record._graphics is None
    assert logical_item(record) is record
    assert record._asset_record is record
    replacement = record.graphics_item()
    assert replacement is not item
    assert logical_item(replacement) is record


def test_model_reset_releases_geometry_generation(qapp):
    from tests.test_asset_canvas_layout_identity import canvas
    owner, host, model = canvas(qapp)
    record = ImageRecord({'data': {'imageWidth': 100, 'imageHeight': 80}}, None)
    reference = weakref.ref(record)
    rows = [record]
    model.replace(rows)
    host._catalog_geometry_items = rows
    host._catalog_geometry_rows = host._item_rows
    model.replace([])
    assert host._catalog_geometry_items is None
    assert host._catalog_geometry_rows is None
    del record, rows
    assert reference() is None
    owner.close()


def test_partial_refresh_consumes_unused_layout_hint(main_window, monkeypatch):
    browser = main_window.asset_browser
    browser.view.scene._partial_project_source = 'isolated.prism'
    old = ImageRecord({'data': {'imageWidth': 100, 'imageHeight': 80}}, None)
    reference = weakref.ref(old)
    browser._layout_hint = ([old], 'all')
    del old
    monkeypatch.setattr(browser, '_refresh_catalog_assets', lambda: None)
    browser.refresh()
    assert browser._layout_hint is None
    assert reference() is None
