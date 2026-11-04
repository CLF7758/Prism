"""The preview helper must be ready before the user opens a model.

The helper loads VTK before it answers the handshake.  Measured on the 100k
project: 2.32 s of start-up, against 0.07 s for the artifact and 1.07 s for
the render itself.  The first preview of a session therefore cost 3.60 s,
of which two thirds was the helper waking up.

These tests pin the two properties that fix it:

  * a helper that was started early but never received a job gives its
    memory back on its own,
  * such a helper never spends the restart budget a real job needs.
"""
import time

from prism.model_library.native_jobs import NativeModelJobController, RuntimeConfig


def _controller(tmp_path, **kwargs):
    return NativeModelJobController(RuntimeConfig(cache_root=tmp_path, **kwargs))


class _Channel:
    """Just enough of the channel for the controller to talk to."""

    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)

    def close(self):
        pass


def test_a_helper_that_never_served_a_job_is_released(tmp_path):
    controller = _controller(tmp_path, unused_exit_s=0.01, idle_exit_s=0)
    channel = _Channel()
    controller._channel = channel
    controller._last_used_mono = time.monotonic() - 5
    try:
        controller._maybe_idle_exit()
        assert channel.sent, 'the unused helper was not released'
    finally:
        controller.shutdown()


def test_a_helper_that_served_a_job_stays(tmp_path):
    controller = _controller(tmp_path, unused_exit_s=0.01, idle_exit_s=0)
    channel = _Channel()
    controller._channel = channel
    controller._last_used_mono = time.monotonic() - 5
    controller._served = True
    try:
        controller._maybe_idle_exit()
        assert not channel.sent, 'a helper that did the work was released'
    finally:
        controller.shutdown()


def test_a_helper_without_the_rule_is_never_released(tmp_path):
    controller = _controller(tmp_path, idle_exit_s=0)
    channel = _Channel()
    controller._channel = channel
    controller._last_used_mono = time.monotonic() - 600
    try:
        controller._maybe_idle_exit()
        assert not channel.sent, 'a helper was released without the rule'
    finally:
        controller.shutdown()


def test_a_helper_that_never_worked_does_not_spend_the_restart_budget(tmp_path):
    """A warm start that fails must not take the attempts a job needs."""
    controller = _controller(tmp_path)

    class DeadProcess:
        def poll(self):
            return 3

        def wait(self, timeout=None):
            return 3

    try:
        controller._reap(DeadProcess())
        assert not controller._restarts, 'an unused helper spent the restart budget'
        # A helper that did the work still counts as a crash.
        controller._served = True
        controller._reap(DeadProcess())
        assert len(controller._restarts) == 1
    finally:
        controller.shutdown()


def test_a_helper_that_was_asked_to_stop_is_not_started_again(tmp_path):
    """A released helper must not come back, or it would stop and start forever."""
    controller = _controller(tmp_path, unused_exit_s=0.01, idle_exit_s=0)
    channel = _Channel()
    controller._channel = channel
    controller._last_used_mono = time.monotonic() - 5
    started = []
    controller._ensure_started = lambda: started.append(True)
    try:
        controller._maybe_idle_exit()
        assert channel.sent, 'the unused helper was not released'
        controller._reap(None)
        assert not started, 'the released helper was started again'
    finally:
        controller.shutdown()


def test_a_helper_that_said_goodbye_is_replaced(tmp_path):
    """The preview window closed: the next model must find a warm helper."""
    controller = _controller(tmp_path, unused_exit_s=0, idle_exit_s=0)
    started = []
    controller._ensure_started = lambda: started.append(True)
    controller._graceful_goodbye = True
    try:
        controller._reap(None)
        assert started, 'the helper was not replaced after it said goodbye'
    finally:
        controller.shutdown()


def test_the_material_service_asks_for_the_helper_to_start(tmp_path):
    from prism.model_library.multimaterial.application import ProjectMaterialService

    asked = []

    class Controller:
        def warm(self):
            asked.append(True)

    service = object.__new__(ProjectMaterialService)
    service.controller = Controller()
    service.warm_native()
    assert asked, 'the material service did not ask for the helper'
