"""Measure the per-call cost of the screenshot hotkey's native event filter.

The live trace sampled the GUI thread inside ``capture_hotkey.py:67
nativeEventFilter`` for 4 of 9 stack samples of the 445.8 ms stall that
Chromium's window creation caused while the library pre-warm built the card
surface.  Every native message the process receives goes through this filter,
so its per-call cost is multiplied by the message burst.

This probe compares the current body (build the whole ``MSG`` structure) with
a body that reads only the two fields it needs, and measures the early-out for
the case where no global shortcut is registered.

Run: ``python -u tests/_audit_native_event_filter_cost.py``
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

WM_HOTKEY = 0x0312
ITERATIONS = 200000


def build_message(message_id: int = WM_HOTKEY, wparam: int = 0x1234) -> int:
    """A MSG that looks like the one Windows hands to a native filter."""
    msg = wintypes.MSG()
    msg.hwnd = 0
    msg.message = message_id
    msg.wParam = wparam
    msg.lParam = 0
    msg.time = 0
    msg.pt = wintypes.POINT(0, 0)
    return ctypes.addressof(msg)


def current_body(address: int) -> bool:
    native = ctypes.cast(address, ctypes.POINTER(wintypes.MSG)).contents
    if native.message == WM_HOTKEY and native.wParam == 0x1234:
        return True
    return False


def narrow_body(address: int) -> bool:
    # MSG on 64-bit: hwnd (8) | message (4) | wParam (8) | lParam (8) ...
    # The message id sits at offset 8, so it is the third 32-bit word.
    code = ctypes.cast(address, ctypes.POINTER(ctypes.c_uint32))[2]
    if code != WM_HOTKEY:
        return False
    wparam = ctypes.cast(address, ctypes.POINTER(ctypes.c_size_t))[2]
    return wparam == 0x1234


def timed(label: str, function, address: int) -> float:
    start = time.perf_counter()
    for _ in range(ITERATIONS):
        function(address)
    elapsed = time.perf_counter() - start
    per_call = elapsed / ITERATIONS * 1e6
    print(f'  {label:28s} {per_call:8.3f} us/call  ({elapsed:.3f}s for {ITERATIONS})')
    return per_call


def main() -> int:
    hotkey = build_message(WM_HOTKEY, 0x1234)
    other = build_message(0x0005, 0x1234)          # a message that is not WM_HOTKEY
    print(f'iterations={ITERATIONS}')
    current_hot = timed('current (WM_HOTKEY)', current_body, hotkey)
    current_other = timed('current (other message)', current_body, other)
    narrow_hot = timed('narrow (WM_HOTKEY)', narrow_body, hotkey)
    narrow_other = timed('narrow (other message)', narrow_body, other)
    print(f'\n  speed-up: WM_HOTKEY {current_hot / narrow_hot:.1f}x, '
          f'other message {current_other / narrow_other:.1f}x')
    print(f'  a burst of 20000 native messages costs '
          f'{current_other * 20000 / 1000:.1f}ms today, '
          f'{narrow_other * 20000 / 1000:.1f}ms with the narrow body')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
