"""pytest 插件：跑太久就把**所有线程**的 Python 栈写进文件并强制退出。

比 faulthandler 更可靠：它不依赖 faulthandler 的定时器线程，而是用一个
普通的 threading.Timer（拿 GIL 就能跑），并用 ``sys._current_frames()``
拿到每个线程的栈。

用法：
    $env:PYTHONPATH = "<repo>\\tests"
    $env:FAULT_AFTER = '25'
    $env:FAULT_OUT   = "<path>\\thread-dump.txt"
    python -m pytest <file> -p _audit_thread_dump_plugin ...
"""
import os
import sys
import threading
import traceback


def _dump():
    lines = ['=== THREAD DUMP ===']
    frames = sys._current_frames()
    for tid, frame in frames.items():
        name = threading.current_thread().name
        try:
            for thread in threading.enumerate():
                if thread.ident == tid:
                    name = thread.name
                    break
        except Exception:
            pass
        lines.append(f'--- thread {tid} ({name}) ---')
        lines.append(''.join(traceback.format_stack(frame)))
    path = os.environ.get('FAULT_OUT', 'thread-dump.txt')
    try:
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write('\n'.join(lines))
    except Exception:
        pass
    os._exit(3)


def pytest_configure(config):
    after = int(os.environ.get('FAULT_AFTER', '25'))
    timer = threading.Timer(after, _dump)
    timer.daemon = True
    timer.start()
