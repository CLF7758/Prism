# 源码版启动崩溃修复（2026-10-29）

用户连续启动失败。最新 Prism-native-crash-107536.log 指向 resource_tree_view._BlankAreaDelegate.paint → fixed_resource_sections.SectionRow.paintEvent，原生访问异常。

## 原因与改动

固定分类标题此前只调用 QStyleOptionViewItem.initFrom(tree)，没有初始化 QAbstractItemView 所需的 decorationSize/widget 等属性。实测 decorationSize 为 (-1,-1)，widget 为 None。正式 QSS 图标绘制因此出现 QPainter engine==0 警告，并在 Windows 原生样式下发生访问异常。

SectionRow 改为调用原树视图的 initViewItemOption，再复用现有 delegate.initStyleOption/paint。实测尺寸为 (16,16)，widget 为 ResourceTreeView。不修改启动脚本、依赖、工程数据或分类交互设计。

## 同条件对照

不可见子进程、Windows QPA、正式 theme.qss、80 个目录、固定分类完整绘制到内存 QImage（三轮）：
- 旧初始化：QPainter 警告后访问异常，退出 -1073741819（0xC0000005），栈与用户日志一致。
- 新初始化：PAINT_OK，退出 0，无 QPainter 警告。

脚本 artifacts/fixed-section-startup-probe-20261029.py（--legacy 重现旧初始化）；原始日志 fixed-section-startup-native-before-20261029.txt 与 fixed-section-startup-native-after-20261029.txt。

## 回归

`QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -m pytest -q tests/test_resource_section_startup.py tests/test_pinned_resource_sections.py tests/test_resource_tree_view.py tests/test_model_global_search.py`

16 passed in 13.45s，输出 artifacts/fixed-section-startup-regression-20261029.txt。

新增回归包含 offscreen/Windows 两种不可见子进程的正式主题绘制，以及完整主窗口的正式主题离屏绘制。既有测试没加载正式主题，先前通过未覆盖这次崩溃条件。已补测试，不关闭主题、不隐藏分类、不吞掉原生错误。

未控制用户桌面，未打开或改写真实工程；未用桌面点击启动 bat，桌面重新启动由用户确认。
