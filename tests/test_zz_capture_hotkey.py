"""The global hotkey filter must read the right field of a native MSG.

Every native message of the process arrives at the filter, and the filter
only cares about ``WM_HOTKEY``.  Reading that one field instead of copying
the whole struct keeps the common case free of any structure work -- the
library panel construction produces thousands of messages, and the copy
showed up as 45-118 ms blocks on the GUI thread right after a project
opened.

The ``message`` field is **not** the first one: a MSG starts with the
window handle.  Reading the first four bytes reads a handle, never a
message id, and the hotkey would silently stop working.
"""
import ctypes
from types import SimpleNamespace

from ctypes import wintypes


def _filter():
    from prism.capture_hotkey import CaptureHotkey

    hotkey = CaptureHotkey.__new__(CaptureHotkey)
    hotkey.registered = True
    hotkey.view = SimpleNamespace(on_action_screen_capture=lambda: None)
    return hotkey


def test_the_message_id_is_read_from_the_message_field(qapp):
    from prism.capture_hotkey import CaptureHotkey

    hotkey = _filter()
    message = wintypes.MSG()
    message.message = CaptureHotkey.WM_HOTKEY
    message.wParam = CaptureHotkey.IDENTIFIER
    assert hotkey.nativeEventFilter(b'windows_generic_MSG',
                                     ctypes.addressof(message)) == (True, 0)


def test_another_message_is_not_taken(qapp):
    from prism.capture_hotkey import CaptureHotkey

    hotkey = _filter()
    message = wintypes.MSG()
    message.message = 0x0005           # WM_SIZE
    message.wParam = CaptureHotkey.IDENTIFIER
    assert hotkey.nativeEventFilter(b'windows_generic_MSG',
                                     ctypes.addressof(message)) == (False, 0)


def test_an_unregistered_shortcut_reads_nothing(qapp):
    from prism.capture_hotkey import CaptureHotkey

    hotkey = _filter()
    hotkey.registered = False
    message = wintypes.MSG()
    message.message = CaptureHotkey.WM_HOTKEY
    message.wParam = CaptureHotkey.IDENTIFIER
    assert hotkey.nativeEventFilter(b'windows_generic_MSG',
                                     ctypes.addressof(message)) == (False, 0)


def test_the_message_field_is_not_the_first_field():
    """Guard the offset itself: the struct starts with the window handle."""
    from prism.capture_hotkey import _MESSAGE_FIELD_OFFSET

    assert _MESSAGE_FIELD_OFFSET == wintypes.MSG.message.offset
    assert _MESSAGE_FIELD_OFFSET > 0
