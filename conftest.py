# 根目录的 conftest：只用来排除不是测试的脚本。
#
# 根目录的 test_video.py 是手工调试抽帧用的脚本，名字却符合 pytest 的
# 默认收集模式，跑全量时会去 import 它、执行它，把整个测试会话带偏。
#
# 曾经用 setup.cfg 的 `python_files = tests/test_*.py` 来排除它 —— 能
# 排除掉，但那个配置是按「相对路径 glob」匹配的，副作用是目录参数被
# 静默过滤：
#
#     pytest tests/actions                  -> no tests collected
#     pytest tests/actions/test_actions.py  -> 56 passed
#
# 谁用目录参数跑子目录都会以为那里没有测试。所以改成在这里显式忽略。
collect_ignore = ['test_video.py']
