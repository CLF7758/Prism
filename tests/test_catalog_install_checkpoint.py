from prism.catalog_install import OverlayProgress, checkpoint_install, resume_install
from prism.catalog_browser import CatalogResultCache


def test_two_resumes_do_not_share_iterator_or_candidates():
    pending = dict(offset=2, items=['a'], overlay={3: 'b'}, new=['c'],
                   match_context={'active': {'tag'}},
                   overlay_progress=OverlayProgress.begin({3: 'b'}, ['c']))
    first, pending['overlay_progress'] = pending['overlay_progress'].take()
    pending['items'].append(first[0])
    cache = CatalogResultCache()
    cache.put_pending('q', pending)
    pending['new'].append('late')
    left = resume_install(cache.get_pending('q'))
    right = resume_install(cache.get_pending('q'))
    entry, left['overlay_progress'] = left['overlay_progress'].take()
    assert entry == ('c', 'new_rows')
    assert right['overlay_progress'].take()[0] == entry
    left['items'].append('local')
    left['overlay'].clear()
    left['match_context']['active'].add('local')
    saved = cache.get_pending('q')
    assert saved['items'] == ('a', 'b')
    assert saved['new'] == ('c',)
    assert saved['overlay'] == {3: 'b'}
    assert saved['match_context']['active'] == {'tag'}
    saved['overlay'].clear()
    cache.pending_view()['q']['match_context']['active'].clear()
    assert cache.get_pending('q')['overlay'] == {3: 'b'}
    assert cache.get_pending('q')['match_context']['active'] == {'tag'}


def test_checkpoint_excludes_live_preview_ownership():
    state = checkpoint_install(dict(grid_preview=object(), grid_preview_view=object(),
        grid_data_index={}, rows=object(), offset=0))
    assert not any(key.startswith('grid_') for key in state)


def test_bounded_overlay_batches_own_counters_and_continuations():
    from prism.catalog_install import consume_overlay
    start = OverlayProgress.begin({1: 'a', 2: 'b'}, ['c'])
    first = consume_overlay(start, matches=lambda x: x != 'b', valid=lambda: True,
                            expired=lambda: False, limit=2)
    assert first.accepted == ('a',)
    assert (first.overlay_rows, first.new_rows, first.filtered_rows, first.stop) == (2, 0, 1, 'limit')
    second = consume_overlay(first.progress, matches=lambda x: True, valid=lambda: True,
                             expired=lambda: False)
    assert second.accepted == ('c',)
    assert second.stop == 'exhausted'
    assert start.offset == 0
    cancelled = consume_overlay(start, matches=lambda x: True, valid=lambda: False,
                                expired=lambda: False)
    assert cancelled.stop == 'invalid' and cancelled.progress is start
    budget = consume_overlay(start, matches=lambda x: True, valid=lambda: True,
                             expired=lambda: True)
    assert budget.stop == 'budget' and budget.progress.offset == 1
