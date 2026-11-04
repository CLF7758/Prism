# AI 1：模型多材质数据、资源与事务

## 1. 任务与项目

你在 Prism 的模型多材质编辑工作中负责数据，不是重做模型库。项目是 Python/PyQt6 桌面软件，已有材质库、目录、标签、模型原件存储和原件导出。用户要导入后识别原材质，局部修改并可靠保存。

目录 `C:/Users/admin/Desktop/Prism-main/Prism-main`，Python `.venv/Scripts/python.exe`，当前无 Git。先读 `docs/MODEL_MULTI_MATERIAL_FRAMEWORK_20261027.md`，它是共同契约。主对话负责集成，没有其他集成 AI。

## 2. 已有什么，缺什么

已搭 `multimaterial/contracts.py/ports.py/service.py`。service 所有执行方法抛未实现，不含调度、校验、存储。旧 ModelAppearance 仍是全模型，旧 ModelLibraryService 暴露 `.multimaterial` 但没有真实实现。

现有附件走 `project_storage.py` 的 ModelResourceStore；模型记录在现有工程 SQLite。`register_model_attachments/model_attachment_ids` 只遍历 files 和顶层 bindings。你必须修通嵌套材质引用的保存/清理链；unknown_fields 只保留数据不等于资源会被保留。

## 3. 文件所有权

可实现的新文件：`prism/model_library/multimaterial/catalog.py`、`inspectors/`、`state_store.py`、`resources.py`、`validation.py`、`service.py`、`commands.py`。单元测试放 `tests/model_library/multimaterial/test_data_*.py`。这些是建议新模块，当前尚不存在，禁止当已有 API 调用。

共享 contracts.py/ports.py 只读；需变更先发 RFC。旧 repository/project_storage/partial_save/partial_project/composition 和主 ModelLibraryService 的接线由主对话处理：你提交最小补丁和完整调用点清单，不能并行重写。不得修改 AI 2 运行时、AI 3 UI 或 AI 4 期望文件。

## 4. A0 接手与基线

1. 核实目录与文件哈希，阅读 contracts、appearance、service、project_storage、repository、bundles、exporter 及部分保存调用链。
2. 用离屏环境跑现有相关回归，保留原始输出。区分旧失败、其他角色改动和你的新增问题。
3. 写明真实附件提交、索引注册、清理、另存为、目录工程转换、历史保留分别由谁负责。不要只画类图。
4. 提交持久化 schema 与命令参数 RFC：请求参数 JSON 的格式、类型、未知字段、原子范围、错误码、回执事件、取消和资源租约。通过主对话后冻结，避免 AI 2/3 各自猜。

## 5. A1 解析语义清单

- glTF/GLB：materials、primitive 分配、node 实例路径、textures/images/bufferView、外部 BIN、UV/变换/sampler/扩展；原始二进制不重写。
- OBJ：多 mtllib、newmtl/usemtl 与面组、相对/绝对路径和 MTL 选项；同名冲突可定位；bump 不当 normal，Ks 不当 roughness。
- FBX：Material/Texture/Video/Connections、Geometry 材质映射/引用模式、节点实例、嵌入资源。现有依赖扫描不是完整材质分配解析，数组跳过逻辑不能直接当答案。
- 纯数值、无材质、未使用定义、缺图均为可表示结果；解析失败不得返回空清单假成功。
- 身份基于固定 source_revision 与明确格式定位，不按名称或 actor 序号。跨版本映射保留歧义，不强配。

交付：四格式可追溯清单，独立期望对照，未支持属性/扩展列表。某格式受阻就交证据，不能编清单凑数量。

## 6. A2 工作材质、覆盖与存储实验

实现语义：源定义只读；工作材质本地 UUID；使用点到工作材质分配；SET/CLEAR/INHERIT；数值缺省/0/禁用区别；仅此部件独立复制状态但不复制资源字节。

基础快照与覆盖有固定优先级：原材质/已应用库材质快照 → 当前工作覆盖 → 未提交草稿。所有状态都可解释来源。库快照依赖托管，不实时链接源库。

先对 1/10/100/1000 材质比较稀疏模型 JSON 与细粒度表原型，测单槽修改读写/WAL/序列化/恢复；不预设换数据库。建议完整清单受管、模型引用、稀疏覆盖，但具体磁盘 schema 经主对话确认。

新数据必须接完整/局部保存、另存、目录工程转换、嵌套未知字段保留。禁止 GENERATED 列破坏现有转换。旧 appearance 保留显式兼容，不自动污染每个材质。旧程序兼容必须实际证明，不靠新增版本字段自行宣称。

## 7. A3 原子提交、历史与资源

1. 暂存新图，核对字节与哈希；校验目标会话、所有修订及命令参数。
2. 原子提交状态、分配和资源引用；版本冲突拒绝覆盖。多目标命令全成或全不成。
3. 提交成功才产生持久化回执；UI/原生失败不伪装保存失败或把已提交数据丢弃。
4. 接项目命令栈，每个真实手势一个命令。撤销/重做采用正确逆操作与新修订，不把版本倒退；异步旧回调不能再落地。
5. 保存期间新编辑进入下一修订；旧回执不能清新脏标记。崩溃恢复能识别已提交未通知。
6. 资源遍历覆盖源清单、工作材质、继承、显式新图、撤销/重做、预览租约、回收站。取消只清自身暂存，历史释放才回收独占引用。

证明：应用库材质后删源库，再保存重开仍完整；撤销到旧图仍可读；另存/搬迁不依赖原磁盘；原件导出哈希不变。不要把“合并了滑条事件”当完成 Undo。

## 8. A4 门面协调与日志

实现 MultiMaterialService 的异步 inspect/preview/commit/cancel，经端口依赖注入。job_id 只表示已受理。明确 queued/running/completed/failed/cancelled/stale 的事件和终态；取消不撤回已持久化事务，需撤销命令。

准备在后台，Qt 安装经已有 dispatcher，原生操作去 AI 2 端口。真实能力须同时满足数据、渲染、持久化、会话和目标；不能只配置布尔值就打开编辑。能力缺失说明第一缺失条件。

日志复用 model_library.events，记录身份/修订/命令/阶段/耗时/错误，不写原始字节。与 AI 4 核对事件契约。交可装配实现给主对话；不能擅自把服务换进用户真实工程。

## 9. 验收与质量硬门

独立清单正确；共享与拆分只影响指定范围；三态明确；新资源完整保存重开；未知字段保留；事务故障无半状态；历史资源不丢；过期结果拒绝；原件哈希一致。

不 import Qt/F3D，不保留全局当前模型，不在 UI 做 I/O，不默默清空不认识的数据，不吞错误报成功。文件名不能替代资源身份。禁止扩大到整个导入导出重构。

## 10. 汇报与交付模板

```text
角色：AI 1
阶段：A0/A1/A2/A3/A4；每项通过/失败/受阻/未测
工作目录、相关文件 SHA-256：
修改文件及目的：
schema/接口 RFC 与当前冻结版本：
实际命令、环境、原始日志路径：
已证明的用户结果与适用格式：
故障和资源保留证据：
仍未证明、旧失败、外部依赖：
交主对话的最小装配补丁与顺序：
回退方法（不得丢新数据）：
```

执行记录：本任务书建立时，仅框架存在，A0—A4 均未验收。不得改成已通过直到有证据。

---

## 11. 执行记录（AI 1，2026-10-27）

| 阶段 | 状态 | 证据 |
| --- | --- | --- |
| A0 基线 | 通过 | `AI_1_A0_BASELINE.md`；接手时 `277 passed, 1 skipped`，改动后 `475 passed, 2 skipped, 1 failed`（唯一失败是 AI 4 的 D6 日志字段，属归还项），日志 `artifacts/model-multimaterial-ai1-20261027/model-library-regression.txt` |
| A0 归属 | 通过 | 同文件 §3：字节提交 `project_storage.py:584`、索引注册 `project_storage.py:512` + `composition.py:230`、清理 `fileio/sql.py:1773`、局部保存 `partial_project.py:748-784`、目录转换 `storage/migration.py:181/224`、历史 `service.py on_commit` |
| A0 RFC | 已提交，**待主对话裁决冻结** | `AI_1_RFC_PERSISTENCE_AND_COMMANDS.md`（含 §7 的 6 项裁决/补丁请求） |
| A1 解析 | 通过（可交付） | `multimaterial/catalog.py`、`multimaterial/inspectors/`（gltf/glb/obj/fbx/locator/派发）；测试 `tests/model_library/multimaterial/test_data_catalog_{gltf,obj,fbx,codec}.py` 41 条；AI 4 独立期望对照 `38 passed`（`d1-acceptance-against-product.txt`） |
| A1 未支持属性清单 | 已写入 `inspectors.PARSER_LIMITS` 与各定义的 `unsupported` | 同上 |
| A2 工作材质/存储实验 | **未做** | 依 RFC 冻结后开工 |
| A3 原子提交/历史/资源 | **未做** | RFC §7.4/§7.5 的补丁需先由主对话落地，否则新贴图会在保存/清理链里丢 |
| A4 门面/日志 | **未做** | `MultiMaterialService` 仍默认不可用；`events.IDENTITY_FIELDS` 待补（RFC §7.3） |

未测：另存为、目录工程转换、清缓存后重开、崩溃恢复、撤销/重做的资源保留。以上均不得按已通过计。

---

## 12. 执行记录（AI 1，2026-10-27 第二轮，按主对话退回意见修订）

退回的五类问题逐条落地，**A2 已做**，A1 三处修正已做，A3/A4 仍未做。

| 条目 | 状态 | 证据 |
| --- | --- | --- |
| 一、绑定 resolution 三态 | 语义已改：悬空引用**保留**在 `binding_sites`（保留原始 `usemtl:` 名），状态由 `catalog.binding_resolutions()` 提供；契约字段待裁决 | `catalog.py`（`bindings_by_resolution`/`dangling_bindings`）、`test_data_catalog_obj.py::test_obj_dangling_usemtl_keeps_its_usage_point_with_a_state`、RFC §8.1 |
| 二、文本编码策略 | 改为字节解析：UTF-8/BOM/UTF-16 确定；其他标"不确定"并报告候选；坏字节标 `damaged`；**身份用原始字节**（`名字~hash`） | `inspectors/text.py`、`test_data_catalog_text.py`（10 条：UTF-8/UTF-16/GB18030/损坏字节/替换字符碰撞/跨编码不合并） |
| 三、槽位词表 | 唯一来源 `vocabulary.py`：`STANDARD_SLOTS`(9) / `SOURCE_ROLES`(7) / `editable_slots(verified)`；SSS/置换默认不可编辑 | `vocabulary.py`、`test_data_state.py::test_the_ui_gets_three_states_per_slot` |
| 四、存储布局 | 不冻结。四方案实测（1/10/100/1000 材质 × 50 次单槽修改）：A 内嵌写 P95 17.2ms 且每次增长 2.2MB；**B 受管清单+稀疏覆盖写 P95 0.087ms、增长 16KB**；C 细粒度写 0.034ms、恢复 3.5ms | `AI_1_A2_STATE_STORAGE.md`、`artifacts/.../a2-storage-experiment.json`、`a2-storage-table.txt` |
| 五、资源闭环 | 引用根表（catalog/working/snapshot/history/lease/trash/extra）、七段生命周期、目录工程与 SQLite 事务边界（不宣称跨文件系统原子） | RFC §6、`state_store.collect_references`/`orphaned_resources`（有测试） |
| 六、命令歧义 | 目标只从 `working_ids`/`binding_ids`；`keep`/`apply_defaults` 一律拒绝；`restore_material` 不碰分配；`make_unique` 同源一份/异源各自一份；幂等按内容签名 | `validation.py`、`test_data_state.py` 请求相关 9 条 |
| 七、共享补丁 | 4 个最小补丁 + 调用点 + 依赖 + 针对性测试（未落地时 `skip` 并写明补丁编号） | `AI_1_SHARED_PATCHES.md`、`test_data_shared_patches.py`（5 过 / 4 待补丁） |
| 八、A4 端口注入 | **未做**（本轮按要求先做 A2） | — |

本轮验证（我的范围，不含并行角色的 UI/验收文件）：
`147 passed, 4 skipped`（`artifacts/model-multimaterial-ai1-20261027/final-verification.txt`），含真实工程里 `multimaterial` 段经改名/封面刷新/删除后仍在的往返测试。

AI 4 验收当前状态：`115 passed, 6 failed`（`d1-acceptance-round2.txt`）。6 条失败全在其期望集与其自身负例构建（其中 3 条单独运行能通过 → 共享状态/顺序问题），我未修改其文件，已提交反证与期望变更请求：`AI_1_REFERENCE_CORRECTIONS.md`。

仍未做：A3（原子提交/历史/资源回收实测）、A4（门面/日志）、另存为/目录转换/崩溃恢复的实测证据。


