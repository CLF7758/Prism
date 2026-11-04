# 模型库交接入口：四个实现 AI，总集成由当前对话 Codex 负责

**总集成更新（2026-10-26）**：四条工作线已交回，当前主对话已装配并修复核心链路。实际代码、复测证据、仍禁用及未测范围以[总集成记录](C:/Users/admin/Desktop/Prism-main/Prism-main/docs/MODEL_LIBRARY_INTEGRATION_20261026.md)为准。后文“只有骨架／尚未实现”是最初分发任务时的基线，不再代表当前全部状态。

日期：2026-10-26。本文是分发说明与真实进度基线。每个 AI 各有一份完整任务文档；没有第五个“总集成 AI”。当前主对话的 Codex 负责公共接口、最终装配、冲突裁决、回归检查与最终验收。四个 AI 将结果交回这里，由我检查后接入。

本轮只重写交接文档，没有继续实现业务功能，也没有启动或派发其他 AI。下面的实施步骤是接下来要做的工作，不能当成已经实现。

## 1. 最终要交付什么

在 Prism 左侧“材质库”下面增加本地“模型库”。用户导入 FBX、OBJ、GLB、glTF，列表显示模型当前外观的封面；使用现有贴图槽位或现有材质给模型调整效果；双击进入可旋转、缩放、平移的预览；需要时单个或批量导出原件及配套文件。

- 复用现有布局、卡片、标签、备注、分类、管理和贴图控件，不再新建一套资产管理系统。
- 模型封面是有版本的缓存图片。列表不为每张卡片启动实时三维引擎，详细统计放右侧。
- 导入保持原文件字节和目录关系；自动识别依赖，无法确认的依赖明确报告。
- 外观编辑保存在 Prism 工程内。导出原件仍取出导入时的模型及依赖，不会包含这些外观修改。
- 直接使用 F3D 读取支持的源格式，不要求生成中间 GLB。各格式的真实能力必须分别验证。
- 双击预览包含完整材质、材质加边线、纯线框。F3D 的 show_edges 不能冒充纯线框。
- 原有材质库、保存、导航和管理行为必须回归；十万级目标要实测，不能以卡片复用证明完成。

产品与 F3D 源码分析见[完整方案](C:/Users/admin/Desktop/Prism-main/Prism-main/docs/MODEL_LIBRARY_F3D_PLAN_20261026.md)。该方案提供依据和候选实现；当前分工、实际完成状态和交接执行顺序以本文及四份任务文档为准。用户新指令优先。发现技术矛盾时提交最小问题与证据，由当前主对话裁决，不能默默更改另一份任务。

## 2. 四份文档如何分发

将对应文档的完整内容交给对应 AI。每份都包含总体目标、现有代码、文件修改范围、接口、逐步实施、并行依赖、验收与汇报模板，不要只复制标题或最后一段。

| 实现者 | 独立任务文档 | 主要责任 | 第一项可检查交付 |
|---|---|---|---|
| AI 1 | [存储、导入与原件导出](C:/Users/admin/Desktop/Prism-main/Prism-main/docs/model-library-tasks/AI_1_STORAGE_IMPORT_EXPORT.md) | 工程持久化、依赖识别、原件保全、工作包租约、单个及批量原件导出 | A1：真实保存调用链、大文件通道和最小存储设计 |
| AI 2 | [F3D 后端与原生进程](C:/Users/admin/Desktop/Prism-main/Prism-main/docs/model-library-tasks/AI_2_F3D_BACKEND.md) | 原生运行时、四格式读取、预览、封面、统计、线框、取消与崩溃隔离 | B1：固定运行时后用真实文件完成最小读取与预览实验 |
| AI 3 | [UI、贴图与管理接线](C:/Users/admin/Desktop/Prism-main/Prism-main/docs/model-library-tasks/AI_3_UI_WORKFLOW.md) | 已有页面延伸、有界列表、贴图操作、选择、现有管理接线 | C1：状态图、按钮条件和会话边界验证 |
| AI 4 | [日志与独立验收](C:/Users/admin/Desktop/Prism-main/Prism-main/docs/model-library-tasks/AI_4_LOGGING_ACCEPTANCE.md) | 现有日志接入、独立期望、真实任务、故障和规模实测 | D1/D2：独立样本、原件清单和当前版本基线 |

AI 4 负责独立核查与证据，不代替当前主对话的集成和最终裁决。现在不创建额外聊天、不自动向其他聊天发消息；由用户选择何时分发文档，再把结果带回本对话。

## 3. 我已经搭好的架子

源目录：C:/Users/admin/Desktop/Prism-main/Prism-main。该目录当前没有可读取的 Git 提交号，交接时必须记录实际源文件版本或哈希，不能虚构提交号。

| 已有文件或位置 | 已完成范围 | 尚未完成 |
|---|---|---|
| prism/model_library/contracts.py、ports.py | Qt-free 数据类型、端口、能力开关、任务和修订身份 | 尚未成为工程持久化格式；部分业务端口需要集成时补充 |
| prism/model_library/service.py | 可注入 repository 的查询门面；未实现操作明确抛错 | 真实项目装配、异步作业与编辑流程 |
| prism/model_library/f3d_backend.py | 无原生导入的禁用占位后端 | F3D 运行时、进程和渲染全部未实现 |
| prism/model_library/events.py | 接入已有 performance_trace 的 model_ 事件入口 | 完整事件、进程日志、故障与性能验收 |
| prism/widgets/model_library.py | 搜索与列表布局、空状态、卡片摘要注入、日志入口 | 分页、虚拟化、真实导入导出及任务状态 |
| prism/widgets/model_details.py | 原有 MaterialSlot 复用、详情和文件页签、只读标签备注 | 完整记录加载、可编辑外观、真实文件和统计 |
| prism/widgets/model_preview.py | 工具栏、viewport_host 和大预览窗口壳 | 原生视图、相机、线框、封面交互 |
| material_cards.py 与 assets/materials/cards.js | 共用卡片的模型摘要入口、封面和可用性显示适配 | 十万级有界安装和缓存预算尚未实现 |
| __main__.py、window_workspace.py、资源树 | 材质库下方的模型入口、页面和右侧面板安装、既有导航提交门 | 模型分类与可写管理未接入 |

当前 ModelLibrary 默认没有真实模型 repository，业务按钮禁用。没有模型导入、原件存储、保存重开、标签写入、F3D 加载、真实封面或原件导出。没有假模型填充生产页面，也没有用内存列表假装持久化。

资源树的 UI_SECTIONS 有 model，WorkspaceService.TREE_SECTIONS 没有 model。这是保存链路未完成时的保护。不能仅向 TREE_SECTIONS 加一个值就开放目录创建、拖入和可写标签。

当前 ModelQuery 默认 limit=128；MaterialCards.set_display_assets 只证明可展示已准备的摘要，不证明完整分页、全量排序、跨页选择或十万级能力。

当前 UI 的 _project_id 只用于清理视图，不能充当异步安全身份。同一工程再次打开也必须改变 open_epoch／项目会话。

## 4. 总体架构和接线边界

~~~text
Prism 主进程
  现有导航、资源树、标签、备注、材质控件
      ↓
  模型列表与详情 UI（AI 3）
      ↓
  项目作用域 ModelLibraryService（当前主对话集成）
      ├─ Repository / 工程保存 / 原件包（AI 1）
      ├─ 导入 / 原件导出作业（AI 1）
      ├─ NativeModelJobController / F3DBackend（AI 2）
      │      ↓ 不可变请求、依赖工作包、修订身份
      │   独立 F3D 辅助进程（AI 2）
      │      ├─ 离屏封面与检查
      │      └─ PyQt6 大预览窗口，复用 AI 3 的窗口壳
      └─ 现有 Trace / 诊断入口（AI 4）

结果 → 主进程核对会话、模型和修订身份 → 安装封面／统计或提交项目
独立验收（AI 4） → 实际证据与缺陷 → 当前主对话复核
~~~

主进程是工程数据的唯一写入者；原生辅助进程不持有数据库连接或 live scene。隔离后的大预览是复用现有视觉的顶层窗口，不把另一个进程的 QWidget 塞进主进程。AI 3 的窗口类可以在辅助进程实例化；AI 2 在那个进程中创建并安装 F3D widget。IPC 携带模式、相机、外观和关闭事件，不能传 QWidget。

公共 contracts.py、ports.py、service.py、项目服务最终工厂及跨角色装配由当前主对话负责。四个 AI 提交最小接口需求、确切签名和可接入实现；不各自复制公共协议。四份文档的文件所有权是修改边界，其他文件的需求先写入本角色汇报。

ModelAppearance、unknown_fields 等 Mapping 字段不是深度不可变。提交后台或跨进程前复制、验证、固定快照。结果按 project_session、open_epoch、model_id、request_id、source_revision、appearance_revision、camera_revision 校验；过期结果不能覆盖当前状态。

F3D 本地源码位于 C:/Users/admin/Downloads/f3d-master/f3d-master，声明 4.0.0。正式运行时必须固定真实构建、reader 插件、Qt/Python ABI 与许可证；不得依赖用户 Downloads 路径。Babylon 材质对象不能直接传给 F3D，贴图参数需要显式映射。纯线框、准确统计、SSS 等能力必须分别取证。

## 5. 可以怎么并行，什么时候必须等

| 阶段 | AI 1 | AI 2 | AI 3 | AI 4 | 当前主对话工作 |
|---|---|---|---|---|---|
| 第一轮可同时开始 | A1 保存链调查；A2 最小持久化 | B1 原生最小验证 | C1 状态边界；C2 有界列表与选择 | D1 独立期望；D2 基线；D3 日志规范 | 接收最小接口需求、冻结版本、纠正冲突 |
| 第二轮 | A3 依赖导入；A4 租约工作包；A5 原件导出 | B2 参数；B3 线框统计；B4 作业隔离 | C3 导入导出呈现；C4 外观快照 | 原件核对、故障夹具、规模数据准备 | 装配已交付端口；把真实事件接入 UI |
| 依赖到齐后 | 补齐失败恢复、迁移和部分保存证据 | B5 封面与预览；B6 打包 | C5 真预览；C6 可写管理；C7 回归 | D4 真实任务；D5 故障；D6 规模；D7 结论 | 实际合入、复核端到端、做最终取舍 |

上述第二轮不是等待第一轮全部结束：能独立做的继续做，依赖项标明“等待哪个产物”。

关键门槛：

1. AI 2 可用自己的真实样本做读取实验；正式业务接入必须使用 AI 1 的 PreparedBundle 和租约。FBX 依赖提取接口由二者提出精确需求，当前主对话统一接线。
2. AI 3 可用仅测试可见的 fake 验证显示状态；上线的导入、编辑、导出必须接真实服务。不能用 fake 绕过未完成的保存。
3. AI 1 的全量及部分保存、快照、未知字段、并发编辑确认和恢复未完成前，AI 3 不开放模型可写管理。
4. AI 2 的四格式、参数语义和独立进程未取证前，不宣称真实模型预览完成。外观更新与封面共享相同快照及版本。
5. AI 4 可以早做日志、基线和故障设计；真实端到端与规模验收等依赖到齐后执行，不能把模拟结果写成通过。

## 6. 如何防止多人互相覆盖

- 使用同一目录时严格按四份文档的文件所有权修改。新发现的交叉路径先报告，不能顺手重构整个文件或另一模块。
- 使用独立副本时记录共同基线、改动文件和可应用补丁；交回补丁及必要新文件，不能拿整份旧目录覆盖当前工程。
- 当前目录没有 Git，不要求伪造分支／提交；可交精确 diff、文件哈希、命令和产物。若后续在可用 Git 副本开发，记录真实提交。
- 每轮先交本角色第一个检查点，再扩大实现。接口不足写 A/B/C/D-RFC：具体任务、最小签名、返回语义、取消、错误、持久化与验收影响。
- 现有行为测试失败要保留并定位。旧脚本问题和旧测试契约单独说明，不能统一删断言或更新快照消除失败。
- 每个 AI 更新自己任务文档末尾的执行记录，不去改别人的报告或替别人宣布完成。

## 7. 如何向我汇报，我如何接着集成

每份角色文档都有专用汇报模板。把完整汇报、修改文件或补丁、运行命令、原始结果路径带回本对话；只说“写好了”“测试全绿”不算交付。中途可按检查点交回，不必等整份任务完成。

我收到后负责：

1. 核对实际文件、接口、数据边界与独立证据；对照原始基线，不把 AI 自述当验收。
2. 处理公共 DTO、service 方法、项目作用域工厂和生命周期装配；保留实际会话与修订判断。
3. 按依赖接入 repository、原件任务、F3D 作业、UI 与日志，补上拒绝、取消和关闭路径。
4. 仅对实现、处理器和验收齐备的能力启用入口；未通过能力保留禁用与原因。
5. 复跑受影响的材质、导航、资源树、保存回归；再核对 AI 4 的真实任务、故障和规模结果。
6. 把缺陷退回相应角色，明确证据和补验内容；最终向用户报告可用范围、未测项、风险与回退办法。

## 8. 已有验证与后续验收不能混为一谈

2026-10-26 的框架验证记录：125 passed，44.94s；共享 cards.js 语法检查通过；9 个新增 Python 模块语法解析通过；导入 prism.model_library 不加载 f3d。模型入口位于材质库之后；框架测试确认未向 workspace_nodes、material_records 写入模型数据。

125 项覆盖以下文件；这是框架及旧行为回归，不是模型业务或十万级通过证明：

~~~text
tests/test_model_library_scaffold.py
tests/test_material_cards.py
tests/test_resource_tree_model.py
tests/test_resource_tree_view.py
tests/test_resource_tree_keys.py
tests/test_resource_tree_integration.py
tests/test_resource_tree_create.py
tests/test_resource_tree_drag.py
tests/test_resource_tree_bulk.py
tests/test_resource_tree_workflows.py
tests/test_navigation_commit.py
tests/test_navigation_async.py
tests/test_material_navigation_async.py
~~~

从项目根目录使用 .venv/Scripts/python.exe -m pytest -q 加上相关文件执行。Windows 无显示会话时，可仅对测试进程设置 QT_QPA_PLATFORM=offscreen、QTWEBENGINE_CHROMIUM_FLAGS="--disable-gpu --no-sandbox"，不改用户配置。真实 GPU 预览另测，不能用 offscreen UI 测试替代。

[框架截图](C:/Users/admin/Desktop/Prism-main/Prism-main/artifacts/model-library-scaffold/model-library.png)只证明布局，未使用用户真实工程，也未植入演示模型。

最终必须覆盖：四格式真实导入与原效果、外观保存重开、原件及依赖字节核对、搬迁和部分保存、保存期间新编辑、过期与取消、原生崩溃、单个及批量导出、旧材质与管理回归。AI 4 文档列出独立样本、负载条件、参考硬件、P50/P95/P99、最长停顿、资源峰值与趋势要求；所有阈值是待验证目标，不是当前成绩。

未验证项继续明确写“未测／无法判断”。不能用测试数量、截图、缓存命中或原生 API 调用成功代替端到端交付。
