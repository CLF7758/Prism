from prism.widgets.preview_updates import PreviewUpdates


def test_latest_tick_flush_and_cancel(qtbot):
    queue = PreviewUpdates(interval=10)
    values = []
    queue.ready.connect(values.append)
    for i in range(100):
        queue.submit(i)
    qtbot.waitUntil(lambda: bool(values))
    assert values == [99]
    queue.submit('final')
    queue.flush()
    qtbot.wait(30)
    assert values == [99, 'final']
    queue.submit('discarded')
    queue.stop()
    qtbot.wait(30)
    assert values == [99, 'final']


def test_reentrant_intent_is_not_lost(qtbot):
    queue = PreviewUpdates(interval=10)
    values = []
    def receive(value):
        values.append(value)
        if value == 1:
            queue.submit(2)
    queue.ready.connect(receive)
    queue.submit(1)
    qtbot.waitUntil(lambda: values == [1, 2])
