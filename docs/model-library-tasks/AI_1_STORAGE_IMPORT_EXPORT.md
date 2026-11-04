# AI 1：模型存储、依赖导入和原件导出

日期：2026-10-26。你是实现 AI 1，不是总集成。总集成、公共契约裁决与最终用户验收由当前主对话的 Codex 负责。

## 1. 总体要交付什么

Prism 已经有材质库布局、标签、备注、管理、PBR 贴图控件和渲染。要新增的模型库位于材质库下面：导入 FBX／OBJ／GLB／glTF → 显示模型当前效果封面 → 使用原有槽位调整贴图 → 双击交互查看 → 单个或批量导出原件。

你只负责让模型及其依赖真正属于当前项目，并能完整取回。不要写三维渲染或再建一套标签系统。只支持原件导出；不做编辑后模型导出、GLB 转换、减面或贴图转码。

本任务文件已经包含你的实施与汇报要求，不需要另外找一个“集成 AI”。

## 2. 现有代码到底完成了什么

源码根目录：C:/Users/admin/Desktop/Prism-main/Prism-main。

已经存在：

- 左侧模型库入口、中间空布局、右侧贴图／文件资料／标签备注 UI。
- MaterialCards.set_display_assets：复用原有卡片，接收已准备摘要。
- ModelLibrary.set_service：把项目作用域服务装到现有 UI。
- contracts.py：ModelQuery、ModelPage、ModelCardSummary、ModelRecord、ModelFileRef、ModelAppearance、请求身份与能力定义。
- ports.py：ModelRepository、ModelImportPort、ModelExportPort 等 Protocol。
- service.py：组合入口；默认没有 repository，缺失功能抛 ModelFeatureUnavailable。
- 默认没有导入成功记录、没有 model_records 持久化、没有修改工程格式。
- 模型分区只在 UI_SECTIONS，不在 WorkspaceService.TREE_SECTIONS；不能创建模型文件夹。

不能当作完成的内容：

- ModelRecord 只是 Python 数据类型，不是落盘格式。
- ModelAppearance 中 Mapping 不是深层不可变，不能直接把活字典交给保存线程。
- summary 页面不代表完整记录或完整文件清单。
- 当前 125 项框架回归只证明框架／旧行为，不能证明任何模型持久化或导出。

## 3. 文件所有权

你可以创建：

- prism/model_library/repository.py：项目绑定 repository。
- prism/model_library/project_storage.py：现有存储后端适配、模型域快照与变更确认。
- prism/model_library/importer.py：导入任务与依赖清单。
- prism/model_library/exporter.py：原件导出任务。
- prism/model_library/bundles.py：受租约保护的预览工作目录。
- tests/model_library/test_storage.py、test_import.py、test_export.py、test_bundles.py。
- 本文最后的执行记录。

已有文件只有你可以准备最小改动：

- [scene.py](C:/Users/admin/Desktop/Prism-main/Prism-main/prism/scene.py)
- [fileio/sql.py](C:/Users/admin/Desktop/Prism-main/Prism-main/prism/fileio/sql.py)
- [fileio/schema.py](C:/Users/admin/Desktop/Prism-main/Prism-main/prism/fileio/schema.py)
- [fileio/save_snapshot.py](C:/Users/admin/Desktop/Prism-main/Prism-main/prism/fileio/save_snapshot.py)
- [fileio/save_preparation.py](C:/Users/admin/Desktop/Prism-main/Prism-main/prism/fileio/save_preparation.py)
- [fileio/partial_save.py](C:/Users/admin/Desktop/Prism-main/Prism-main/prism/fileio/partial_save.py)
- [partial_domains.py](C:/Users/admin/Desktop/Prism-main/Prism-main/prism/partial_domains.py)
- [partial_project.py](C:/Users/admin/Desktop/Prism-main/Prism-main/prism/partial_project.py)

这里“可以准备”不代表可以改写整个存储架构。按新增模型域做增量适配。涉及其他已有存储文件，先提出具体文件、原因和变更摘要，由主对话确认所有权，不能自行扩散。

你不得修改：contracts.py、ports.py、service.py、F3D 文件、widgets、__main__.py、window_workspace.py、资源树／标签／全局搜索实现、performance_trace.py、diagnostics.py、公共 conftest。

## 4. 先固定你对外提供的接口

先保留现有 ModelRepository 的 query、get、save 签名。你可以在自己的模块提供下面的适配入口；若确实需要改公共 DTO，提交变更请求，不能自行改公共文件。

~~~text
ModelRepositoryAdapter(project_context)
  query(ModelQuery) -> ModelPage
  get(model_id) -> ModelRecord | None
  save(record, expected_revision=...) -> None

ModelImporter(project_context, repository, publish_event)
  import_paths(tuple[str, ...], folder_id=None) -> job_id
  cancel(job_id) -> None
  close_project(project_session) -> None

ModelOriginalExporter(project_context, repository, publish_event)
  export_originals(tuple[model_id, ...], destination) -> job_id
  cancel(job_id) -> None

ModelBundleStore(project_context)
  prepare(model_id, source_revision) -> PreparedBundle
  release(lease_id) -> None
~~~

project_context 必须绑定现有真实项目会话、打开代次、后端与资源访问者，不是一个全局文件名。报告你用了哪个现有类型／字段。不要凭空造“同一 project_id 就是同一次打开”的规则。

PreparedBundle 需要包含 package_root、main_relative_path、manifest_path、lease_id、source_revision。它不是导出文件，是 F3D 可读取的工作目录；AI 2 通过它读原件。租约存在时不得清理。

导入／导出 job_id 代表已接受任务，不代表已完成。publish_event 回报阶段与结果；先采用你的私有不可变结果 DTO，完整列出字段，交主对话统一接入，不能把额外数据偷偷塞进字符串。

## 5. 必须按这个顺序做

### A1：理解现有保存链，不急着建表

1. 阅读上述保存文件以及 material_service、material_textures、fileio/attachments_store、storage/source_ref 和现有受管理资源通道。
2. 画出当前工程打开、全量保存、部分保存、另存为和附件读取的实际调用链。
3. 列出需要新增的模型域字段、表／映射、快照、覆盖范围和未知字段处理位置。
4. 用一个小模型和一个较大普通文件验证资源读写接口是否能流式处理，记录峰值内存和实际磁盘写入。

交付 A1：字段与调用链、最小存储补丁设计、大文件通道结论。若现有 API 必须整文件进内存，不得直接把全部模型塞进附件 dict。

### A2：先完成真实 repository 和保存重开

1. 用稳定模型 ID 标识记录，与路径和名称分离。
2. 模型元数据、原始文件清单、外观配置、统计／封面缓存身份分别保存。
3. 对常用名称、格式、分类、删除状态、排序字段建立适当查询索引；query 只返回当前页摘要。
4. get 返回完整记录；query 不读模型 BLOB、不读原图、不启动 F3D。
5. save 使用 expected_revision，冲突要返回明确失败，不能覆盖后来的编辑。
6. 全量保存、快照、部分域覆盖与确认都接入模型域。未加载行不是删除指令。
7. 旧工程使用新增结构，保留已有字段／表；遇到未知模型字段要原样保留。

交付 A2：测试项目真正保存重开；保存期间继续编辑仍保留新值；部分打开保存不丢未加载模型；新旧工程已有材质等数据一致。

### A3：定义依赖清单，之后实现导入

每个原件文件记录：file_id、role、包内 relative_path、源来源、大小、SHA-256、受管理 source_ref、缺失状态。不要依赖用户电脑原始绝对路径维持项目可用。

逐格式做：

- OBJ：解析 mtllib；逐个 MTL 解析贴图引用；路径相对 MTL 所在目录，保留空格、大小写及目录层级。不要用空格 split 粗暴拆文件名。
- glTF：区分外部 BIN／图片、内嵌 data URI、缺失项。正确处理 URI 路径，不能自动从互联网下载。
- GLB：保留原字节；检查存在时的外部依赖，不假定每个 GLB 都完全自包含。
- FBX：使用已确认的读取／依赖检测接口或与 AI 2 约定的只读检测结果，不自己凭文件名猜齐贴图。记录内嵌／外置、绝对／相对引用和未确定项。

边界：

- 相同文件名不同目录的贴图分别保留。
- 只收集已确认依赖，不递归收集用户整个硬盘。
- 模型引用目录外文件时，计算可保留关系的包布局；不能允许任意路径逃逸。
- 模型原件复制前后核对来源状态；复制过程中改变就重试或失败，不把不确定版本报成功。
- 先复制／校验到临时阶段，再一次提交完整记录。取消／失败不留下“ready”资产。
- 缺文件可以作为不完整资产入库，但明确列缺失项。
- 重复字节可复用底层资源，不自动把两个用户模型条目合并。

交付 A3：四格式各真实样本；依赖清单与独立期望一致；中断、取消、缺文件与同名冲突有明确结果。

### A4：给 F3D 提供可搬迁的预览包

1. 从项目受管理资源生成包目录，保留内部相对关系。
2. 预览用派生资源和原件分开；不能改写收存原件。
3. 建立租约，F3D 在读时不能被清理。
4. 原始 FBX／MTL 引用绝对路径无法直接搬迁时，原件仍保留字节；为预览提供受控解析／工作副本适配，并报告做法。
5. 临时包键包含 source_revision；外观资源另含 appearance_revision。

交付 A4：AI 2 不访问数据库，仅凭 PreparedBundle 能打开模型；释放后可清理，打开时不丢依赖。

### A5：实现原件单个／批量导出

1. 导出开始固定原件修订和完整 manifest。
2. 每个模型独立目录；内部路径原样保留。
3. 复制原件，不转码、不重写模型、不平铺改名。
4. 先写临时目录，逐文件校验，再发布最终目录。
5. 取消清理本任务临时文件；不得清理用户已有目标或其他任务目录。
6. 报告 completed、failed、cancelled、missing；批量失败不能说全部成功。
7. 原件引用绝对路径时提供路径对照；不能承诺完全不改字节又自动修复所有外部软件的绝对引用。

交付 A5：独立校验器对文件集合、相对路径、大小、SHA-256 全部一致；有磁盘满、权限失败和取消用例。

### A6：交给主对话集成

提供工厂调用样例、项目绑定与关闭方式、结果事件形状、所有修改文件和数据迁移说明。你只把自己的能力标为“本任务验证通过／等待主对话集成”，不能自行使能 UI 或声称全产品完成。

## 6. 可以与谁同步做

- 与 AI 2 并行：A2 repository 与其独立 F3D 实验互不阻塞。
- 与 AI 3 并行：对方使用测试 repository 做展示，生产代码不得用假 repository。
- 与 AI 4 并行：对方建立独立原件期望和故障场景，不把你的导入清单当唯一期望。
- A4 的包接口一旦确定，报告主对话。主对话发布给 AI 2 后再联调。
- F3D 必需能力未验证前，不进行大范围原有项目数据迁移。

你不能为赶进度修改他人文件。接口不够时继续做不依赖接口的任务，并按第 9 节格式报告变更请求。

## 7. 验收条件

必须通过：

- 四种格式的文件清单、嵌入／外置资源、同名贴图、中文路径、缺文件。
- 保存重开、另存为、项目搬迁、删除恢复。
- 保存 A 修订期间产生 B 修订，A 完成不能把 B 当成已保存。
- 部分加载保存不删除未载入模型或未知字段。
- 只改备注／标签／外观配置，不重写所有模型大文件。
- 原件导出逐文件一致，批量状态诚实。
- 不需要 F3D 安装也能执行 repository 和原件导出测试。
- 没有数据库连接跨线程随意共用，没有 Qt 控件进入 IO 服务。

检查：

~~~text
.venv/Scripts/python.exe -m pytest -q tests/model_library/test_storage.py tests/model_library/test_import.py tests/model_library/test_export.py tests/model_library/test_bundles.py
.venv/Scripts/python.exe -m pytest -q tests/test_partial_domains.py tests/test_workspace_migration.py tests/test_model_library_scaffold.py
~~~

文件名可按实际测试创建，但必须在报告给出实际命令和结果；不能用“建议运行”替代真实运行。性能数据标明机器、规模、文件总量、缓存和样本数。

## 8. 给主对话的最终汇报模板

把下面内容作为最终回复。用户会把你的汇报带回当前集成对话；未明确授权不要自行向别的聊天发送消息。代码要在约定工作目录可检查；隔离副本交 patch／差异和路径，不用整个项目覆盖主目录。

~~~text
角色：AI 1
状态：A1…A6 各自为 通过／失败／未测／受阻
工作目录或提交／补丁位置：
本次修改与新增文件：
接口：工厂、构造参数、方法、事件字段、关闭方式、最短调用例子
已实现：只列有证据的能力
尚未实现／限制：
数据结构与迁移：新增内容、旧工程兼容、部分保存、未知字段、回退
测试：实际命令、通过／失败数、耗时、失败原文或文件
独立证据：样本清单、原件哈希、导出核对、保存重开结果路径
资源／写入放大：测量条件和原始结果路径，没测就写未测
需要集成者修改：具体文件、位置、调用方法
是否可接入：可读摘要／可导入／可导出分别回答
下一步建议：
~~~

## 9. 中途变更请求模板

~~~text
请求编号：A-RFC-01
现有接口／字段：
实际阻塞：
建议最小变更：
涉及文件与消费者：
不改契约的替代方案：
我可继续完成的不依赖工作：
~~~

## 10. 执行记录（由 AI 1 填写，不能改写上面的任务与标准）

- 当前：A1–A5 本任务验证通过，A6 等待主对话集成（未使能任何 UI 能力、未修改公共契约）。
  详细证据：`artifacts/model-library-ai1-20261026/REPORT.md`、`measurements.json`、`evidence.json`。
- A1：通过。调用链已画清：打开（`SQLiteIO.read`）→ 全量保存（`SQLiteIO.write_board_extras`）→
  部分保存（`partial_project` + `partial_domains` + `fileio/partial_save`）→ 另存为（同一写入器换目标）→
  附件读取（行内 incremental blob / 目录工程对象库 `managed.open_original`）。
  模型域字段：`model_records(id, payload)` + 表达式索引；原件沿用既有 `attachments` 通道，不新建字节仓。
  流式结论：64 MiB 文件写入峰值 2.0 MiB、放大 1.001；读回峰值 2.0 MiB；见 `measurements.json`。
- A2：通过。稳定 `model_id` 与路径/名称解耦；元数据、文件清单、外观、统计、封面缓存身份分开存放；
  `query` 只读摘要并分页，不读 blob、不读原图、不启动 F3D；`get` 返回完整记录；
  `save` 用 `expected_revision`，冲突抛 `ModelRevisionConflict`；全量保存/快照/部分域/未知字段全部接入；
  `retire`/`restore` 为元数据删除恢复，原件不动。
  `tests/model_library/test_storage.py` 24 项通过（保存重开、另存为、工程搬迁、删除恢复、
  保存期间另行编辑、部分加载不删未载入模型、只改备注不重写大文件、旧材质记录不变、64 MiB 流式）。
- A3：通过。四格式真实样本各一：OBJ（含 MTL 与其贴图、缺失贴图如实列出）、glTF（外部 BIN + 图片、
  data URI 不算文件、不联网下载）、GLB（保留原字节；带外部 buffer 的 GLB 不假定自包含）、
  FBX（7.x 容器游标读取 Texture/Video/Media 声明，内嵌与外置、绝对与相对都记录，不靠文件名猜）。
  中文与空格路径、同名贴图分目录、目录外依赖的包布局（禁止 `..` 与绝对路径入库）、
  复制前后源文件变化即失败、取消不留 ready 资产、重复导入不合并条目，均有测试。
  `tests/model_library/test_import.py` 23 项通过。
- A4：通过。`ModelBundleStore.prepare` 生成可搬迁工作目录（原件副本 + `manifest.json`），
  租约未释放时不可清理，`release`/`release_all` 后目录删除；键含 `source_revision`，
  外观派生包另含 `appearance_revision`；绝对引用在 `_preview/` 生成派生工作副本，原件字节不改。
  `tests/model_library/test_bundles.py` 11 项通过。接口形状已按第 6 节报告主对话，等发布给 AI 2 联调。
- A5：通过。导出开始即固定修订与完整清单；每个模型独立目录、内部路径原样保留；先写临时目录逐文件校验
  （大小 + 源侧 SHA-256 对账）再 `rename` 发布；取消只清本任务临时目录；批量失败/缺失/取消逐条如实报告；
  磁盘满（ENOSPC）、权限失败、目标已存在、原件已不在工程内均有失败用例；
  绝对引用给出 `_prism_references/<model-id>.json` 对照。`tests/model_library/test_export.py` 14 项通过。
- A6：等待主对话集成。工厂与最短调用样例、事件 DTO 字段、关闭方式写在
  `artifacts/model-library-ai1-20261026/REPORT.md` 第 4 节；`service.py`、`ports.py`、`contracts.py`
  与所有 widgets 由我保持未改。
- 交付证据与待集成事项：
  1. 本任务实际命令与结果：
     `pytest -q tests/model_library/test_storage.py tests/model_library/test_import.py
     tests/model_library/test_export.py tests/model_library/test_bundles.py` → 72 passed / 24.15 s；
     `pytest -q tests/test_partial_domains.py tests/test_workspace_migration.py
     tests/test_model_library_scaffold.py` → 13 passed / 5 failed（失败全部来自 A-RFC-01 与 A-RFC-02，
     即测试夹具缺 `PRAGMA user_version` 与 AI 2 的 `F3DBackend` 构造签名，均非本域代码）；
     另 `pytest -q tests/test_partial_project.py tests/test_partial_project_gui.py tests/test_partial_save.py
     tests/test_material_cards.py tests/test_storage_migration_roundtrip.py tests/test_storage_integration_safety.py`
     → 71 passed / 39.86 s。
  2. 独立证据：`verify_evidence.py` 对四格式各自“导入 → 导出”，用磁盘上的源文件重新算哈希与导出目录对账，
     全部一致；同一工程保存重开后 `same_model_ids / same_file_manifests / same_attachment_ids` 均为 true。
  3. 迁移：只新增 `model_records` 表（`(id, payload)` + 表达式索引），不动既有表；老工程读到时按需建表；
     带生成列的早期实验形状会按行重建；未知字段原样保留；目录工程转换已验证可用。
  4. 待集成者修改：`service.py` 注入真实实现；导入完成回调在 GUI 线程更新 `scene.model_records` 并调用
     `register_model_attachments`；工程关闭时 `close_project` + `release_all`；`publish_event` 接现有
     performance/diagnostics 管道（详见 REPORT.md 第 4 节）。
