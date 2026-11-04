# AI 3：模型库 UI、贴图操作与现有管理能力接线

日期：2026-10-26。你是实现 AI 3。总集成由当前主对话的 Codex 负责，不再安排集成 AI。

## 1. 最终用户操作是什么

左侧材质库下面进入模型库 → 中间显示模型当前材质效果的封面 → 导入 FBX／OBJ／GLB／glTF → 使用现有贴图槽位或应用材质库里的材质 → 封面更新 → 双击交互查看 → 单个／批量导出原件。

已有标签、备注、分类管理、材质库、PBR 控件和渲染不能另建一套。中间卡片主体必须是模型封面；统计资料放右侧。不要创建材质槽管理页签。

原件与外观分开：修改外观保存在 Prism，导出原件取回导入时的原始文件。不做修改后模型导出，不强制转换 GLB。

## 2. 你接手时已有的真实骨架

根目录：C:/Users/admin/Desktop/Prism-main/Prism-main。

- widgets/model_library.py 已有 ModelLibrary、set_service、load_from_scene、refresh、网格／列表和日志入口。
- widgets/model_details.py 已有预览占位、贴图槽位、版本与文件、只读标签／备注。
- widgets/model_preview.py 已有工具栏和 viewport_host，尚未安装 F3D。
- 模型库卡片复用 MaterialCards，通过 set_display_assets 显示摘要；没有复制卡片渲染器。
- 现有 cards.js 增加 assetKind、metaLabel、dragEnabled、exportEnabled 适配；材质默认行为保留。
- 主窗口已经注册模型页和右侧面板；open_model_library 走现有导航提交门。
- 资源树 UI_SECTIONS 已有 model，WorkspaceService.TREE_SECTIONS 没有 model，这是防止无存储支持就写数据。
- ModelLibraryService 只有注入 repository 后可查询摘要；导入、导出、预览和外观操作都明确未实现。
- 当前摘要默认 limit=128，没有生产分页导航、虚拟列表或全局选择机制。
- 125 项旧框架回归与 UI 截图只证明骨架，不证明业务闭环。

## 3. 文件所有权

你可以修改：

- [model_library.py](C:/Users/admin/Desktop/Prism-main/Prism-main/prism/widgets/model_library.py)
- [model_details.py](C:/Users/admin/Desktop/Prism-main/Prism-main/prism/widgets/model_details.py)
- [model_preview.py](C:/Users/admin/Desktop/Prism-main/Prism-main/prism/widgets/model_preview.py)
- [material_cards.py](C:/Users/admin/Desktop/Prism-main/Prism-main/prism/widgets/material_cards.py)
- [cards.js](C:/Users/admin/Desktop/Prism-main/Prism-main/prism/assets/materials/cards.js)
- [cards.css](C:/Users/admin/Desktop/Prism-main/Prism-main/prism/assets/materials/cards.css)
- 新增 prism/model_library/appearance.py：现有槽位值到 ModelAppearance 的业务适配；不包含 F3D 对象。
- 新增 prism/model_library/selection.py：跨页选择与当前查询身份；不读模型原件。
- __main__.py、window_workspace.py：仅模型入口、详情切换、项目绑定和对应 UI 接线。
- workspace_service.py、widgets/resource_tree_model.py、widgets/resource_tree_view.py、widgets/media_library.py：仅模型资源分区与导航接线。
- widgets/tag_panel.py、project_search.py、search_controller.py：仅模型载体、命中跳转和计数接线。
- tests/model_library/test_ui.py、test_selection.py、test_appearance.py、test_navigation.py。
- 本文执行记录。

material_slots.py 默认只复用、不修改。确需修改共用槽位先报告最小原因，并回归材质库。

不得修改：contracts.py、ports.py、service.py、模型 repository／导入／导出、存储保存文件、F3D 后端与辅助进程、performance_trace.py、diagnostics.py、公共 conftest。

“可修改”不允许整文件重写或顺手重构其他模块。主对话负责最终装配 service 和公共契约；你提交准确的调用需求。

## 4. 你消费什么、提供什么

消费：

- ModelQuery → ModelPage，summary 只用于卡片。
- repository.get(model_id) → ModelRecord，用于详细文件、备注和外观；不能用摘要反向覆盖完整记录。
- ModelCapabilities，真实可用功能由集成后的服务提供。
- Import／Export／Preview／Job 端口；调用返回 job_id 不是完成。
- AI 2 的 create_f3d_widget 与交互事件约定，由主对话发布确认版本。

提供以下展示接口（可在现有类中补齐）：

~~~text
ModelLibrary.set_service(project_bound_service)
ModelLibrary.bind_scope(project_session, open_epoch)
ModelLibrary.publish_page(page, query_identity)
ModelLibrary.publish_job_event(event)
ModelDetails.show_record(record, cached_cover, capabilities)
ModelDetails.collect_appearance() -> detached ModelAppearance
ModelPreviewWindow.install_viewport(widget)
ModelPreviewWindow.set_capabilities(capabilities)
~~~

进程边界必须明确：主进程拥有列表与详情；ModelPreviewWindow 的纯 UI 窗口壳由 AI 2 在独立预览进程实例化，随后在同一进程创建 F3D widget 并调用 install_viewport。install_viewport 不是跨进程接口。主进程使用 PreviewPort 发起打开与更新，模式、相机、封面和关闭事件通过 IPC 返回；不要在主进程实例化 F3D widget，不要把另一个进程的 QWidget 传回来或嵌入主窗口。你负责让窗口壳可以脱离主窗口的全局状态运行，AI 2 负责辅助进程启动与原生适配。

大预览工具栏向宿主发出 view_mode_requested(mode)、reset_requested、cover_requested、close_requested。模式使用现有 ModelViewMode 的字符串值。你不在主进程导入 F3D，也不在 UI 控件里读取模型原件。

导入／导出／编辑等事件最终调用现有 service 的对应端口；若当前接口尚缺 get_record／update_metadata／create_folder 等，提交精确需求给主对话。测试可以注入 fakes，生产不能用假数据替代后端。

## 5. 逐步实施

### C1：先把 UI 状态边界做完整

1. 保留既有布局，不重新设计整个 Prism。
2. 识别：未接入、数据准备中、空库、查询无结果、查询失败、有结果、任务失败。
3. “数据未知”不显示 0；“未接入”不显示“导入成功”。
4. 每个按钮必须同时满足：真实 capability、有效项目会话、有效选择和对应处理器存在。
5. 删除演示状态只限测试夹具；不在生产启动塞样本模型。
6. 当前 _project_id 只能清视图；增加真实会话／代次绑定，不能拿它做完整防串项目机制。

交付 C1：状态图、按钮解锁表、空状态与错误状态测试；原有材质界面不变。

### C2：完成摘要列表与分页，而不是一次塞十万条

1. query 在有明确任务上下文的后台执行；主 GUI 只安装小批摘要。
2. 请求包含 text、folder、sort、offset、limit 及查询修订；新查询使旧查询结果失效。
3. 在共享卡片上实现可见区域复用／有限预取，控制 DOM 和图片缓存；不能只是不断追加48张卡片。
4. 所有结果可访问；不能只显示前128或5000条并把它说成完整模型库。
5. 查询和排序在全库口径完成，再分页；不能只对当前页排序。
6. 滚动、选择、展开、卡片大小按项目和目录保存视图状态。
7. 跨页选择归独立 selection controller，不能让 set_display_assets 的当前页交集悄悄清掉用户批量选择。
8. 选择条显示选择数量；批量导出使用固定的选择／查询身份，不随列表刷新乱变。
9. 卡片只读封面缓存。无封面显示模型占位；不能显示材质球、不能读完整模型生成列表。

交付 C2：第一页至末页可达、快速筛选／排序／滚动、跨页选择与缓存有界的可复现测试。

### C3：接入真正的导入和导出入口

1. 按钮／拖入走同一个 ImportPort；前端不自己复制或解包文件。
2. 显示阶段和成功／失败／取消／缺失数量，避免不可估算步骤的假进度。
3. 任务接受后显示等待，不提前添加“ready”的假模型。
4. 模型域提交后刷新对应摘要，不全量重建所有卡片。
5. 右键、右侧和多选导出调用同一 ExportPort，明确只有“导出原件”。
6. 导出失败展示缺失项和任务编号；批量部分失败不能报全部成功。

交付 C3：真实端口未就绪时继续禁用；就绪后按钮、右键与拖放行为一致。文件操作证据归 AI 1，你验证 UI 状态和调用。

### C4：复用现有贴图槽位完成外观操作

1. 从完整 ModelRecord 读取外观，不从卡片摘要拼接。
2. 使用现有 MaterialSlot、material_parameters、material_textures 的参数约定。
3. collect_appearance 输出脱离控件和活字典的快照，包含原件引用、通道与属性。
4. 应用现有材质时保存当次配置快照，不让以后修改源材质自动改变所有模型。
5. 首版以整模型覆盖为范围；导入的原有多材质先正确显示。不自行追加部位编辑器。
6. 连续滑动合并为最新外观修订；需要撤销的用户动作按实际编辑动作分组，不能逐像素堆撤销。
7. 旧封面可以保留等待，但清楚标“更新中”；新修订封面到达再换。
8. 只有 AI 2 验证有效的槽位开放；SSS／置换等不支持项明确禁用，不能静默忽略。
9. 外观修改走 service 的受控保存；未完成持久化前不能把输入框变成可写。

交付 C4：同一模型的槽位配置、封面、大预览和保存重开一致；修改不串模型、不改原件和源材质。

### C5：完成详情和大预览宿主 UI 接线

1. 右侧显示格式、原件大小、统计及 basis、文件依赖状态。
2. None 显示未取得，Cells 不显示成三角形；同一属性的统计口径可查。
3. 大预览沿用既有外观，安装 AI 2 的 F3DWidget；仅在辅助进程安装。
4. 工具栏发命令给宿主；纯线框由真实能力解锁。
5. 回正、缩放、窗口变化、设封面和返回操作验证；关闭时通知宿主释放。
6. 主窗口项目切换后旧窗口／旧封面不能继续提交到新项目。

交付 C5：真实模型用户操作录像／步骤与事件；截图只作为视觉辅助。

### C6：接现有分类、标签、备注和搜索

前提：AI 1 模型域的全量／部分保存、删除恢复已经通过验收并由主对话确认可用。

1. 扩展 WorkspaceService 的 model 分区校验，只允许模型文件夹，不把模型创建为画布页。
2. 资源树创建、重命名、移动、删除恢复走现有管理与模型域约定。
3. 模型绑定现有标签体系；增加 model 载体和计数，不再建一个标签库。
4. 备注／名称编辑需防抖、保存反馈与 expected_revision；切换选择先正确提交。
5. 全局搜索增加模型结果与 reveal_model 定位，不改现有画布／材质结果语义。
6. 模型数只计条目，不把贴图、BIN、封面都算成模型；总数、命中数和已显示数分开。
7. 进入／退出模型库使用既有导航提交门，保留其他模块未完成编辑。

交付 C6：目录、标签、备注、搜索跳转、计数、导航与删除恢复端到端结果。

### C7：交回主对话集成

提供所有事件／控件到服务的接线表、能力开关条件、交互宿主所需信号。
不要把尚缺服务支持的处理器写成 pass 后说完成。可交付“UI已准备，等待端口X”，同时保持按钮禁用。

## 6. 并行安排

- C1、C2 可立即与其他 AI 同步；使用只存在于测试内的 repository。
- C4 的参数快照可先做；真实效果需 AI 2 能力表。
- C3、C6 的生产启用需 AI 1 保存与原件端口通过。
- C5 由 AI 2 提供 widget／事件后接线；你不改辅助进程文件。
- contracts／ports／service 由主对话统一修改；你报告签名需求。
- 共享卡片文件只有你改，其他 AI 只能提建议。

## 7. 验收

产品：

- 原材质库布局和行为保持；左侧模型库位置正确。
- 卡片主体是模型当前效果封面；元数据主要在右侧。
- 没有新的材质槽管理、另一套标签系统或 GLB 转换选项。
- 入口、按钮、快捷键、右键、拖入与错误状态一致。

正确性：

- 快速选择、筛选、翻页、切项目，旧查询／旧封面不串回。
- 跨页多选不会悄悄丢失，导出选择数量与范围明确。
- 保存期间新编辑保留；部分项目不误删内容。
- 只读／尚未接入状态不能写项目。

资源／性能：

- 不加载所有模型原件、不为每张卡片创建引擎。
- R0机器（13900K／128GiB／4080 SUPER／NVMe）已建索引十万条查询目标 P95≤200ms、P99≤500ms；滚动／选择P95≤100ms、P99≤200ms；主GUI最长停顿≤200ms。
- DOM目标≤300个可见／预取卡片，图片缓存预算目标≤256MiB。
- 未有真实十万数据就写未测，不能用小样本宣称通过。

回归至少：

~~~text
.venv/Scripts/python.exe -m pytest -q tests/model_library/test_ui.py tests/model_library/test_selection.py tests/model_library/test_appearance.py tests/model_library/test_navigation.py
.venv/Scripts/python.exe -m pytest -q tests/test_model_library_scaffold.py tests/test_material_cards.py tests/test_resource_tree_model.py tests/test_resource_tree_view.py tests/test_resource_tree_keys.py tests/test_navigation_commit.py tests/test_navigation_async.py tests/test_material_navigation_async.py
node --check prism/assets/materials/cards.js
~~~

测试需按已实现行为更新脚手架断言，但不删除“不能假成功／不能写未支持项目域”的边界测试。报告每项测试变化的原因。

## 8. 汇报模板

把结果交给用户带回当前主对话，由 Codex 检查集成；不另设集成 AI，不擅自向别的聊天发消息。

~~~text
角色：AI 3
状态：C1…C7逐项通过／失败／未测／受阻
工作目录／提交／补丁：
修改文件：
UI到服务接线表：控件、事件、调用、能力门槛、错误反馈
新增展示接口与宿主信号：签名和最短例子
已启用／仍禁用能力及原因：
数据保存依赖：已验证哪些、哪些尚待AI 1
F3D依赖：采用哪个AI 2接口／版本，缺什么
实际用户任务：步骤、结果、录像／截图辅助、日志路径
测试命令与结果、失败和未测：
分页／选择／缓存／性能实测：
材质库与导航回归：
需要主对话集成的具体位置：
限制和下一步：
~~~

接口变更请求：

~~~text
编号：C-RFC-01
需要的服务方法／字段：
当前无法完成的用户动作：
最小签名与错误语义：
消费者与兼容影响：
可继续的独立任务：
~~~

## 9. 执行记录

- 当前：C1／C2 完成；C4 完成；C3／C5／C6 的 UI 侧完成、端口侧按能力门保持禁用（等主对话装配 service 与 AI 1 验收）；C7 见"待集成事项"。67 个新用例 + 111 个回归用例通过，`node --check cards.js` 通过。十万条真机性能未实测（见限制）。
- C1：状态机 `unavailable/loading/empty/no_results/error/ready` 全部具名，`library.state` 可读；空库与"查询无命中"分开（`_is_filtered()`）；未知统计显示"未取得"而不是 0；按钮统一走 `_blocking_reason(capability, handler, needs_selection)`，提示只说第一个真正缺失的前提。实测缺陷：过滤后 0 条曾被报成"模型库为空"（已修）。
- C2：`PAGE_SIZE=72`／`MAX_WINDOW_PAGES=4` 的窗口分页，首末页可达（300 条 5 页走查，卡片 ≤288）；查询在 `QThreadPool` 后台跑，主线程只装小批摘要；`QueryIdentity`(text/folder/sort/revision) 不一致即拒收；跨页选择归 `ModelSelection`，卡片只镜像可见部分。实测缺陷：①点卡片后选择条永远是"未选择"（漏调 `_refresh_selection_bar`）；②`bind_scope` 只涨 revision 不改 `_identity`，切项目后旧摘要页仍能落地（已修）。布局/卡片大小/排序/滚动/焦点按"项目+目录"存窗口 settings，不进工程数据。
- C3：导入按钮、拖放、`import_paths` 同走 `service.import_paths`，前端不复制解包；接受后只说"已提交…等待后台完成"，不预插 ready 模型；导出用 `selection.freeze()` 固定快照；`publish_job_event` 区分 queued/running/completed/failed/cancelled/stale，`missing` 非空报"部分完成 N 项缺失"并带 job id。端口未就绪时按钮保持禁用。
- C4：复用 `MaterialSlot`／`material_parameters`／`material_textures`；`collect_appearance` 输出脱离控件与活字典的快照；应用材质时立刻复制配置。实测缺陷：①每条 slider patch 都保存一次（C4.6 禁止）→ 320ms 合并、一次动作一个修订，切选择先 flush、切项目丢弃未提交编辑；②`_load_slots` 把 `None` 传给 `MaterialSlot.show_binding`，选中无贴图模型直接崩溃（按材质库约定改传 `{}`）；③SSS／置换等未验证通道带原因禁用。
- C5：右侧显示格式、原件大小（取主文件）、三角形/顶点/basis、依赖状态、标签备注；`None`→"未取得"，Cells 不改写成三角形。大预览窗口壳支持 `install_viewport`／`set_capabilities`，`wireframe` 只在真实能力后可选，`closeEvent` 发 `close_requested`。新增项目切换钩子：`project_open_finished` → `bind_scope(refresh=False)`，旧项目的摘要、封面、记录、选择与未提交外观编辑一并作废。
- C6：模型分区校验（`model_section_enabled` 门、`createPage('model')` 拒绝）；门未开时不画"+"、右键不给"新建"、分区行不接受拖放；模型数只计条目并发布到分区行（未知为 `None` 不显示 0）；备注编辑防抖 + `expected_revision`。实测缺陷：`_BlankAreaDelegate` 调不存在的 `self.model()`，一画模型分区就 AttributeError，拖垮 8 个既有测试（改为问所属视图）。**未做**：模型标签载体、全局搜索模型结果与 `reveal_model` —— 前提是 AI 1 模型域保存通过验收并由主对话确认，目前未获确认，保持禁用。
- C7：见"待集成事项"与本次汇报。
- 待集成事项：
  1. `ModelLibraryService` 需由主对话按能力逐项装配，并提供 `appearance_slots`（槽位→阻塞原因）与 `get(model_id)`；页面按"capability + handler + 会话 + 选择"四条件解锁。
  2. `ModelLibrary._record_getter` 优先用 `service.get`，否则退回 `service.repository/_repository.get`；主对话若改装配方式请保持二者之一可用。
  3. AI 2 需在预览进程实例化 `ModelPreviewWindow` 后调 `install_viewport(f3d_widget)`，并把 `view_mode_requested/reset_requested/cover_requested/close_requested` 经 IPC 回传；主进程只用 `PreviewPort.open/update_appearance`。
  4. `F3DBackend` 构造签名已变为 `F3DBackend(runtime_config, controller=None)`（AI 2 改动）。脚手架测试 `test_unimplemented_operations_do_not_report_success` 已按新签名注入"探测不可用"的 controller，保留"未验证运行时不得假成功"边界，未删除任何断言。
  5. 模型标签与全局搜索需主对话先给"模型条目 + 标签"数据源（AI 1 存储验收后），此前不接死代码。
