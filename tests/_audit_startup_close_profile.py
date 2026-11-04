"""取证：**主窗口构造**与**关闭**两条路径的耗时构成。

用户实测（本轮新数据）：
* 主窗口构造相关停顿约 **1.30 秒**；
* 关闭过程仍有停顿，**保存确认路径约 1.09 秒**；
* GC：主线程最长约 385ms。

本脚本给这两条路径做 cProfile，指出具体函数；不做任何修改。

用法：
    $env:PYTHONIOENCODING='utf-8'; $env:QT_QPA_PLATFORM='offscreen'
    .\\.venv\\Scripts\\python.exe -m pytest tests\\_audit_startup_close_profile.py -q -s --no-header -p no:cacheprovider --no-cov
"""
from __future__ import annotations

import cProfile
import io
import pstats
import time
from unittest.mock import patch

import pytest
from PyQt6 import QtWidgets


def _dump(profiler, title):
    stream = io.StringIO()
    pstats.Stats(profiler, stream=stream).sort_stats('tottime').print_stats(24)
    print(f'\n===== {title} =====')
    print(stream.getvalue(), flush=True)


def test_startup_and_close_profile(qtbot):
    from prism.__main__ import PrismMainWindow
    app = QtWidgets.QApplication.instance()
    with patch('prism.browser_capture.BrowserCaptureService.start'), \
         patch('prism.capture_hotkey.CaptureHotkey'), \
         patch('prism.clipboard_capture.ClipboardCaptureAdapter.start'):
        profiler = cProfile.Profile()
        profiler.enable()
        started = time.perf_counter()
        window = PrismMainWindow(app)
        elapsed = (time.perf_counter() - started) * 1000
        profiler.disable()
        print(f'\nAUDIT: 主窗口构造 {elapsed:.1f}ms', flush=True)
        _dump(profiler, '主窗口构造（tottime）')

        # 关闭路径：先把场景标脏，让"保存确认"真的走一遍。
        window.view.mark_content_dirty()
        profiler = cProfile.Profile()
        profiler.enable()
        started = time.perf_counter()
        window.close()
        elapsed = (time.perf_counter() - started) * 1000
        profiler.disable()
        print(f'\nAUDIT: 关闭 {elapsed:.1f}ms', flush=True)
        _dump(profiler, '关闭（tottime）')
