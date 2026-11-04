from types import SimpleNamespace

from prism.model_library.multimaterial.edit_session import (
    CommitLedger, PendingCommit, PendingPreview, PreviewSession)


def test_out_of_order_receipt_only_confirms_its_own_command():
    ledger = CommitLedger()
    for name in ('a', 'b'):
        ledger.register(PendingCommit(SimpleNamespace(request_id=name)))
    assert ledger.acknowledge('unknown') == ('unknown', None)
    assert ledger.acknowledge('a')[0] == 'accepted'
    assert ledger.newest_waiting().request_id == 'b'
    assert ledger.acknowledge('a') == ('duplicate', None)
    assert ledger.order == ['b']


def test_cancelled_or_other_project_frame_cannot_install():
    target = SimpleNamespace(project_session='project', open_epoch=1, model_id='model')
    session = PreviewSession()
    frame = SimpleNamespace(target=target, request_id='a', draft_generation=2)
    session.current = PendingPreview('a', 2)
    assert session.rejection_reason(frame, target) is None
    other = SimpleNamespace(project_session='other', open_epoch=1, model_id='model')
    assert session.rejection_reason(frame, other) == 'model_or_project_changed'
    session.take_current()
    assert session.rejection_reason(frame, target) == 'no_pending_preview'
    session.lease_id = 'lease'
    assert session.take_lease() == 'lease'
    assert session.take_lease() is None
