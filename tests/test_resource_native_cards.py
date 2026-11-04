import base64

from PyQt6 import QtCore, QtGui

from tests.test_catalog_browser_partial import prepare, wait_complete
from tests.test_material_cards import javascript


def test_mixed_scope_uses_real_library_cards_and_visible_covers(main_window, tmp_path, qtbot):
    scope, scene, browser = prepare(main_window, tmp_path, [
        (1, 'pixmap', dict(canvasId='A', title='Image', tags=[]))])
    image = QtGui.QImage(32, 32, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('#d13820'))
    buffer = QtCore.QBuffer()
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, 'PNG')
    data = bytes(buffer.data())
    scene.attachments[500] = ('cover.png', 'image/png', data)
    material = main_window.view.category_panel.material_service.create(None, name='Native material')
    stored = next(record for record in scene.material_records if record['id'] == material['id'])
    stored['preview']['thumbnailAttachmentId'] = 500
    scene.model_records.append(dict(id='native-model', name='Native model', folder_id=None,
        tags=[], notes='', files=[], format='fbx', deleted_at=None,
        thumbnail={'data_url': 'data:image/png;base64,' + base64.b64encode(data).decode('ascii')}))
    main_window.resize(1200, 800)
    main_window.show()
    browser.open('all')
    wait_complete(qtbot, browser, 5)
    grid = browser.grid_list
    qtbot.waitUntil(lambda: grid.native_cards is not None and grid.native_cards.ready, timeout=15000)
    cards = grid.native_cards
    from prism.widgets.material_cards import MaterialCards
    assert isinstance(cards, MaterialCards)
    qtbot.waitUntil(lambda: len(cards.assets) == 5, timeout=10000)
    for key, name in (('material:' + material['id'], 'Native material'), ('model:native-model', 'Native model')):
        dom = cards._dom_id(key)
        expression = f"document.querySelector('[data-asset=\"{dom}\"] h3')?.textContent"
        qtbot.waitUntil(lambda: javascript(cards, expression, qtbot) == name, timeout=10000)
        cover = f"document.querySelector('[data-asset=\"{dom}\"] .card-image img')?.src.startsWith('data:image/png')"
        qtbot.waitUntil(lambda: javascript(cards, cover, qtbot) is True, timeout=10000)
    javascript(cards, f"document.querySelector('[data-asset=\"{cards._dom_id('model:native-model')}\"]').click()", qtbot)
    qtbot.waitUntil(lambda: browser.list.currentIndex().data(QtCore.Qt.ItemDataRole.UserRole).get('model_id') == 'native-model')


def test_native_cards_virtual_scroll_keeps_dom_bounded(main_window, tmp_path, qtbot):
    scope, scene, browser = prepare(main_window, tmp_path, [
        (key, 'pixmap', dict(canvasId='A', title=str(key), tags=[])) for key in range(1200)])
    main_window.view.category_panel.material_service.create(None, name='Visible material')
    scene.model_records.append(dict(id='m', name='Visible model', folder_id=None,
        tags=[], notes='', files=[], format='obj', deleted_at=None))
    main_window.resize(1200, 800)
    main_window.show()
    browser.open('all')
    wait_complete(qtbot, browser, 1204)
    qtbot.waitUntil(lambda: browser.grid_list.native_cards is not None and browser.grid_list.native_cards.ready, timeout=15000)
    cards = browser.grid_list.native_cards
    qtbot.waitUntil(lambda: len(cards.assets) == 1204, timeout=10000)
    qtbot.waitUntil(lambda: javascript(cards, "document.querySelectorAll('.asset-card').length", qtbot) > 0)
    initial = javascript(cards, "document.querySelector('.asset-card').dataset.asset", qtbot)
    javascript(cards, 'window.scrollTo(0,12000)', qtbot)
    qtbot.waitUntil(lambda: javascript(cards, 'Math.round(window.scrollY)', qtbot) > 5000, timeout=10000)
    qtbot.waitUntil(lambda: javascript(cards, "document.querySelector('.asset-card').dataset.asset", qtbot) != initial)
    assert javascript(cards, "document.querySelectorAll('.asset-card').length", qtbot) < 200
    assert javascript(cards, "Array.from(document.querySelectorAll('.asset-card')).some(c=>{const r=c.getBoundingClientRect();return r.bottom>0&&r.top<innerHeight})", qtbot)


def test_native_cards_show_libraries_and_keep_scroll_while_rows_arrive(main_window, tmp_path, qtbot, monkeypatch):
    scope, scene, browser = prepare(main_window, tmp_path, [
        (key, 'pixmap', dict(canvasId='A', title=str(key), tags=[])) for key in range(2000)])
    material = main_window.view.category_panel.material_service.create(None, name='Early material')
    scene.model_records.append(dict(id='early-model', name='Early model', folder_id=None,
        tags=[], notes='', files=[], format='fbx', deleted_at=None))
    queue = browser._queue_catalog_install
    monkeypatch.setattr(browser, '_queue_catalog_install',
                        lambda pending, **kw: queue(pending, delay=30, progress=kw.get('progress', True)))
    main_window.resize(1200, 800)
    main_window.show()
    browser.open('untagged')
    qtbot.waitUntil(lambda: browser._catalog_pending is not None
        and browser.grid_list.native_cards is not None and browser.grid_list.native_cards.ready
        and len(browser.grid_list.native_cards.assets) > 100, timeout=15000)
    cards = browser.grid_list.native_cards
    assert {'material:' + material['id'], 'model:early-model'} <= set(cards._record_ids)
    initial_count = len(cards.assets)
    javascript(cards, 'window.scrollTo(0,600)', qtbot)
    qtbot.waitUntil(lambda: javascript(cards, 'window.scrollY', qtbot) >= 500)
    qtbot.waitUntil(lambda: len(cards.assets) > initial_count, timeout=10000)
    assert browser._catalog_pending is not None
    assert javascript(cards, 'window.scrollY', qtbot) >= 500
    wait_complete(qtbot, browser, 2004)
    qtbot.waitUntil(lambda: len(cards.assets) == 2004)
    assert javascript(cards, 'window.scrollY', qtbot) >= 500
