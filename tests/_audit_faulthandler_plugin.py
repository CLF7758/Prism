"""pytest 插件：跑太久就把所有线程的栈打出来并退出。

用法：
    $env:PYTHONPATH = "<repo>\\tests"
    python -m pytest <file> -p _audit_faulthandler_plugin ...
"""
import faulthandler
import os

faulthandler.enable()
faulthandler.dump_traceback_later(int(os.environ.get('FAULT_AFTER', '25')), exit=True)
