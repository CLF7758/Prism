"""Card projections and repeated navigation must not reprocess full records."""
import copy
from unittest.mock import patch

from prism.workspace_service import ResourceTreeService


def test_card_summary_matches_full_card_and_does_not_expose_owned_data(main_window):
    page = main_window.material_library
    service = page.records
    material = service.create(name='Display projection')
    owned = service.records[0]
    owned.update(tags=['wood'], files=[{'name': 'original.png', 'private': 'manifest'}],
                 textures={'basecolor': {'width': 2048, 'height': 1024, 'channel': 'RGB',
                                         'sha256': 'private-original-hash'}},
                 nativePackages=[{'engineVersion': '5.5', 'private': 'package contents'}],
                 preview={'properties': {'slots': {'roughness': {'value': .2}}},
                          'signature': 'render-signature', 'thumbnailAttachmentId': 123})
    summary = service.list_card_records()[0]
    assert 'files' not in summary
    assert 'properties' not in summary['preview']
    assert 'sha256' not in summary['textures']['basecolor']
    page.materials.set_records(service.list_records(), material['id'])
    expected = copy.deepcopy(page.materials.assets)
    page.materials.set_records([summary], material['id'])
    assert page.materials.assets == expected
    summary['tags'].append('changed')
    summary['textures']['basecolor']['width'] = 1
    summary['nativePackages'][0]['engineVersion'] = 'wrong'
    assert owned['tags'] == ['wood']
    assert owned['textures']['basecolor']['width'] == 2048
    assert owned['nativePackages'][0]['engineVersion'] == '5.5'


def test_card_scope_follows_move_trash_restore_and_purge(main_window):
    page = main_window.material_library
    folders, service = page.service, page.records
    root = folders.createFolder('material', 'Root')
    child = folders.createFolder('material', 'Child', root['id'])
    other = folders.createFolder('material', 'Other')
    material = service.create(child['id'], 'Scoped material')
    def ids(folder=None):
        return [r['id'] for r in service.list_card_records(folder)]
    assert ids(root['id']) == [material['id']]
    folders.moveBatch([child['id']], other['id'], 'material')
    assert ids(root['id']) == []
    assert ids(other['id']) == [material['id']]
    folders.trash([other['id']], 'deleted')
    assert ids() == []
    folders.restore_deleted([other['id']])
    assert ids() == [material['id']]
    folders.trash([other['id']], 'deleted')
    folders.purge([other['id']])
    # Existing orphan policy: purged folders must not hide retained records.
    assert ids() == [material['id']]


def test_selected_export_query_only_copies_selected_records(main_window):
    service = main_window.material_library.records
    selected = service.create(name='Selected')
    unrelated = service.create(name='Unselected')
    class UncopiedPayload(dict):
        def __deepcopy__(self, memo):
            raise AssertionError('An unrelated material was copied')
    owned = next(r for r in service.records if r['id'] == unrelated['id'])
    owned['preview']['private-extra'] = UncopiedPayload()
    records = service.list_records(record_ids={selected['id']})
    assert [r['id'] for r in records] == [selected['id']]
    assert service.list_records(record_ids=set()) == []
    assert len(service.list_card_records()) == 2


def test_entry_refreshes_once_and_reuses_unchanged_cards(main_window):
    window = main_window
    page = window.material_library
    page.records.create(name='Navigation material')
    page._loaded_revision = None
    with patch.object(page.materials, 'set_records', wraps=page.materials.set_records) as rebuild:
        window._open_material_library_entry()
        assert rebuild.call_count == 1
        window.tabs.setCurrentWidget(window.canvas_page)
        window._open_material_library_entry()
        window._open_material_library_entry()
        assert rebuild.call_count == 1
        page.records.create(name='New material')
        assert page.materials.count() == 2
        assert rebuild.call_count == 2


def test_resource_node_index_tracks_mutations_before_notifying(qapp):
    service = ResourceTreeService('project')
    observed = []
    service.changed.connect(lambda change: observed.append(
        (service.revision, [service._by_id(key) is not None for key in change['ids']])))
    root = service.createFolder('material', 'Root')
    child = service.createFolder('material', 'Child', root['id'])
    held = service._by_id(child['id'])
    service.moveBatch([child['id']], None, 'material')
    service.rename(child['id'], 'Renamed')
    assert service._by_id(child['id']) is held
    assert held['parentId'] is None and held['title'] == 'Renamed'
    assert service._descendants(root['id']) == set()
    service.trash([child['id']], 'deleted')
    service.purge([child['id']])
    assert service._by_id(child['id']) is None
    assert observed[-1][1] == [False]
    assert [revision for revision, _ in observed] == list(range(1, len(observed)+1))


def test_explicit_browser_refresh_consumes_queued_refresh(main_window):
    browser = main_window.asset_browser
    browser.timer.start()
    browser.refresh()
    assert not browser.timer.isActive()


def test_metadata_query_uses_existing_unsorted_asset_iterator(main_window):
    from tests.test_asset_browser import photo
    first = photo(main_window, 'default-canvas', 'First')
    second = photo(main_window, 'default-canvas', 'Second')
    scene = main_window.view.scene
    def no_stacking_sort(*args, **kwargs):
        raise AssertionError('Metadata enumeration requested the full stacking order')
    # Only the metadata query must avoid stacking order. Drawing, groups and
    # teardown legitimately use scene.items() outside this synchronous call.
    with patch.object(scene, 'items', no_stacking_sort):
        records = main_window.view.category_panel.catalog.query()
    assert {record.item for record in records} == {first, second}
