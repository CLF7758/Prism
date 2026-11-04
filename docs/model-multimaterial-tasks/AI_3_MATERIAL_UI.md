# AI 3：模型多材质编辑界面与用户工作流

## 1. 项目与任务

Prism 是已有材质库/模型库的 PyQt6 桌面软件。你只实现模型内部多材质的选择、编辑、替换和反馈，沿用现有 UI、颜色、卡片与槽位，不重做模型库和材质库。

目录 `C:/Users/admin/Desktop/Prism-main/Prism-main`，Python `.venv/Scripts/python.exe`。先读 `docs/MODEL_MULTI_MATERIAL_FRAMEWORK_20261027.md`。主对话负责集成。默认离屏，禁止控制用户桌面。

## 2. 已搭框架与真实状态

`widgets/model_material_panel.py` 已有列表容器、状态、替换/独立/恢复的禁用按钮，没有数据、选择模型或真实编辑。`ModelDetails.material_panel` 已挂在右侧“材质”页上方；下面标明旧全模型外观兼容区。换 summary/record 会 reset 容器，防止旧状态残留。

`service.multimaterial` 默认不可用。新 contracts/ports 只是接口，不是已完成服务。不要把旧全模型 appearance、全局覆盖或演示数据接进去伪装局部完成。

## 3. 文件所有权

可改 `widgets/model_material_panel.py`；新增 `widgets/model_material_editor.py`、`model_material_picker.py`、`model_material_list.py`、测试 `tests/model_library/multimaterial/test_ui_*.py`。

旧 model_details/model_library/material_cards 的必要变化交最小候选补丁，由主对话整合。contracts/ports、composition、存储、运行时不改；参数体系和主题不复制。任何接口缺项发 RFC。

## 4. C0 接手与设计核对

读 MaterialSlot、ModelDetails、MaterialCards、材质库预览/选择、ModelSelection 与 AppearanceSession。记录哪些控件可直接复用，哪些旧信号只作用全模型。

先用明确标成测试的端口替身驱动 UI；生产服务仍禁用。不要在生产代码里加假材质、静态计数或默认成功回调。给主对话提供界面状态表和屏幕宽度/滚动方案。

## 5. C1 材质列表、范围与选择

1. 展示未解析/解析中/失败/缺图/部分支持/可编辑，未知不叫 0。
2. 列表展示实际使用的工作材质，未使用定义折叠，未指定组另列。显示名称、使用点数、缺图/修改状态，材质小预览只加载可见/预取行。
3. 选材质后显示“当前材质，影响 N 处”；模型高亮请求发给端口，不能自己猜 actor。材质选择与具体部位选择是不同状态。
4. 只有明确选到使用点、后端确认可重绑时启用“仅此部件独立”。否则说明缺失条件，不能随便选某个部位。
5. 大预览拾取结果按稳定 binding_id 同步；透明候选和旋转工具互不抢占，实际鼠标事件接线由主对话装配。

切模型、切工程、关闭、异步过期时清理旧选择/草稿上下文。不能只比较 model_id，完整会话与修订必须匹配。

## 6. C2 复用槽位编辑器

每个目标工作材质下复用现有 MaterialSlot。建立单一有效状态视图，不复制一套颜色空间/通道/参数规则。展示原始、快照、本地覆盖来源；纯数值合法。

三态 UI：重置继承、设置、明确移除。禁用槽位、值为 0、无贴图不是同一回事。清除图后仍允许数值。名称改动不重分配材质。

旧兼容全模型区域暂保留，不与新局部编辑同时启用到让用户误操作。真实多材质能力贯通后，主对话按迁移状态隐藏/折叠旧区；不要现在删掉旧数据或断旧路径。

连续滑条属于同一 gesture_id，预览代次递增，提交与撤销各一次。文本遵循字段编辑完成。失败保留用户可恢复草稿，不能悄悄跳回旧值表示成功。

## 7. C3 材质库选择器与替换

复用现有卡片、标签搜索和缩略图，在当前模型上下文打开选择器；不跳走再让用户重新找部位，不用仅名称下拉。

默认“替换表面”，清楚说明保留透明/双面/SSS/置换。高级“替换完整材质”说明关键改变。旧 basecolor A 是镂空时，UI 和请求都不能将它默默覆盖；让 AI 1/2 在数据与显示上证明保留。

选择卡片先临时预览，应用一个事务，取消只取消草稿；选择器关闭不能提交。库材质拖到明确材质行可触发相同预览流程；无目标不覆盖全模型。源库删除后模型仍使用快照，不显示实时链接假承诺。

没有 UV/支持能力时说明具体原因，不要求用户猜为什么没效果。预览小球不能替代模型本体确认。

## 8. C4 保存、恢复与快捷键

准确显示预览中/保存中/已保存封面待更新/失败。服务返回 job_id 不显示保存成功；等持久化回执。原生失败和存储失败分别说明。

恢复一个材质与恢复全模型明确区分，后者恢复原分配。Ctrl+Z 先文本输入，再当前项目历史；别拦截成模型删除/画布撤销。Delete 行为不能误删原件。

通道/高亮/孤立状态不改正式封面。“设为封面”只用已提交的完整材质模式。高级效果按实际能力独立开放，不能 UI 可点但画面不变。

有本地外观修改时显示“原件导出不包含 Prism 外观修改”，不添加转换导出按钮。

## 9. 验收与质量

以用户任务断言：共享影响说明正确、独立只选一处、同名不串、过期结果不落地、清除与继承不同、预览取消没提交、保存失败不假成功、文本 Undo 优先、无目标全禁用。

1/10/100/1000 材质行虚拟化/驻留，未知数不能显示空库；不能每行创建常驻 3D 引擎。主线程不读原件/数据库、不 import F3D。主题用正式 QSS/组件，无第二套白色窗口。

UI 替身通过只证明 UI，不证明真实材质显示/保存。待 AI 1/2 真端口可用后与 AI 4 跑端到端，才交生产接线。截图不代替交互、保存重开和范围核验。

## 10. 交付模板

```text
角色：AI 3；C0/C1/C2/C3/C4 各项状态：
新增/修改文件与相关哈希：
已复用组件与旧兼容路径：
界面状态、目标与影响范围规则：
测试替身验证范围/真实端口验证范围：
实际离屏命令、原始日志：
失败/受阻/未测/缺失端口：
接口 RFC 与主对话接线补丁：
用户操作链与回退说明：
```

执行记录（2026-10-27，AI 3 本轮）：C0—C4 的 UI 与测试替身验收完成；真实端口、真实材质显示与保存仍未接通，生产面板在无端口时全禁用。详见 §11。

## 11. 本轮执行记录（AI 3）

```text
角色：AI 3（模型多材质编辑 UI）

C0 接手与设计核对：完成
  - 读完框架文档、contracts/ports/service、MaterialSlot、ModelDetails、MaterialCards、
    材质库预览/选择、ModelSelection、AppearanceSession。
  - 可复用：MaterialSlot（9 槽位、通道、范围、颜色、翻转、置换中点）、
    material_parameters.slot_options/validate_slot_patch、material_textures.SLOTS/CHANNELS、
    MaterialCards.set_records/set_display_assets/update_thumbnail、库内部拖拽 MIME
    （application/x-prism-library-assets）、场景内存里的材质记录。
  - 只作用于全模型的旧信号：ModelDetails.appearance_changed/texture_requested/
    slot_cleared/material_apply_requested、composition.apply_material 的名称下拉。
    本轮未把这些信号接到局部编辑，旧路径保持原样。
  - 界面状态表、宽度与滚动方案：artifacts/model-multimaterial-ui-ai3-20261027/STATE-TABLE.md

C1 材质列表、范围与选择：完成（测试替身驱动）
  - 未解析/解析中/失败/缺图/部分支持/可编辑分列；未知数量写「—」，不写 0。
  - 工作材质按 working_id 聚合使用点数；未使用定义、未指定组、未解析条目单独成组。
  - 选材质 = 「当前材质，影响 N 处」+ 临时高亮（只调 highlight 端口）；
    选材质与选部位是两个状态（selected_working_id / selected_binding_id）。
  - 「仅此部件独立」需要：已选材质 + 恰好一处使用点 + 后端 assign_material。
  - 大预览拾取按稳定 binding_id 同步（publish_pick_result）；不属于当前材质则拒绝。
  - 1000 行虚拟化：单一 QAbstractListModel，缩略图在途上限 8，按代次丢弃过期图。

C2 复用槽位编辑器：完成（测试替身驱动）
  - 每个工作材质复用同一批 MaterialSlot；参数/通道规则未复制。
  - 三态：继承 / 设置贴图 / 明确清除；清除后数值仍可编辑。
  - 来源标注：原始 / 库快照 / 本地覆盖（数值覆盖）/ 原始值未取得。
  - 滑条拖动 = 一个 gesture_id、预览代次递增、松开一次提交（撤销一条）；
    文本按字段编辑结束提交；切换目标前 flush 手势，旧回调不写新目标。
  - 提交失败保留草稿并给「重试」，不跳回旧值；SSS/置换未验证则禁用并写原因。

C3 材质库选择器与替换：完成（测试替身驱动）
  - 模型上下文内打开；复用卡片（有记录时走 set_records，否则 set_display_assets）、
    标签筛选（复用库标签）、缓存缩略图；不是名称下拉。
  - 默认「替换表面」并写明保留 透明/镂空、双面、SSS、置换、几何分配；
    「替换完整材质」为高级选项并列出改变项。
  - 旧 basecolor 的 A 通道镂空：选择器明示保留，请求带 preserve_alpha；
    数据面是否真的保留需 AI 1/2 证明（RFC-3-5）。
  - 选卡片只预览；「应用」一个事务；取消/关闭只丢草稿。
  - 拖放：接受现有库拖拽载荷，落到明确材质行才预览；无行/批量/模型拖拽一律拒绝。
  - 能力或端口缺失时写明原因并禁用，不要求用户猜。

C4 保存、恢复与快捷键：完成（测试替身驱动）
  - 预览中 / 保存中 / 已保存 / 封面待更新 / 预览失败 / 保存失败 分开显示；
    服务返回 job_id 只显示「等待保存回执」，只有 MaterialCommitReceipt 显示「已保存」。
  - 恢复此材质与恢复全模型是两条不同命令（restore_material / restore_model）。
  - Ctrl+Z：文本控件先撤销文字（面板不抢），非文本区域走项目历史一步；
    Delete 在面板内被吞掉，不会删模型或原件。
  - 高亮/选择/通道切换不提交、不请求封面。
  - 有本地修改时显示「原件导出不包含 Prism 外观修改」。

新增/修改文件（SHA-256 见 artifacts/model-multimaterial-ui-ai3-20261027/hashes.json）：
  - 修改 prism/widgets/model_material_panel.py（容器重写：状态/选择/请求/回执/键盘）
  - 新增 prism/widgets/model_material_list.py（行推导 + 虚拟化模型 + 拖放视图）
  - 新增 prism/widgets/model_material_editor.py（复用 MaterialSlot + 三态 + 手势）
  - 新增 prism/widgets/model_material_picker.py（卡片选择器 + 场景材质源适配）
  - 新增 tests/model_library/multimaterial/{conftest.py,test_ui_list.py,
    test_ui_editor.py,test_ui_picker.py,test_ui_feedback.py,test_ui_identity.py,__init__.py}
  - 未改动：contracts.py、ports.py、service.py、model_details.py、model_library.py、
    material_cards.py、material_slots.py（哈希与框架基线一致）。

已复用组件与旧兼容路径：MaterialSlot、material_parameters、material_textures、
MaterialCards、库标签/缩略图、库内部拖拽 MIME、SceneMaterialLibrarySource（只读内存记录）。
旧全模型外观区未删除、未改保存路径；面板通过 local_editing_changed 通知主对话折叠。
折叠≠写入隔离：旧写入门由主对话统一处理（CANDIDATE-PATCH.md C 节）。

第二轮修补（按主对话意见，2026-10-27）：
  - 一 预览身份：成功/失败/准备完成都核对 request_id + draft_generation；取消后本地 token
    先失效；过期 PreparedMaterialView 的租约当场 release。反例测试：旧代次、重复帧、
    取消后晚到、别的 request 的失败、cancel 只传 job id。
  - 二 提交队列：按 request_id 维护在途提交（pending_commits），回执只确认自己；
    重复回执幂等且不再触发重新读取；未提交过的回执拒绝；旧失败晚到不污染新提交；
    提交期间 catalog 刷新不覆盖草稿；重试=新 request_id + 同 gesture_id。
  - 三 跨材质拾取：从完整分配表解析 binding_id → working_id，先切材质再选部位；
    悬空/未分配/透明多候选分别处理（多候选不猜）。测试覆盖「选木材点金属部位」。
  - 四 取消与关闭拆义：finish_edit（切材质）/ discard_edit（切模型、切工程、关程序）/
    cancel_preview(job id) / release_lease / restore_committed_preview / close_picker；
    取消后请求恢复已提交外观，缺端口时明说画面可能仍是草稿帧。
  - 五 参数与快照契约：目标只走 working_ids/binding_ids；参数集中在
    _command_parameters 并带 command_contract=1；库替换必须带 snapshot_token，
    缺失则连预览都不发并禁用应用；去掉 UI 侧按 channels 推断 preserve_alpha；
    基础材质改由注入 base_source.bases() 提供，不再把快照塞进源 definitions。
  - 六 回传与缩略图边界：subscribe/unsubscribe 幂等（重复接线不重复回调）、
    事件带完整身份；缩略图端口返回 bytes/元数据而非 QPixmap；新增驻留预算
    （64 张，超出释放最旧）；缺图/未加载/无贴图/解码失败/状态未取得分开判定。
  - 七 候选接线：删除 (0,0) 兜底，未知修订返回裸 id 让面板停在「缺少会话/修订」。

测试替身验证范围（只证明 UI）：
  84 passed（test_ui_*.py；连同框架安全门共 87 passed）。覆盖：状态文案、未知≠0、
  共享影响说明、同名不串、独立仅一处、清除≠继承、手势一次提交、失败保留草稿、
  文本撤销优先、卡片预览不提交、取消丢草稿、拖放无目标不覆盖、1000 行虚拟化，
  以及第二轮全部反例（预览乱序/重复/取消后晚到、A 回执不清 B、重复回执、
  未提交回执拒绝、旧失败晚到、提交期间 catalog 刷新、跨材质拾取、多候选不猜、
  五种取消/关闭语义、订阅幂等、缩略图字节契约与驻留预算、贴图状态不猜）。

真实端口验证范围：无。真实材质识别、局部原生显示、保存/重开、导出哈希均未验证。

实际离屏命令与原始日志：
  $env:QT_QPA_PLATFORM='offscreen'
  .venv/Scripts/python.exe -m pytest -q tests/model_library/test_multimaterial_scaffold.py
      tests/test_model_library_scaffold.py tests/model_library/test_ui.py
      tests/model_library/test_appearance.py
      -> 52 passed（regression.txt）
  .venv/Scripts/python.exe -m pytest -q tests/model_library/multimaterial/test_ui_*.py
      -> 84 passed（ui-tests.txt）

失败/受阻/未测/缺失端口：
  - 生产 MultiMaterialService 仍全关：无端口时面板不可用（安全门保留）。
  - 缺订阅/退订正式契约、restore 端口、快照基础视图、贴图状态字段、库摘要令牌、
    正式命令 schema → RFC.md（本轮已修订）。
  - 未做：真实端口端到端、界面截图、真实保存重开、性能阈值（未绑定机器基线）。
  - 归因请求（不再以「不属于本角色」排除）：第二轮实时复现
    `pytest -q tests/model_library/multimaterial` → 6 failed / 305 passed / 11 skipped，
    失败全部在 acceptance/（D1 期望集构建、期望记录、期望文件写出、两条产品对比、
    FBX 双路径声明）。上一轮记录的 test_data_catalog_fbx.py 4 项已随并行修改消失，
    旧清单作废。请 AI 4 按同一源码快照归因；本角色未修改 acceptance/、
    test_data_catalog_*.py 或任何独立期望。原始输出：
    artifacts/.../attribution-report.txt，说明：artifacts/.../attribution-note.md。

接口 RFC 与接线补丁：artifacts/model-multimaterial-ui-ai3-20261027/{RFC.md,CANDIDATE-PATCH.md}

用户操作链与回退：见 STATE-TABLE.md §5。回退方式：不注入端口即回到旧全模型路径，
无需删除任何新状态。
```

