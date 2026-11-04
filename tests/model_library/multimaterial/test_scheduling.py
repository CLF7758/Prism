from threading import Event

from prism.model_library.multimaterial.scheduling import MaterialScheduler


def test_commit_precedes_pending_preview_without_parallel_scene_mutation():
    scheduler = MaterialScheduler()
    entered, unblock = Event(), Event()
    order = []
    def running():
        entered.set()
        assert unblock.wait(5)
        order.append('running')
    first = scheduler.submit(running)
    try:
        assert entered.wait(5)
        preview = scheduler.submit(order.append, 'preview')
        commit = scheduler.submit(order.append, 'commit', priority=0)
        unblock.set()
        for task in (first, preview, commit):task.result(5)
        assert order == ['running', 'commit', 'preview']
    finally:
        unblock.set()
        scheduler.shutdown()


def test_only_pending_drafts_are_replaced_and_commits_are_preserved():
    scheduler = MaterialScheduler()
    entered, unblock = Event(), Event()
    first = scheduler.submit(lambda: (entered.set(), unblock.wait(5)))
    order = []
    try:
        assert entered.wait(5)
        old = scheduler.submit(order.append, 'old', replace_key='model')
        latest = scheduler.submit(order.append, 'latest', replace_key='model')
        commit = scheduler.submit(order.append, 'commit', priority=0)
        assert old.cancelled()
        unblock.set()
        for task in (first, latest, commit):task.result(5)
        assert order == ['commit', 'latest']
    finally:
        unblock.set()
        scheduler.shutdown()


def test_burst_releases_obsolete_work_and_keeps_only_latest_pending():
    scheduler=MaterialScheduler()
    entered,unblock=Event(),Event()
    order=[]
    running=scheduler.submit(lambda:(entered.set(),unblock.wait(5)))
    try:
        assert entered.wait(5)
        futures=[scheduler.submit(order.append,i,replace_key='model') for i in range(1000)]
        commit=scheduler.submit(order.append,'commit',priority=0)
        stats=scheduler.stats()
        assert stats['pending']==2 and stats['active']==1
        assert stats['replaced']==999
        assert stats['heap_entries']<=2*stats['pending']+32
        assert all(f.cancelled() for f in futures[:-1])
        unblock.set()
        running.result(5);commit.result(5);futures[-1].result(5)
        assert order==['commit',999]
    finally:
        unblock.set();scheduler.shutdown()
