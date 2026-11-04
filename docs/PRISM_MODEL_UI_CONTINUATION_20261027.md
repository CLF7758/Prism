# 模型库实际 UI 验收与继续工作记录

本轮未完成，禁止按前面的日志/离屏测试通过数字宣告模型库可用。

## 用户最新授权与计划

用户要求以北京时间 2026-10-27 22:02 为基准，三个半小时后（10 月 28 日 01:32）继续，每 30 分钟续查，直到完成。模型库和材质库共用实现发现同类缺陷时一起修复，不能只修模型库。

已调用线程 heartbeat 工具。指定 DTSTART 的立即创建被工具拒绝；按工具指示生成了带 Asia/Shanghai 时区、01:32 起始和每 30 分钟重复的确认卡。**截至生成本文时，卡片仍需用户确认，文件系统中尚未出现该自动任务，不能说已启用。** 两个旧的 prism/prism-t29-t30 自动任务处于暂停状态，不要更改。

## 工程、代码与样本

- 源码：`C:/Users/admin/Desktop/Prism-main/Prism-main`，无 Git，现有修改须保留。
- 真实工程：`C:/Users/admin/Desktop/Prism测试/Prism-100k-v1-新版.prism.data`；启动入口同目录 `.prism`。
- 样本：`E:/Prism-stress-100k/Game/LZPV/Assets/Envs/Slay/Assets/SetDec/WoodCarving`。
- 主模型 `SM_WoodCarving.FBX`；BC、N、ORM 三张原图都是 4096×4096。源 FBX 一个材质、三个 LOD 使用点，没有声明源贴图。
- 真实工程当前 WoodCarving ID：`a160ca7f4eaa4d888405c0c453b8375d`，工作材质 `w-0e2267cf7c43576be7d471f0`。最后只读检查 record_revision=51：颜色 RGB，法线 **A**，粗糙度 G，颜色强度 1.792。禁止绕过正在运行的场景直接更新 DB。

## 本轮真实 UI 已观察到的事实

已使用 computer-use 技能的 `@oai/sky` 对真实窗口观察/操作，不是仅离屏或内部 API：

1. 原本没有 Prism 正在运行；已正常启动源码版本并打开上述真实工程。
2. 折叠画布树、打开模型库、选择 WoodCarving 和材质球、打开大预览均实际操作过。
3. 大预览出现在主窗口前面。预览标题 `SM_WoodCarving.FBX · 模型预览`。
4. 实际看到了：完整材质很暗；切到颜色模式后仍覆盖密集三角布线。**不能把变黑全归咎于 ORM 图。** 用户也明确复述了所有通道都显示布线的问题。
5. 数据库查到法线旧绑定是 A；这本应被程序约束为 RGB，属于程序约束缺失。不要责怪用户。
6. 模型库卡片页顶部工具栏在窄宽度下存在文字拥挤；单材质列表区域过高，槽位出现多层滚动，仍需逐项检查实际尺寸。

当前用户可能仍在操作界面。每次 CUA 输入前重新观察并准确绑定窗口；不要碰其他应用。不要复用本文或旧工具结果中的坐标/窗口句柄。

## 已写代码但尚未验收的修复

### A. 所有通道显示布线的根因与修复

原代码：`native/renderer.py` 的 `highlight()` 用 `vtkProperty.EdgeVisibility` 高亮选中材质；`mode()` 又对 `bid in self.selected` 开边线。切通道生成新 property 后边线颜色还是默认黑色，密集三角边线会遮暗模型。

现在已改：

- `prism/model_library/multimaterial/native/renderer.py`：新增 `_selection_actors` 和 `sync_highlight()`，使用 `vtkPolyDataSilhouette`、相机和源 actor 变换，独立蓝色轮廓、不可拾取，匹配 LOD 可见性；`highlight()` 不再改材质 property 的三角边线；只有 `material_edges` 开启面边线；`wireframe` 仍是原生线框 representation。
- `prism/components/material_preview.py`：切 LOD 后调用 `renderer.sync_highlight()`。

**没有跑完这个补丁的测试，也没有重启旧预览进程验证最终画面。** 必须检查：带选择 → 每个通道 → 切回完整材质 → 参数修改/重装材质 → 清除选择，不能再出现布线污染；确认轮廓只落在选中使用点，隐藏 LOD 不露出轮廓，封面不包含选择高亮。三种线框相关行为不能混淆。

### B. 法线单通道约束

- `multimaterial/application.py` 的新贴图 set 和 channel 命令拒绝 normal 非 RGB。
- `widgets/model_material_editor.py` 在加载槽位时禁用法线组合框中的非 RGB 项。

仍需测试旧 A 绑定如何清楚展示并允许用户修正，不能让用户陷入「先正确预览才能编辑，错误法线又无法预览」的死循环。原始文件不能改。旧工程是否自动修复必须有明确语义和保留证据，不要静默覆盖用户数据。

### C. 外部 FBX 拖入中间列表导入

**尚未实现。** 已读代码：

- `widgets/model_library.py: _model_paths / dragEnterEvent / dragMoveEvent / dropEvent` 已有父页面导入处理；`import_paths()` 会使用当前目录，并走正式异步 service。
- 中间区域是共享 `MaterialCards` 的 WebEngine，原生子控件先接走拖放，父页面拿不到事件。
- `widgets/material_cards.py` 已在 QApplication 上安装 eventFilter，目前主要处理 Delete/F2，没有转交外部文件拖入。

建议在共享卡片提供**可注入**的外部拖放接收接口，只在模型库注入模型导入处理；覆盖 WebEngine 及内部接收子控件。保留原来的内部资产 MIME 拖放和材质库行为，不能把内部跨目录整理当成外部重新导入。未支持类型不能导致浏览器跳转本地文件。拖入一次只提交一次任务；复用 import_paths 当前目录/能力门/日志。文件夹枚举不要放在每次 dragMove 的高频路径。

## 必须继续的验收清单

逐项列出按钮/开关/滑块、操作前提、真实操作、画面与数据预期、实际结果、修复及复测证据：

- 导入（按钮、中心拖入、目录目标、多文件、失败、取消）、文件夹、网格/列表、排序、搜索、卡片大小、多选、清除选择、改名、标签备注、导出原件与哈希。
- 材质球选择、使用点范围、库替换、恢复、复制粘贴、选中轮廓。
- 每个槽位的添加/拖入/复用/清除、通道、启用、折叠、颜色/复原颜色、反转/法线绿通道、滑块和数值输入最小/中间/最大、连续拖动提交、撤销。
- 所有预览模式、LOD、正面、封面、返回、双击、窗口层级、拖动旋转、静止长按、滚轮缩放、平移、Ctrl 点选。
- 保存重开、失败恢复、跨项目晚到结果、原件不变、缓存刷新、模型与材质库共享功能回归。
- SSS/置换等仍未有合格生产实现的项目必须标明未实现/受阻，不能把禁用菜单记成已验收。

真实工程测试授权持续有效；不要擅自结束用户其他程序或丢弃未保存修改。Windows UI 删除动作遵守 computer-use 技能的确认规则；可先用隔离测试数据验证代码路径，真实数据删除不要偷偷执行。

## 日志改造与原有证据

日志补齐详见 `docs/LIBRARY_LOGGING_COMPLETION_20261027.md`。之前 1024 passed 的大范围回归和日志故障注入，是之前快照的证据，**不证明本轮轮廓/拖放/真实 UI 已完成**。

- `artifacts/library-logging-real-project/summary.json`：真实工程只读重放和诊断包路径。
- `artifacts/woodcarving-project/report.json`：之前离屏读写/图像证据，不代替真实控件验收。
- `tests/model_library/multimaterial/test_native_texture_rendering.py`：可扩展实际像素反例。
- `tests/model_library/multimaterial/test_integrated_runtime.py`：真实 service + Qt 端口回归，可扩展完整控件操作；不要只调私有 `_texture_requested` 就称按钮通过。

## 每轮结束前

更新本文：完成项、失败项、真正改动、运行中的测试/进程、下一步。不要另建“总集成 AI”。全部必要项真正验收完成后，再停止对应半小时自动任务。


## 2026-10-28 最新接续入口

本轮已实际修复并验证多项 UI/渲染问题，后续先读 `docs/MODEL_UI_VERIFICATION_20261028.md`，不要按上面的旧状态重复工作。定时任务 prism-2 已通过工具更新并在 TOML 核对 ACTIVE，UTC 2026-10-27 17:30Z 对应北京时间 10 月 28 日 01:30，每 30 分钟继续。当前仍非全部功能完成。

2026-10-28 半小时接续：新增七槽位实际 Qt 控件 → 真实 service/native 的验证；本轮无生产改动、无用户工程写入。详见 MODEL_UI_VERIFICATION_20261028.md 的半小时接续节；无仍运行的 pytest。

2026-10-28 09:30 触发接续：已通过真实桌面右栏将 WoodCarving 原件导出到 `artifacts/model-ui-export-20261028-0930`，独立 SHA-256 与导入清单一致，5 条实际导出事件同 job_id 闭合。只证明一个 FBX 原件，不证明批量/其他格式/编辑结果导出。无生产改动与用户数据库写入、无新 pytest。准备多选时检测到用户输入且 Explorer 到前景，已停止争用桌面；下轮先观察当前状态再继续批量导出与未验控件。详细原始证据、日志片段和限制见 MODEL_UI_VERIFICATION_20261028.md 新增节；本轮未更改自动任务。


## 2026-10-28 14:08 接续：用户最新 UI 调整

当前已改：model_details 隐藏检查栏封面/预览/导出按钮，标签备注放到版本与文件；材质页撑满剩余空间。model_library 改为材质库式单行工具栏，排序/导出/取消保留在数量标签右键菜单。model_material_panel 隐藏数量与贴图说明，材质球只画名称；model_material_list 按 viewport 宽度三等分，双击/F2 发重命名，PATCH display_name 经现有提交链持久化（application.py）。独立此部位缺选中部位时弹出明确部位选择器，取消不提交。以上用户直接要求优先于旧布局契约。

证据：model-sidebar-simplify-20261028.txt 45 passed；model-layout-match-20261028.txt 41 passed；model-metadata-tab-20261028.txt 45 passed；model-unique-ui-20261028.txt 37 passed；model-material-cards-20261028.txt 42 passed；model-material-rename-save-20261028.txt 真实服务提交/清缓存重读 1 passed；model-compact-cards-20261028.txt 新增实际 Qt F2 请求、三列尺寸、隐藏说明检查 1 passed。不是新版真实桌面全部验收，双击和独立后实际渲染仍待桌面验证。当前无 pytest 运行，已有 Prism 主进程及工作进程，不要覆盖或重复启动。此轮没有操作用户工程。

上轮共用 cards.js 菜单 Esc/blur 关闭已修，50 项回归并实际桌面复测；真实 WoodCarving 颜色选择红色/复原/数字恢复 1.792 已测，记录 model-ui-color-{red,restored,events}-20261028.json。最后已知 Wood record_revision=68/state_revision=67，normal RGB，BC 白色/1.792，thumbnail ready/material_state_revision=67；这是当时状态，接续先只读核对当前，不能回写旧值。曾观察材质库自动预览使工程 dirty，不要未经检查关闭并丢弃。其他十万真实模型/SSS/置换/安装包等未验项继续保留。
