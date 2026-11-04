# 模型库日志与诊断：实现说明（L0）

状态：**L0 完成（现状核查 + 契约冻结 + 一处阻断性最小修补）**。
L1—L4 未完成，逐项列在文末，未测项一律标明，不当作已交付。

- 改动日期：2026-10-27
- 运行环境：`.venv\Scripts\python.exe`（Python 3.12.8 / PyQt6 6.7.1 / VTK 9.7.1）
- 原始证据：`artifacts/model-library-logging/l0/`

---

## 1. 现有真实调用链

```text
业务调用点（widgets/model_library.py, model_library/composition.py,
            model_library/service.py, multimaterial/application.py）
        │
        ├─ record_model_event(name, **fields)      ← events.py 唯一模型域入口
        │        │  _prepare(): 脱敏 + 身份填充 + schema 校验
        │        ▼
        │   prism.performance_trace.record('model_'+name, priority=…, **payload)
        │        │  双队列: events(384) / important(128)，按优先级入队
        │        ▼
        │   PrismTraceWriter 线程（daemon）→ Prism-operations-<pid>.jsonl（轮转）
        │        └─ 饱和时丢弃 sample 级事件并计数（dropped / dropped_by_kind）
        │
        └─ service._publish(dict(kind=…, job_id=…, state=…))
                 ▼
            composition._event(key, data) → record_model_event('service_job', **data)

原生进程（prism/components/model_worker.py，独立进程）
        │  native_protocol 帧
        ▼
   native_jobs.NativeModelJobController._emit(event, **fields)
        │  publish_event 回调（两个不同签名，见 §3 缺陷 D2）
        ├─ 多材质：lambda event, **fields: service.multimaterial.native_event(...)
        └─ service：lambda data: composition._event(key, data)   ← 单参

界面
   widgets/model_library.py  ── publish_job_event → 卡片/详情刷新
   widgets/diagnostics_dialog.py ── collect_bundle(...) → 诊断包 zip
```

关键事实（都能在源码里定位）：

| 事实 | 位置 |
| --- | --- |
| 模型域唯一事件入口，含脱敏/身份/schema | `prism/model_library/events.py:249` `record_model_event` |
| 日志写入是**异步**的，双队列 + 优先级 + 丢弃计数 | `prism/performance_trace.py:2038-2046`、`:2123` writer 线程、`:2307` `_note_drop` |
| 事件名与队列分类是两个字段 | 落盘记录里 `event='model_…'`、`kind` 是 trace 分类（实测 `(has_event, has_kind)` = 30431 / 120） |
| job 生命周期统一成一条 `service_job` | `prism/model_library/composition.py:220` |
| 多材质走分层命名 `material.<域>.<阶段>` | `prism/model_library/events.py` `REQUIRED_FIELDS` 的 `material.*` 族 |
| 诊断包入口只有一个 | `prism/widgets/diagnostics_dialog.py:89` `export_bundle` |
| 诊断包已支持模型快照位 | `prism/diagnostics.py:153` `collect_bundle(..., model_snapshot=)` |

---

## 2. 事件与身份契约（冻结基线）

### 2.1 身份字段（`events.IDENTITY_FIELDS`，15 个）

`project_session, open_epoch, model_id, request_id, job_id, source_revision,
appearance_revision, camera_revision, backend_version, material_id, binding_id,
catalog_revision, state_revision, draft_generation, gesture_id`

`model_event_contract()['legacy_identity_fields']` 保留前 9 个，作为旧调用点的兼容集合。

### 2.2 实测填充率（553 条真实 `model_*` 事件）

| 字段 | 填充率 | 说明 |
| --- | --- | --- |
| `thread_id` | 100% | 采集层自动加 |
| `open_epoch` | 86.8% | |
| `model_id` | 75.9% | |
| `project_session` | 74.1% | |
| `source_revision` / `catalog_revision` / `request_id` | 60.2% | 主要来自 `material.*` |
| `state_revision` | 57.1% | |
| `origin_action_id` / `trace_id` | 26.8% | 只有部分链路接入 trace 上下文 |
| `job_id` | 20.6% | |
| `gesture_id` | 19.0% | |
| `draft_generation` | 6.0% | |
| `parent_span_id` | **0%** | 因果父子链未建立 |
| `working_material_id` / `source_material_id` / `binding_id` | **0%** | 材质身份只到 `material_id`，不到使用点 |
| `worker_instance_id` / `process_id` | **0%** | 跨进程身份缺失（`pid` 只在报文顶层） |

原始数据：`artifacts/model-library-logging/l0/identity-fill.json`。

### 2.3 状态语义（必须区分的那些）

现有实现已经**分开表达**的部分：`state` 取值 `queued/completed/failed/stale`
（`service._publish`）、`material.*` 各自的阶段事件、`EVENT_INSTALLED` 与
`EVENT_OUTPUT_WRITTEN` 是两个事件、`model_commit_confirmed` 与
`material.commit.completed` 分开。

仍然**混在一起**的部分（本轮未改，见 §5）：
"图片回传成功"与"界面已安装"没有独立事件；"窗口显示请求"与"窗口可见/前台"
没有事件；"数据库提交成功"与"工程场景已同步"共用 `model_commit_confirmed`。

---

## 3. 已证实缺陷（本轮实测，非推测）

### D1（P1，已本轮修复）诊断包从不包含模型库诊断快照

- **证据**：`l0/bundle-probe.json`。同一份输入跑两次：
  - A（当前 UI 行为，不传 `model_snapshot`）：24 条目，**无** `model-diagnostic.json`，
    `manifest.omissions = [{name: model-diagnostic.json, reason: snapshot_not_supplied}]`，
    `summary.model_library.state = not_supplied`；
  - B（同样的调用 + 传入 `model_diagnostic_snapshot()`）：25 条目，含
    `model-diagnostic.json`，`summary.model_library.state = available`。
- **根因**：`diagnostics_dialog.export_bundle` 未传该参数，且全仓库**没有**
  `model_diagnostic_snapshot()` 的调用点（`Get-ChildItem ... | Select-String` 仅命中定义处）。
- **影响**：诊断包缺少任务书第九节要求的"活动任务/未完成任务、队列、缓存、租约、
  日志完整性"整块内容，只能看到 omissions 里的一句原因。

### D2（P2，未修复）原生阶段事件被两套回调签名夹在中间

- `native_jobs.NativeModelJobController._emit(name, **fields)` 用
  `callback(event, **fields)` 调用（`native_jobs.py:1039-1048`）。
- 多材质控制器接线正确：`lambda event, **fields: service.multimaterial.native_event(...)`
  （`composition.py:187`）。
- 但 service 侧接线是单参：`publish_event=lambda data: self._event(key, data)`
  （`composition.py:176`）。若该回调被以 `(name, **fields)` 形式调用，会
  `TypeError`，而 `_emit` 用 `except Exception: pass` 吞掉。
- **当前后果**：`model_native_worker_started` / `model_native_log` 在真实日志里
  只有 `project_session`，没有 `job_id` / `model_id`，无法与具体任务关联
  （`identity-fill.json` 中这两类事件的 identity 只有 1 个字段）。
- **未证实部分**：无法从现有日志区分"回调签名不匹配导致丢事件"与"事件本来就没带
  这些字段"。要证实需要一次带断点的真实原生任务，本轮**未做**（列为待验证）。

### D3（P2，未修复）`service_job` 把阶段压平成一个事件名

- 真实日志 66 条 `model_service_job`，字段是 `kind`（任务类别：import /
  export_originals / thumbnail / preview / texture / capabilities）+ `state`
  （queued/completed/failed/stale）。
- `events.REQUIRED_FIELDS` 为 `queued/started/native_loaded/rendered/
  output_written/installed/completed/failed/cancelled/stale` 定义了必需字段，
  但这些事件名**从未被使用**——schema 校验对这些记录永远不会触发。
- **后果**：①不能按事件名统计阶段；②`failed` 的必需字段 `error_code` 事实上没有
  （只有 `error_message`）；③"读文件/哈希/解析/事务提交"等中间阶段没有事件位。

### D4（P2，未修复）多目标操作用逗号拼接的 `material_id` 代替集合摘要

- 实测样本：`material_id = "w-7b34b240…,w-0e2267cf…,w-09507af1…"`，
  并被 `MAX_STRING` 截断（末尾 `…`）。
- 违反任务书四.5："多目标操作记录数量、集合摘要及必要的有界明细"。
- 现在既没有 `count`，也没有稳定的集合摘要（哈希），截断后连成员都不可知。

### D5（P3，未修复）缩略图/贴图类任务不区分模型封面、材质球、贴图槽

- `service._publish(dict(kind='thumbnail'|'preview'|'texture', …))` 只到类别。
- 任务书三要求"模型封面 / 材质球缩略图 / 贴图槽缩略图"分开，第六节第 1、2 条
  故障（一直"待生成"、全部"不可用"）正是靠这个区分来定位。

---

## 4. 本轮唯一改动（阻断性最小修补，可回退）

**文件**：`prism/widgets/diagnostics_dialog.py`（+40 行，其余未动）

1. `_model_library_service(window)`：从 `window.model_library.service` 取服务句柄，
   取不到返回 `None`（不 import 模型库模块，不影响无模型库的窗口）。
2. `_model_snapshot(service, problem)`：在**导出线程**里调用
   `model_diagnostic_snapshot(service=service)`，成功/失败都写进
   `problem['model_snapshot']`，失败返回 `None`。
3. `export_bundle` 改为先取句柄、再在后台任务里构建快照并传给
   `collect_bundle(..., model_snapshot=snapshot)`。

**验证**（`l0/repair-verification.json`、`l0/repair-negative.json`）：

| 场景 | 结果 |
| --- | --- |
| 有 model service | 快照写入 `model-diagnostic.json`，`service_supplied=true`，omissions 为空 |
| 无 window / 无 model_library | 仍生成快照（能力标 `not_supplied`），omissions 为空 |
| 快照构建抛异常 | 诊断包**照常导出**，`problem.model_snapshot={state: failed, reason: RuntimeError}`，omissions 记 `snapshot_not_supplied` |

**回退**：`artifacts/model-library-logging/l0/diagnostics_dialog.py.before` 是改动前原件，
拷回即回退；该文件无其它改动依赖。

**未做**：没有把 `queue` / `resources` / `job` 句柄接进快照（当前没有稳定、
与本模块解耦的句柄），因此快照里这几项仍显示 `not_supplied` / `not_measured`。
这是**如实标注**，不是"已采集"。

---

## 5. 覆盖矩阵（任务书第三节 15 个领域）

判定口径：**已覆盖**＝真实运行日志里有对应事件且身份可用；**部分**＝有事件但缺
阶段/身份/区分度；**缺失**＝没有事件位。数据来源：`l0/event-inventory.json`、
`l0/identity-fill.json`（本机 30,461 行、553 条模型事件）与源码调用点。

| 领域 | 现状 | 依据 | 缺口 |
| --- | --- | --- | --- |
| 进入与浏览（打开/切目录/分页/搜索/排序/空结果/查询失败） | 部分 | `model_library.py` 的 `scope_bound`、`query_failed`、`query_discarded`、`page_discarded`；真实日志 5 条 `model_scope_bound` | 空结果、排序无事件；事件名是自由字符串，不在契约里 |
| 文件夹与整理（建/改名/移动/拖放/跨目录/跨工程） | 部分 | `composition.py:54` `assets_moved` / `assets_move_failed` | 无逐项结果（只 count）；跨工程传输无事件 |
| 模型元数据（重命名/标签/备注/批量/冲突拒绝） | 部分 | `model_library.py:1033` `model_renamed`、`:1048` `models_deleted` | 标签/备注/批量无事件；冲突拒绝只有 `composition_failed` |
| 导入（接收/格式/依赖/复制/哈希/解析/提交/取消/部分失败） | 部分 | `service._publish(kind='import', state=queued/completed/failed)`、`import.dependencies_detected` 等契约已定义 | 契约事件**无调用点**；真实的 import 只有 4 个 state，没有依赖/哈希/复制/解析分段 |
| 材质识别（定义/工作材质/使用点/未指定/悬空/未使用/缺图/路径冲突） | 部分 | `material.parse.queued`（18 条真实） | 计数明细没有进日志（只在 catalogue 内存里）；未指定/悬空/缺图没有事件字段 |
| 模型封面 | 部分 | `service._publish(kind='thumbnail')` | 与材质球/贴图槽**不区分**（D5）；缓存命中/校验/界面接受无事件 |
| 材质球缩略图 | 部分 | 同上 `kind='preview'` | 同上 |
| 贴图槽缩略图 | 部分 | 同上 `kind='texture'` | 没有通道提取/解码/缓存命中分段 |
| 材质库选择器（打开/查询/可见图片/缓存/原件/回传/选择/关闭） | 缺失 | 无对应 `record_model_event` 调用点 | 全部缺 |
| 多材质编辑（选择/数值/贴图/通道/开关/复制粘贴/库替换/独立/恢复） | 部分 | `material.parse.queued`、`material.commit.*`、`material.install.*`（真实 300+ 条），身份最完整 | 选择/数值/贴图/开关等**编辑动作**没有事件；只有提交与安装 |
| 交互预览（启动/加载/映射/首帧/窗口显示/通道/LOD/拾取/高亮/关闭） | 部分 | `native_loaded`、`installed`、`rendered` 在契约里；真实原生事件只有 `worker_started`/`native_log`，且缺 `job_id`（D2） | 首帧、窗口可见/前台、拾取、高亮、LOD 全缺 |
| 原生进程（启动参数/握手/版本/任务/超时/崩溃/退出码/重启/关闭） | 部分 | `EVENT_WORKER_STARTED` / `WORKER_LOST` / `exit_code` / `log_tail` 已实现；`model_native_worker_started` 38 条、`model_native_log` 34 条 | 缺 `worker_instance_id` / `process_id` / `backend_version`；无重启计数 |
| 持久化（接受/资源准备/事务/提交回滚/回执/重开/版本冲突） | 部分 | `model_commit_confirmed`（25 条）、`material.commit.completed`（15 条） | 事务开始/回滚、保存重开、版本冲突无独立事件 |
| 删除与恢复（删除/回收站/恢复/引用变化） | 部分 | `models_deleted`（count） | 回收站、恢复、引用变化无事件 |
| 原件导出（单个/批量/清单/依赖/写出/哈希/发布/失败/取消） | 部分 | `service._publish(kind='export_originals')`、`export.*` 契约事件存在但**无调用点** | 清单冻结、哈希核验、发布无事件 |
| 生命周期（切模型/切工程/另存/退出/取消/晚到/释放） | 部分 | `scope_bound`、`query_discarded(reason=scope/revision)`、`stale` | 切工程、另存、退出、资源释放无事件 |

统计：**已覆盖 0 / 部分 14 / 缺失 1**（按"真实落盘 + 身份可用 + 阶段可分辨"的
严格口径；"部分"里含若干只有契约没有调用点的项）。

---

## 6. 日志可靠性现状（任务书第八节逐条）

| 要求 | 现状 | 位置 |
| --- | --- | --- |
| 异步、有界写，GUI 不做磁盘写 | **已满足** | `performance_trace.py:2038/2123` 双队列 + daemon writer |
| 队列满按优先级，优先故障/提交/终态 | **已满足** | `PRIORITY_ERROR/TERMINAL/STATE/SAMPLE`，`_enqueue` 分流 |
| 记录丢失数量、原因 | **已满足** | `dropped` / `dropped_by_kind` / `trace_stats()['seq_gap_basis']` |
| 重复原生日志合并并保留首末次数 | **已满足** | `NATIVE_LOG_WINDOW_S` / `_emit_native_merged` / `model_event_state` |
| 日志故障不得让业务失败 | **已满足** | `record_model_event` 全包 try + `_note_failure`；`_emit` 同样吞异常 |
| 磁盘满/权限/关闭超时可诊断 | **部分** | `PrismRotatingFileHandler.doRollover` 吞 `OSError` 只做截断，**不计数也不上报** |
| 刷新/轮转/容量/保留策略明确 | **部分** | 轮转 4MiB×2、保留 3 份；`RETAINED_LOG_FILES`；退出刷新未见于本模块 |
| 退出有有限等待并记录未完成刷新数 | **未验证** | 需真实退出路径取证（本轮未做） |
| 原生 stdout/stderr 有界保留进诊断包 | **部分** | `log_tail` + `logs/Prism-native-crash-*.log` 已进包（实测 3 个） |
| 崩溃后残缺日志仍可读 | **已满足** | 逐行 JSON 解析；实测 30,461 行 0 坏行、0 截断尾 |

---

## 7. 跨线程 / 跨进程身份传递（现状图）

```text
GUI 线程                                 后台线程                    原生进程
────────                                 ────────                    ────────
origin_action_id (trace 层)  ──contextvar──►  同一 contextvar  ──序列化──►  无
project_session / open_epoch ──显式参数────►  显式参数         ──显式报文──►  ✅ 有
model_id                     ──显式参数────►  显式参数         ──显式报文──►  ✅ 有
job_id                       ──显式参数────►  显式参数         ──显式报文──►  ✅ 有
request_id                   ──显式参数────►  显式参数         ──显式报文──►  ❌
draft_generation             ──显式参数────►  显式参数         ──显式报文──►  ❌
worker_instance_id           ──❌──────────►   ❌              ──❌────────►  ❌
process_id                   ──✅(pid)─────►   ✅(pid)         ──❌────────►  ❌
parent_span_id               ──❌──────────►   ❌              ──❌────────►  ❌
```

- 跨线程：`performance_trace` 用 `contextvars` 传 trace 上下文（`capture_context` /
  `restore_context`），**线程池不会自动继承**，现有代码在提交任务处显式捕获/恢复。
- 跨进程：身份走 `native_protocol` 帧；对不属于当前工程会话的回传，`native_jobs`
  已有 `ERR_STALE` 拒绝路径（`_emit(EVENT_STALE)`）。
- 缺：`worker_instance_id` 从头到尾没有，重启后无法区分"同一个 worker"与"新 worker"。

---

## 8. 未完成清单（L1—L4）

| 阶段 | 状态 | 说明 |
| --- | --- | --- |
| L0 基线与契约 | **完成** | 本文档 + `l0/` 证据 + 一处最小修补 |
| L1 打通最小真实链（选材质→草稿→原生→回传→界面→应用→提交→回执） | **未完成** | 多材质链已有 `material.parse/commit/install` 真实落盘（300+ 条），但"界面接受/拒绝"与"回执→界面"之间仍无事件；本轮未做 |
| L2 覆盖完整模型库（15 领域） | **未完成** | 见 §5：0 已覆盖 / 14 部分 / 1 缺失 |
| L3 诊断包与日志可靠性 | **部分完成** | 快照已进包（§4）；轮转失败计数、退出刷新、进程日志归集策略未做 |
| L4 性能对照与独立验收 | **未开始** | 见验收文档"性能"一节：未测 |

---

## 9. 可复现命令

```powershell
$env:QT_QPA_PLATFORM='offscreen'

# L0 证据：诊断包在有/无快照时的差异
.venv/Scripts/python.exe artifacts/model-library-logging/l0/probe_bundle.py     # 见 §10 说明
# L0 证据：真实日志的事件与身份填充（只读，不复制原文）
.venv/Scripts/python.exe artifacts/model-library-logging/l0/probe_identity.py

# 修补验证（正/负分支）
.venv/Scripts/python.exe artifacts/model-library-logging/l0/verify_repair.py
.venv/Scripts/python.exe artifacts/model-library-logging/l0/verify_negative.py

# 未破坏既有诊断/日志契约
.venv/Scripts/python.exe -m pytest -q tests/test_diagnostic_bundle.py `
  tests/test_diagnostic_safe_metadata.py tests/test_performance_trace.py `
  tests/model_library/test_multimaterial_event_integration.py `
  tests/model_library/acceptance/test_logging_contract.py `
  tests/model_library/acceptance/test_log_fault_injection.py
# 结果：53 passed
```

## 10. 证据清单

| 文件 | 内容 |
| --- | --- |
| `l0/bundle-probe.json` | 诊断包 A（现状）/ B（传快照）对照，含 omissions 与 summary |
| `l0/bundle-a-current.zip` | 修复前行为产出的真实诊断包（无 `model-diagnostic.json`） |
| `l0/bundle-b-with-snapshot.zip` | 传入快照后的真实诊断包 |
| `l0/bundle-with-service.zip` / `bundle-without-window.zip` | 修补后的两种真实导出 |
| `l0/bundle-snapshot-raises.zip` | 快照构建失败时的真实导出（仍成功） |
| `l0/repair-verification.json` / `l0/repair-negative.json` | 修补正/负分支结果 |
| `l0/event-inventory.json` | 30,461 行真实日志的事件名/字段统计（无原文） |
| `l0/identity-fill.json` | 553 条模型事件的身份填充率与逐事件字段 |
| `l0/diagnostics_dialog.py.before` | 修补前原件（回退用） |

> 说明：`probe_bundle.py`、`probe_identity.py`、`verify_repair.py`、`verify_negative.py`
> 是本次取证脚本，随证据一起归档到 `l0/`，便于他人复跑；它们只读日志、只写 `l0/`。

---

# L1 / L2 实现记录（第二轮）

范围：按"两个都做"的指令，同时推进

- **L1**：把一次真实写入拆成"已持久化"与"场景已同步"两个事实；
- **L2**：给图片任务贴主体、给多材质操作加集合摘要，并让新事件真正进入契约。

判定依据仍是真实落盘：`artifacts/model-library-logging/l1/l1-probe.json`
（临时工程库 + 真实 `ModelLibraryService` + 真实 trace writer + 真实 JSONL）。

## 1. 本轮改动（5 个文件）

| 文件 | 改动 | 作用 |
| --- | --- | --- |
| `prism/model_library/events.py` | 新增 5 个事件常量 + 对应 `REQUIRED_FIELDS` | 让新事件被 schema 校验，而不是"名字随便写、校验永远不触发" |
| `prism/model_library/service.py` | `_commit_payload` 与 `patch_metadata_batch` 各加 durable/applied 两条事件；`_install_cover` 加"缓存已写"事件 | 分开"数据库已提交"与"场景已同步"；分开"图片落盘"与"界面已显示" |
| `prism/model_library/composition.py` | `_event` 注入 `subject`；`_commit` 补 `project.model_applied`（保留 `model_commit_confirmed`） | 图片主体可区分；场景同步有独立事件 |
| `prism/model_library/multimaterial/application.py` | `material.install.completed` 增加 `material_count` / `material_set_digest` / `..._truncated` / `..._sample` | 多材质操作不再被截断成一串逗号 id |
| `prism/widgets/model_library.py` | `publish_job_event` 在 completed 时记 `ui.image.accepted` | "回传成功"与"界面已装上"分开 |

备份（回退用）：`artifacts/model-library-logging/l1/*.py.before`；
改动后的副本：`artifacts/model-library-logging/l1/patched/`。

## 2. 事件契约变更

| 事件（落盘名） | 必需字段 | 说明 |
| --- | --- | --- |
| `model_commit_durable` | `model_count`, `state` | 记录已写入工程库。单条路径带 `model_id`，批量路径带 `model_ids[≤8]` + `ids_truncated` |
| `model_commit_apply_failed` | `model_count`, `durable`, `error_code` | 已持久化但场景同步失败；`readonly_after` 说明是否进入只读 |
| `model_project.model_applied` | `model_count`, `durable`, `state` | 场景副本已替换（这才是"用户看得见"的那一步） |
| `model_thumbnail.cache_written` | `model_id`, `subject`, `cache_key` | 封面字节已落盘 |
| `model_ui.image.accepted` | `job_id`, `subject`, `state` | 卡片已接受该图片 |

兼容性：`model_commit_confirmed` **保留不变**（现有查询与测试仍可用），
新增事件与其并存。旧调用点无需改动。

## 3. 本轮最重要的发现：写入有两条路径

第一次埋点只加在 `_commit_payload`，探针跑真实的 `service.rename(...)` 之后
**一个事件都没落**（`l0` 的 18 条事件里只有 trace 自身）。原因是：

```text
_commit_payload        ← update_metadata / update_appearance / _install_cover
patch_metadata_batch   ← rename / 标签 / 备注 / 批量元数据   （自己写库，不经 _commit_payload）
```

只按"提交"这个词去找入口，会漏掉重命名与批量编辑这条最常用的路径。
两条路径都补上之后，探针的结果才与预期一致（见 §4）。

## 4. 真实路径落盘证据

`l1/l1-probe.json`（临时工程 SQLite + 真实 service + 真实 trace writer）：

| 场景 | 落盘事件序列 | 关键字段 |
| --- | --- | --- |
| 正常 | `model_commit_durable` → `model_project.model_applied` | `durable=true`、`state=completed` |
| 场景同步失败 | `model_commit_durable` → `model_commit_apply_failed` | `durable=true`、`error_code=RuntimeError`、`readonly_after=true` |

这正是任务书第五节要求的区分：**数据库提交成功 ≠ 工程场景已同步**。
`dropped_total=0`、`writer_errors=0`、`trace_incomplete=false`，说明这些数字不是
"丢了之后猜出来的"。

## 5. 主体与集合摘要（`l1/subject-and-scope.json`）

- 主体映射：`thumbnail→model_cover`、`preview→interactive_preview`、
  `texture→texture_slot`；`import`/`export_originals` 不贴主体；显式 `subject` 优先。
- 集合摘要：3 个材质 → `material_count=3`、摘要 16 位、`truncated=false`；
  1000 个材质 → 摘要长度不变、`truncated=true`、明细固定 8 条。
  摘要对输入顺序稳定（排序后计算）。

**未完成的部分（如实记录）**：

- 缩略图"四态"（正常/未加载/缺失/损坏）只完成了**主体区分**，
  三态仍无法从日志分辨——需要缓存命中与解码层的埋点；
- `binding_id` **仍未贯通**（真实日志里该字段填充率 0%）。本轮只把
  `material_id` 从"截断的逗号串"改成"计数 + 摘要 + 有界明细"，
  使用点级身份还需要 catalogue → 提交 → 日志的传递链。

## 6. 覆盖矩阵的变化（相对 L0）

| 领域 | L0 | 现在 | 变化原因 |
| --- | --- | --- | --- |
| 持久化 | 部分（提交/同步共用一条） | **部分（已拆开）** | durable / applied / apply_failed 三事件；回滚与版本冲突仍缺 |
| 模型封面 | 部分（与材质球/贴图不分） | **部分（主体已分）** | `subject` 注入；四态仍缺 |
| 材质识别 / 多材质编辑 | 部分（`material_id` 被截断） | **部分（范围可核对）** | 计数 + 摘要 + 有界明细；使用点级身份仍缺 |
| 导入 | 部分 | 部分 | 未变（仍无依赖/哈希/复制分段） |

## 7. 既有问题（本轮复现，非本轮引入）

- **7 个测试套件组合运行时，进程在 106 个测试全绿后以
  `0xC0000005`（ACCESS_VIOLATION）退出**：

  ```powershell
  .venv/Scripts/python.exe -m pytest tests/test_ai09_logging_contract_20261024.py `
    tests/test_ai09_round2_logging_contract_20261024.py tests/test_diagnostic_bundle.py `
    tests/model_library/acceptance/test_logging_contract.py `
    tests/model_library/test_multimaterial_event_integration.py `
    tests/model_library/test_ui.py tests/model_library/test_management.py
  ```

  A/B 对照（`l1/ab7-before.txt` / `l1/ab7-after.txt`）：**改动前后都崩**；
  去掉两个 `test_ai09_*` 套件后，改动前后都 `EXIT=0`（63 passed）。
  单独跑两个 `test_ai09_*` 也 `EXIT=0`（43 passed）。
- 影响判断：CI 会把这种退出判为失败；若崩溃发生在真实日志 flush 之前，
  最后一段证据可能丢失（**未验证**）。列入待修，本轮未改（不属于日志埋点范围）。

## 8. 回退方法（本轮）

把 `artifacts/model-library-logging/l1/` 下这五个 `.before` 文件拷回原位置：
`events.py.before`、`service.py.before`、`composition.py.before`、
`application.py.before`、`model_library.py.before`。
它们与改动版本一一对应，且改动之间没有交叉依赖（除 `events.py` 的常量被
其余四处 import，必须成套回退）。

---

# L2 收尾 / L3 / L4 实现记录（第三轮）

## 1. L2 收尾（两个缺口各补一半）

| 项 | 做了什么 | 证据 |
| --- | --- | --- |
| 使用点范围 | `material.install.completed` 增加 `binding_count` / `binding_set_digest` / `binding_sets_truncated` / `binding_set_sample`（来自 `state.assignments`） | `tests/model_library/test_logging_pipeline_contract.py::test_scope_summary_is_bounded_and_order_stable` |
| 封面缓存命中 | `thumbnail.cache_written` 增加 `cache_hit`；命中时 `bytes_written=0` | `l3/l3-probe.json`（同一事件两种取值） |
| 图片主体 | 上一轮已加 `subject`（`model_cover` / `interactive_preview` / `texture_slot`） | `test_image_subject_is_named_per_kind` |

**仍未完成（第三次如实记录）**：

- **`binding_id` 逐点事件**没有做。"影响哪些使用点"现在是**集合级**答案
  （数量 + 摘要 + 有界明细），不是逐点事件。要逐点需要在提交时按 binding 记录，
  事件量会与材质数成正比——需要先定"每动作事件上限"策略。
- **缩略图三态**（未加载/缺失/损坏）只完成了"缓存命中 vs 新写"与"主体区分"。
  三态要落在缓存读取与解码失败两处，都在 widget 侧，本轮未动。

## 2. L3 日志可靠性（任务书第八节）

| 要求 | 本轮改动 | 证据 |
| --- | --- | --- |
| 轮转失败可诊断（8.6） | `prism/logging.py` 增加 `_ROTATION` 计数与 `rotation_failures()`；`doRollover` 失败时计数并记录 `last_error`，截断另计 | `l3/l3-probe.json`：`failures 0→1, last_error=PermissionError` |
| 崩溃后残缺日志可读（8.10） | `collect_bundle` 对每个日志文件记录 `read_to_file_end` / `tail_complete`；`summary.log_integrity` 汇总不完整尾部 + 轮转计数 | `l3/bundle-torn-tail.zip`：`101=True, 102=False`，summary 命中 102 |
| 日志故障不得让业务失败（8.5） | `doRollover` 仍然不抛给调用方（验证 `raised_to_caller=False`） | 同上 |
| 退出有限等待（8.8） | **已有**：`trace.close()` 记录 `trace_shutdown_pending(pending_at_close=N)`、`writer_errors_before_close`，join 超时 1 s | 源码 `performance_trace.py:3321-3328`（本轮未改） |
| 队列/丢弃可查（8.2/8.3） | **已有**：`trace_stats()` 的 `dropped_total` / `dropped_by_kind` / `queue_peak` / `seq_gap_basis` | `l4/overhead.json`：突发 10000 事件时 `dropped=48085` |
| 诊断包内暴露轮转 | `diagnostic_snapshot` 的 `pipeline.log_rotation` | `test_snapshot_exposes_rotation_and_keeps_unknowns_unknown` |

## 3. L4 性能对照与独立验收

### 3.1 冻结预算

`artifacts/model-library-logging/l4/BUDGET.md`：LP-1 入队 P95 ≤ 40 µs、
LP-2 字节 ≤ 900 B/事件、LP-3 单次停顿 ≤ 2 ms、LP-4 快照 ≤ 200 µs、
LP-5 突发上限 512 事件且丢包必须计数。**只对日志管道成立，不是用户操作预算。**

### 3.2 实测（交错重复）

稳态（200 事件/轮 × 10 轮，`dropped=0`）：

| 模式 | median | p95 | 日志字节/事件 |
| --- | --- | --- | --- |
| off | 7.05 µs | 7.33 µs | 42.2 B |
| normal | 11.85 µs | 25.24 µs | 557.0 B |
| diagnostic | 11.97 µs | 14.73 µs | 575.0 B |

突发（1000 / 10000 事件/轮 × 5 轮）：

| 规模 | normal median | dropped（5 轮合计） | 说明 |
| --- | --- | --- | --- |
| 1000 | 13.04 µs | 3085 | 每轮 1000 事件，队列只承接约 380 条 |
| 10000 | 12.58 µs | 48085 | 96% 被丢 |

结论：**突发时绝大多数事件被丢**——这是有界队列的预期行为，且计数可查
（`dropped_total`、`dropped_by_kind`），但意味着"高频刷日志"不可用于诊断；
一次用户操作只有几个事件，不受影响。做容量规划要用稳态的 557 B/事件，
不能用突发的 20.9 B/事件（那是丢弃后的分母效应）。

### 3.3 独立验收（可重复的 pytest）

新增 `tests/model_library/test_logging_pipeline_contract.py`（14 项，全绿）：

| 覆盖 | 断言 |
| --- | --- |
| 契约 | 5 个新事件都在 `REQUIRED_FIELDS`、无 `model_model_` 双前缀、载荷字段齐备 |
| 真实写入路径 | 真 service + 真 SQLite + 真 trace：durable → applied 顺序正确；场景失败时是 `apply_failed` 且 `durable=true`，并且库里确实已写入 |
| 主体与范围 | subject 映射固定；摘要顺序稳定、长度不随规模变化 |
| 日志完整性 | 尾部残缺文件在 manifest 与 summary 同时被标出 |
| 轮转 | 失败被计数且不抛给调用方 |
| 快照 | `log_rotation` 存在；没有队列句柄时 `queue.state == 'not_supplied'` 而不是 0 |

### 3.4 未做

- **用户操作级性能**（导入/换材质/导出的端到端 P50/P95/P99、最长 GUI 停顿、
  内存峰值、日志增长曲线）：仍需真实 UI 会话，本轮未做。
- 存储层（SQLite 事务、保存重开）开销：属于 AI 1 的存储实验，本角色未测。
- VRAM：无可靠测量手段，记未知。

## 4. 本轮改动文件（累计）

| 文件 | 轮次 | 作用 |
| --- | --- | --- |
| `prism/widgets/diagnostics_dialog.py` | L0 | 诊断包带上模型快照 |
| `prism/model_library/events.py` | L1/L2 | 5 个事件 + 必需字段 + `cache_hit` |
| `prism/model_library/service.py` | L1/L2 | durable/applied/apply_failed、封面缓存命中、两条写入路径 |
| `prism/model_library/composition.py` | L1 | subject 注入、`project.model_applied` |
| `prism/model_library/multimaterial/application.py` | L1/L2 | 材质集合摘要 + 使用点集合摘要 |
| `prism/widgets/model_library.py` | L1 | `ui.image.accepted` |
| `prism/logging.py` | L3 | 轮转失败计数 |
| `prism/diagnostics.py` | L3 | 日志尾部完整性 + summary 汇总 |
| `prism/model_library/diagnostic_snapshot.py` | L3 | 快照暴露轮转计数 |
| `tests/model_library/test_logging_pipeline_contract.py` | L4 | 独立验收（新增） |

回退：每轮都在 `artifacts/model-library-logging/l{0,1,2,3}/` 留了对应 `.before`。
**注意 `events.py` 被 L1 与 L2 改过两次**，回退时按 `l2/events.py.before` 一次退到 L1 之前。

## 5. 既有问题（本轮再次出现，仍非本轮引入）

7 个套件组合时进程在全部通过后以 `0xC0000005` 退出（改动前后都复现，
见 `l1/ab7-before.txt` / `l1/ab7-after.txt`）。本轮新增的验收文件单独运行
`exit=0`，并入大组合时仍需观察。

---

# L5：用户操作级旅程（尝试记录 + 一个真实缺陷修复）

## 1. 做了什么

目标：拿到"一次真实导入"的端到端耗时与事件链，把 L4 缺的那一层补上。
路径：临时 SQLite 工程库 + 真实 `ModelRepositoryAdapter` / `ModelBundleStore` /
`ModelImporter` / `ModelLibraryService` + 真实 trace writer，对四种格式各导一次。

## 2. 结果：**受阻**，阻断点明确

| 尝试 | 结果 | 证据 |
| --- | --- | --- |
| 自建 `managed_root` 目录后导入 glTF/GLB/OBJ/FBX | 四次全部 `state=failed`，错误 `[Errno 2] No such file or directory: …\managed\.prism-storage.lock` | `l5/import-journey.json`、`l5/` 下四份 trace |

- 根因（**不是**导入逻辑错误）：手工建出来的 `managed_root` 不是合法的 v2 目录工程，
  存储层要求 `.prism-storage.lock` + `project.json` + `metadata.sqlite` 三件套，
  且该目录被识别成"待转换的 v1 工程"（现场留下 `.metadata.sqlite.prism-convert.lock`）。
- 仓库里没有可编程的"新建 managed v2 工程"入口：`prism/storage/managed.initialize`
  只做 v1→v2 转换；`tests/test_b2_readonly_open.py` 的 `_make_project` 建的是画布工程，
  不含模型库所需的 managed v2 结构。
- **解锁条件**：提供一个 `create_managed_project(root)` 之类的公开入口（或让
  `ModelBundleStore`/`ModelLibraryService` 在 managed root 未初始化时给出明确错误码），
  端到端旅程即可直接跑通，无需改本轮的验收脚本。

## 3. 顺带发现并修复：失败事件丢失错误分类（DEF-7）

- **最短序列**：导入失败（任何原因）→ 看 UI 状态与日志。
- **期望**：能看到"哪一类失败 + 是哪个文件出的问题 + 消息"，
  即 `error_code` / `source` / `message` 三者都在。
- **实际**：`service._install_storage_event` 的 `import_failed` 分支只取了
  `failure['message']`，`ModelImportFailure` 里的 `error_code` 与 `source` 被丢掉；
  用户与日志只看到一段带**绝对路径**的 `[Errno 2] No such file or directory: 'C:\…'`。
  `export_failed` 同样丢 `error_code`。
- **修复**：两条分支都保留 `error_code`；导入失败额外保留 `source_name`
  （只用 basename，不让用户路径进 UI 明文；日志侧仍由 `events._sanitize` 脱敏）。
- **验证**：同一段失败现在产出
  `{"state": "failed", "error_code": "unexpected_error", "source_name": "Box.glb",
    "error_message": "[Errno 2] …"}` —— 类型、对象、消息三者可读。
- **未做**：`ModelImportFailure.error_code` 目前只有 `import_failed` /
  `unexpected_error` 两个值，存储未初始化落在 `unexpected_error`。
  更细的分类（`storage_not_initialised` 等）属于存储层，建议 AI 1 补。

## 4. 既有崩溃的结论修正（更正 L1 的"未定位"）

L1 记录里写"7 套件组合退出期 `0xC0000005`、未定位"。本轮在
`tests/conftest.py:146` 找到仓库自己的说明：

> 文档/脑图面板一旦显示就会建 QWebEnginePage。pytest-qt 只把窗口
> `deleteLater()`，不会跑事件循环，于是这些 page 一直攒到会话结束才一起析构
> —— Qt 打印 "Release of profile requested but WebEnginePage still not deleted"
> 并以 0xC0000005 收场（交接单 §3.7）。

所以：**根因是 QWebEnginePage 延迟析构**，仓库已有 autouse fixture
`_settle_deferred_deletes` 缓解，但我在 7 套件组合下仍能复现。
结论从"未定位"更正为"已定位、已有缓解、特定组合下仍复现"。
与本轮改动无关（改动前后都复现）。

## 5. 本轮改动文件

| 文件 | 作用 |
| --- | --- |
| `prism/model_library/service.py` | `import_failed` / `export_failed` 保留 `error_code`；导入失败保留 `source_name`（basename） |

回退：`artifacts/model-library-logging/l5/service.py.before`。

---

# L6：补齐操作分段与"没有发生的事"（本轮）

出发点：任务书第三节要求每次操作记录"接受、执行、关键阶段、最终结果"。
上一轮之后仍有两类空白——**操作内部的分段不可见**，以及**没有发生的动作
（比如封面请求被跳过）完全没有痕迹**。后者正是"封面一直待生成"最难查的原因。

## 1. 导入：四个真实分段

`importer._import_one` 里本来就有"发现依赖 / 复制+哈希 / 校验 / 事务提交"，
但只有终态可见。现在每段一条事件：

| 事件 | 关键字段 | 取自 |
| --- | --- | --- |
| `model_import.dependencies_detected` | `dependency_count`、`missing_count`、`absolute_count`、`missing[≤8]`、`duration_ms` | `detect_references` + `plan_package` |
| `model_import.copied` | `file_count`、`total_bytes`、`duration_ms` | `_stage` 复制完成 |
| `model_import.validated` | `file_count`、`digest_verified`、`missing_count` | `_stage` 的 SHA-256 校验 |
| `model_import.committed` | `model_id`、`file_count`、`missing_count`、`duration_ms`、`total_duration_ms` | `_commit` 成功 |

**验证（失败场景下也完整）**：`l6/import-stages.json` 是一次**真实失败**的导入
（临时工程没有 managed v2 存储），日志给出

```text
model_import.dependencies_detected  dependency_count=2 missing_count=0 duration_ms=12.47
model_import.copied                 file_count=3 total_bytes=8285 duration_ms=16.54
model_import.validated              file_count=3 digest_verified=true
（随后 failed，没有 committed）
```

也就是说：**"停在哪一步"现在有答案**——依赖发现 2 个、复制 3 个文件 8.3 KB、
哈希校验通过、提交阶段失败。`stage_order_ok=true`、`committed_absent=true`。

## 2. 导出：清单冻结与逐项失败

- `model_export.manifest_fixed`：`model_count`、`missing_count`、`destination_name`，
  在第一个字节写出**之前**记一次（对应"清单冻结"语义，之后不再重读）。
- `model_export.item_failed`：`model_id`、`state`、`error_code`、`missing_count`，
  逐模型失败/缺失时记录。批量导出不再只有一个汇总结果。

## 3. 封面：把"没有发生的事"也记下来

`ensure_covers` 的三个提前 return 过去完全静默，于是"封面待生成"在日志里查不到原因。
现在：

| 情况 | 事件 | 字段 |
| --- | --- | --- |
| 预览能力关闭 | `model_thumbnail.request_skipped` | `reason=preview_capability_off` |
| 只读 | 同上 | `reason=readonly` |
| 服务已关闭 | 同上 | `reason=closed` |
| 正常决策 | `model_thumbnail.planned` | `requested` / `already_ready` / `in_flight` / `cancelled_off_screen` |
| 单个请求 | `model_thumbnail.requested` | `model_id`、`via=multimaterial|native` |

配合已有的 `thumbnail.cache_written`（含 `cache_hit`）与 `ui.image.accepted`，
"待生成"可以区分成六种可行动的原因。

## 4. 原生进程：实例身份

`EVENT_WORKER_STARTED` / `EVENT_WORKER_LOST` 增加 `worker_instance_id`（每次启动
新的 12 位 id）与 `process_id`。原来只有 `worker_pid`，重启前后无法区分
"同一个进程的两次握手"与"新进程"。

**未验证**：没有真实原生进程可用（`f3d` 需先于 Qt 导入，且本轮未起 worker），
所以这两个字段只做了编译与字段审计，**没有真实进程落盘证据**，如实标注。

## 5. 本轮自己踩的坑（写下来防止再犯）

第一次打补丁时，importer 用了 `EVENT_IMPORT_*` 常量却只导入了
`record_model_event`，`NameError` 被导入线程的 `except Exception` 吞掉，
**每一次导入都变成 `unexpected_error`（11 ms 失败）**。这正是任务书 8.5
"日志故障不得让导入失败"要防的事，而它被我的埋点触发了。

修法与防再犯：
- 补齐常量导入；
- 新增 `tests/model_library/test_logging_stage_coverage.py::test_new_events_are_declared`
  与一个 AST 审计（`fix_exporter_import.py` 的检查段），确保模块里用到的
  `EVENT_*` 要么已导入、要么在 `events` 里存在；
- `test_import_stage_events_land_in_the_log` 会在阶段事件缺失时立刻失败。

## 6. 覆盖矩阵变化（相对 L3 记录）

| 领域 | 之前 | 现在 | 依据 |
| --- | --- | --- | --- |
| 导入 | 部分（只有 queued/completed/failed） | **部分（四段可见）** | §1 |
| 模型封面 | 部分（主体已分，缺"为什么没请求"） | **部分（六种原因可分辨）** | §3 |
| 原件导出 | 部分（只有汇总） | **部分（清单冻结 + 逐项失败）** | §2 |
| 原生进程 | 部分（缺实例身份） | **部分（字段已加，未真实验证）** | §4 |

## 7. 回退

`artifacts/model-library-logging/l6/` 下 `importer.py`、`exporter.py`、
`native_jobs.py`、`events.py`、`service.py` 的 `.before` 副本。

---

# L7：材质库选择器（本轮）

任务书第三节里，材质库选择器的八个动作此前**一条日志都没有**——这也是任务书
六.2「图片全部不可用」最难查的原因：卡片停在白球上，日志里连"读过图"都没有。

## 1. 补的八个动作

| 动作 | 事件 | 关键字段 |
| --- | --- | --- |
| 打开 | `material_library_opened` | `records`、`folder`、`searching`、`generation` |
| 查询 | `material_library_query` | `records`、`with_thumbnail`、`empty`、`subfolders`、`folder`、`searching` |
| 可见图片请求 | `material_cards_images`（补字段） | `requested`、`pending`、`ready`、`bytes`、`generation` |
| 缓存/原件读取 | `material_thumbnail_read` | `outcome`、`material`、`attachment`、`source_bytes`、`passthrough` |
| 选择 | `material_library_selection` | `material`、`previous`、`multi` |
| 关闭 | `material_library_closed` | `selected`、`pending_reads` |

「图片回传」由既有的 `material_cards_images` 承担，本轮把它的口径从
"发了多少张图"改成"请求几张、其中几张仍在 pending、几张已就绪"——原来的数字
无法区分"回传了但内容还是占位图"。

## 2. 五态读取结果（任务书六.2 的核心）

`material_thumbnail()` 是唯一的图片读取点，它有四种失败返回和一种成功返回。
现在每一种都留痕：

| outcome | 含义 | 用户下一步 |
| --- | --- | --- |
| `no_thumbnail_record` | 记录里根本没有缩略图 id | 需要重新生成球面图 |
| `missing_attachment` | 有 id 但附件表里没有该条目 | 附件索引/存储问题 |
| `decode_failed` | 有字节但 QImage 解不出来 | 文件损坏 |
| `discarded` | 该记录的调整已被丢弃 | 需要重新渲染 |
| `ready` | 正常读出（附是否原样透传） | — |

## 3. 验证

`tests/model_library/test_logging_material_selector.py`（6 项，全绿）：

- 五种读取结果各自被记录，`missing_attachment`/`decode_failed` 带附件 id 与字节数；
- `ready` 记录 `passthrough=true`（原样发送，未重新压缩）；
- 回传拆分：请求 2 张、pending 1 张、ready 1 张，且只对 pending 的那张发出
  `thumbnail_requested`。

驱动方式：`material_thumbnail` 是纯函数，五种情形都用真实 `QImage`/真实字节造出来；
`_send_images` 用普通对象当 `self`（Qt 子类不能 `object.__new__`），
被替换的只有 trace 出口。**没有 mock 被测函数本身。**

## 4. 未做

- 打开/查询/选择/关闭四条只做了代码埋点与编译检查，**没有真实 UI 会话验证**
  （需要主窗口；本轮未起）。它们的字段设计来自 `refresh()` / `showEvent()` /
  `hideEvent()` / `_selection_changed()` 的真实变量，不是臆造。
- 「原件读取」（从磁盘贴图现场生成预览）路径没有单独事件：材质球的生成在
  `material_render`/native 侧，属于下一批。

## 5. 回退

`artifacts/model-library-logging/l7/material_cards.py.before`、
`l7/material_library.py.before`。

---

# L8：材质编辑动作（本轮）

任务书三节"多材质编辑"列了九个动作。此前只有 parse / commit / install 三段，
"我做的修改为什么没生效"无法回答。

## 1. 补的动作

`_edit()` 是所有编辑动作的唯一分发点，现在每个分支留一条记录：

| 动作 | 事件 | 摘要字段 |
| --- | --- | --- |
| 数值 / 贴图三态 | `material.edit.patch` | `slots[≤8]`、`texture_actions`（`basecolor:set` / `normal:clear` / `x:inherit`）、`channels`、`slots_truncated` |
| 库替换（表面） | `material.edit.replace_surface` | `kept_slots`（保留的 opacity/sss/displacement）、`snapshot`（前 16 位） |
| 库替换（完整） | `material.edit.replace_full` | `snapshot` |
| 复制粘贴 | `material.edit.paste` | `snapshot`、`resource_count` |
| 独立材质 | `material.edit.make_unique` | `clone_count`、`rebound_bindings` |
| 恢复单个材质 | `material.edit.restore_material` | `material_count` |
| 恢复全模型 | `material.edit.restore_model` | `binding_count` |

所有事件带完整身份：`project_session` / `open_epoch` / `model_id` /
`source_revision` / `catalog_revision` / `state_revision` / `request_id` /
`operation` / `material_count` / `binding_count`。

**不记录**：贴图字节、贴图路径、备注正文、完整快照 token（只留 16 位）。

## 2. 又一次"埋点让业务失败"（同类第二起）

`_note_edit` 原本用固定字段 + `**extra`，而 `restore_model` 分支又传了
`binding_count` → **重复关键字 TypeError**。该异常发生在调用 logger **之前**，
`events` 那层的 try 兜不住，后果是"恢复全模型"直接失败。

修法：`fields = {...}; fields.update(extra)`。教训与 L6 的 `NameError` 同类：
**埋点 helper 自己必须不可能抛异常**。

## 3. 验证

`tests/model_library/test_logging_material_edits.py`（13 项，全绿）：

- PATCH 记录槽名与三种贴图动作；`channel` 动作带通道名；
- MAKE_UNIQUE 的 `clone_count` 与"绑定真的搬走"（同时断言返回值里多了材质、
  `binding-1` 指向新 id）；
- RESTORE_MATERIAL / RESTORE_MODEL 都被记录（后者在 `return` 之前）；
- 参数里的标记串不出现在任何字段；
- 7 个事件都在 `REQUIRED_FIELDS` 里声明。

驱动方式：真实 `_edit()` + 真实 `MaterialState`，只替换 trace 出口；
`channel` 分支与 `_initial` 各有一处最小 stub（测试注释里写明）。

## 4. 未验证

`replace_surface` / `replace_full` / `paste` 三条**只有代码埋点、没有单测**：
它们需要冻结快照（`_library_definition` / `_clipboard`），构造成本高于本轮收益。

## 5. 回归里那 5 个失败：既有，不是本轮引入（A/B 证据）

`tests/model_library/multimaterial/test_integrated_runtime.py` 有 5 个失败
（3 个 `test_real_source_binding_loads_in_worker`、`test_real_obj_worker_load_highlight_and_wireframe`、
`test_material_commit_survives_reopen_and_retry`）。A/B 对照：

| 状态 | 结果 |
| --- | --- |
| 本轮改动前（`l8/ab-integrated-before.txt`） | **5 failed** |
| 本轮改动后（`l8/ab-integrated-after.txt`） | **5 failed**，失败项名字完全相同 |

它们需要真实原生 worker（f3d），属于 AI 2 的集成运行域，与日志埋点无关。
脚本：`l8/ab_integrated2.ps1`（会临时切换两个文件，结束时恢复）。

## 6. 回退

`l8/application.py`、`l8/events.py` 是本轮**改动前**的原件（无 `.before` 后缀，
命名如此是为了与 L6/L7 的备份区分）；`l8/patched/` 是改动后的副本。

---

# L9：预览 / 持久化 / 删除恢复 / 生命周期（本轮）

五块一起推进。每块都记录**做到哪、验证到哪、哪还没有**，不把"埋点写了"当成"覆盖了"。

## 1. 交互预览

| 动作 | 状态 | 落点 |
| --- | --- | --- |
| 视口拾取 | **完成并验证** | `service.preview_event` → `material_preview.event`（每个事件一条）+ `material_preview.picked`（带 `binding_id`） |
| 相机变化 / 封面请求 | **完成并验证** | 同上（`has_camera` 字段区分） |
| 高亮 | **完成并验证** | `application.highlight` → `material.edit.highlight`（`binding_count` + 有界 `binding_ids`） |
| 首帧 | **未做** | worker 侧 `material_preview.present()` 之后没有发事件；主进程无从得知"第一帧已到" |
| 窗口显示/前台 | **未做（且无法证明）** | `present()` 调 `showNormal/raise_/activateWindow`，但"是否真的在前台"Qt 与系统都给不出可靠反馈 |
| LOD 切换 | **未做** | worker 侧 `select_lod()` 没有发事件 |

**发现**：`service.preview_event` 原来只处理 `camera_changed` 与 `cover_requested`，
其余事件（含 `material.picked`）**被静默丢弃**——这就是"视口点选没反应"在日志里
查不到任何东西的原因。

## 2. 持久化

| 动作 | 状态 | 事件 |
| --- | --- | --- |
| 事务开始 | **完成并验证** | `model_tx.begin`（`_write_payload`，带 `expected_revision` / `must_exist`） |
| 版本冲突 | **完成并验证** | `model_tx.conflict`，带 `reason` 与 `path` |
| 提交成功 | 已有（L1） | `model_commit_durable` / `model_project.model_applied` |
| 回滚 | **未做** | `patch_batch` / `delete` / `update_cache` 的 `except BaseException: rollback` 分支还没记事件 |
| 保存重开 | **未做** | 需要真实工程保存/打开会话 |

**本轮最有价值的发现**：**同一个"版本冲突"语义散落在四个入口**——

```text
update()        ← 在调用 _write_payload 之前自己先查一次
patch_batch()   ← 自己 BEGIN + 自己查
delete()        ← 自己 BEGIN + 自己查
update_cache()  ← 自己 BEGIN + 自己查
_write_payload()← 唯一被我上一轮埋点的那个
```

只埋 `_write_payload` 时，一次**真实**的过期写入（走 `update()`）在日志里**什么都没有**——
验证时立刻暴露（`test_stale_revision_is_recorded_as_conflict` 首跑失败）。
现在四个入口都记 `model_tx.conflict`，并各自带 `path` 区分。

## 3. 删除与恢复

`commands.TrashItems.redo/undo` 与 `RestoreItems.redo` 统一走 `_note_trash()`：

- `model_items_trashed`：`count`、`kinds[≤8]`、`item_ids[≤8]`、`ids_truncated`、`trash_stamp`
- `model_items_restored`：同上 + `undo` 标记

`trash_stamp` 是"我五分钟前误删了什么"能回答的关键；`kinds` 区分模型与贴图等类别。
**未单测**：`TrashItems` 需要 scene 与真实 item（Qt undo 命令），本轮只做编译与字段断言。

## 4. 生命周期

| 动作 | 状态 | 事件 |
| --- | --- | --- |
| 退出 | **代码完成，未真实运行** | `app_closing`（`storage_running` / `opening_pending`） |
| 资源释放（租约） | **代码完成，未真实运行** | `model_leases_released`（`count`） |
| 切工程 | 已有（L0 起） | `scope_bound` / `query_discarded(reason=scope/revision)` |
| 另存为 | **未做** | `entrypoint.save_as_entry` 未埋点 |

## 5. 材质球生成侧（原件读取 → 渲染 → 入库）

**未做**。落点明确：`prism/widgets/material_render.py` 的 `load_material` / `_record`，
以及 native `material_preview` 的 `renderer.load` / `renderer.apply`。
未做的原因：这一段需要真实渲染管线（VTK actor 与附件读取），本轮没有可运行的
预览会话，无法验证"埋点真的落在渲染路径上"。按本轮一贯口径，**不写没有证据的埋点**。

## 6. 验证与回归

- `tests/model_library/test_logging_lifecycle_and_tx.py`（13 项，全绿）：
  - 真实 SQLite 上触发 `model_tx.begin`；
  - **真实**的过期写入（`expected_revision=99`）产生 `model_tx.conflict`，
    且库里数据未被改动（证明冲突记录不是"失败了才记"，而是失败的那次真的没写）；
  - `preview_event` 两个事件各记一条、`picked` 带 `binding_id`、相机事件带 `has_camera`；
  - `highlight` 带 `binding_count` 与有界 id 列表；
  - 9 个新事件在契约里声明且必需字段齐备。
- 回归：`tests/model_library/test_storage.py`、`test_management.py`、`test_import.py`、
  `test_ui.py`、`test_logging_lifecycle_and_tx.py`、4 个日志套件、`test_diagnostic_bundle.py`、
  `test_b2_readonly_open.py` → **160 passed, exit=0**。

## 7. 回退

`artifacts/model-library-logging/l9/` 下七个 `.before` 前的原件
（`service.py`、`repository.py`、`bundles.py`、`application.py`、`commands.py`、
`__main__.py`、`events.py`）。注意 `events.py`/`application.py`/`service.py`
在前几轮也改过：`l9/` 里的是**本轮改动前**的状态。

---

# L10：首帧 / 保存重开 / 材质球生成（本轮）

## 1. 首帧（worker 侧）

`prism/components/material_preview.py` 的 poll 循环里，`renderer.window.Render()`
之后现在会发一条 `material.first_frame`：

```text
bindings        部位（actor）数量
lod_levels      识别到的 LOD 级别数
shown           是否被要求显示窗口
headless        是否离屏
```

只对 `load` / `open` 动作发**一次**（`first_frame_sent` 标记），滑块拖动与高亮不会重复刷。
主进程侧不用改动：上一轮起 `service.preview_event` 已经转发**所有** worker 事件。

**验证边界**：worker 需要 VTK 窗口 + IPC 通道才能跑，本轮用"源码位置 + 契约 +
触发一次"三重断言（`test_first_frame_event_source_is_present`），**没有**真实
worker 落盘证据。

## 2. 材质球生成入库

`material_cards.update_thumbnail()` 是"卡片拿到最终图片"的唯一入口。它原来无论
有没有真图都会把 `thumbnailPending` 置 False 并塞占位图 —— 于是"一直白球"和
"图还没到"在日志里**完全一样**。现在多一条：

```text
material_thumbnail_stored   material=… real=true/false dom=… surface_ready=…
```

`real=false` 就是"落到了占位图"。配合 L7 的五态读取结果
（`no_thumbnail_record` / `missing_attachment` / `decode_failed` / `discarded` / `ready`），
"卡片为什么没有真图"可以从读取一直追到安装。

生成链路的其余分段**已有**（`material_render_request` / `material_render_start` /
`material_model_loaded` / `material_frame_submitted` / `material_capture_done` /
`material_render_done` / 失败），本轮没有重复造。

## 3. 保存重开

- **保存**：`model_commit_durable`（L1）+ `model_project.model_applied`（L1）已覆盖
  "写库成功 / 场景已同步"；`window_workspace._store_current_page` 已有
  `workspace.store_current_page` 操作段。
- **重开**：`ModelLibraryService.__init__` 末尾现在记 `model_project_bound`
  （`project_session` / `project_id` / `open_epoch` / `has_importer` / `has_exporter`）。
  打开或重开工程都会绑定一次 service，所以它是"这次会话从哪开始"的锚点。

**验证边界**：绑定事件用真实临时 SQLite 工程验证通过；
"保存到磁盘 → 关闭 → 重新打开 → 读回一致"的完整往返**仍受阻**，
因为缺少可编程创建 managed v2 工程的入口（L5 已记录）。

## 4. 验证与回归

- `tests/model_library/test_logging_session_marks.py`（7 项，全绿）：
  - 真实临时 SQLite 工程上构造 service → `model_project_bound` 带
    `session_token` / `project_id` / `open_epoch`；
  - `update_thumbnail(None)` → `real=false` 且卡片仍有占位图、pending 已清；
  - `update_thumbnail(<data url>)` → `real=true`；
  - 三个新事件在契约里声明；
  - 首帧事件的位置与"只发一次"在源码上被断言。
- 回归：cards / ui / management / storage / import / 5 个日志套件 / diagnostic_bundle
  → **158 passed, exit=0**。

## 5. 仍未做

- 真实 worker 的首帧落盘（需要 VTK + IPC）
- 窗口是否真的在前台（Qt 与系统都不给可靠反馈）
- LOD 切换事件
- 保存→关闭→重开的完整往返（缺 managed v2 工程入口）
- 回滚分支（`except BaseException: rollback`）

## 6. 回退

`artifacts/model-library-logging/l10/` 下 `material_preview.py`、
`material_cards.py`、`service.py`、`events.py`（本轮改动前原件）。

---

# L11：A—F 六项缺口（本轮）

按 `audit/GAPS.md` 的清单逐项做。**先看 §0**：本轮埋点一度让所有原生任务失败，
并且这个缺陷从 L6 一直存在到我这一轮才发现。

## 0. 事故：缺 `import uuid`，原生路径全线失败（L6 引入，L11 修复）

L6 给 `native_jobs` 加 `worker_instance_id` 时用了 `uuid.uuid4()`，而该模块
**没有 import uuid**。`py_compile` 只查语法、源码断言查不出名字未定义；运行时
`_serve` 抛 `NameError` → 被 `except Exception` 捕获 → 发 `EVENT_WORKER_LOST` →
**封面、预览、高亮等所有原生任务失败**。

修复后证据：

| 套件 | 修复前 | 修复后 |
| --- | --- | --- |
| `test_native_jobs.py` | 4 failed | 40 passed（与 `test_f3d_backend.py` 合计） |
| `test_f3d_backend.py` | 1 failed | 同上 |
| `multimaterial/test_integrated_runtime.py` | 5 failed | **5 passed** |
| `tests/model_library` 全量 | 15 failed | **709 passed, 9 skipped, 0 failed** |

## 0b. 更正 L8 的结论（重要）

L8 记录里写过：

> `test_integrated_runtime.py` 5 项失败需要真实原生 worker，属 AI 2 域，**与日志埋点无关**，
> 并用 A/B（改动前/改动后都失败）作为证据。

**这个结论作废。** A/B 的两个版本都包含 L6 引入的 `uuid` 缺陷，所以前后都红，
我把"两版都红"误读成"与埋点无关"。修复 `uuid` 后这 5 项立即全绿。

方法论教训（比事故本身更重要）：

> A/B 对照只有在"before 是最近一次已知良好版本"时才成立。
> 如果缺陷在更早的轮次引入，用"当前代码去掉本轮改动"当 before，会把旧回归
> 归因为"既有问题"。本轮之后改为：**每次改动后跑一次全量，并保留上一轮的全量结果
> 做比较**，而不是只比较本次改动的两个变体。

## 1. A：七个死事件 → 补了 4 个

| 事件 | 状态 | 落点 |
| --- | --- | --- |
| `appearance.snapshot` | **已补并验证** | `service.update_appearance`（revision / slot_count / payload_bytes） |
| `export.written` | **已补并验证** | `exporter._export_one` 逐文件写出 |
| `export.validated` | **已补并验证** | 逐文件哈希核验后汇总 |
| `export.published` | **已补并验证** | 临时目录 rename 成最终目录后 |
| `appearance.derived_maps` | **未做** | 仓库里没有派生贴图生成路径，硬发会是假事件 |
| `project.save_snapshot` | **未做** | 没找到真正的"保存工程"入口 |
| `project.save_confirmed` | **未做** | 同上 |

## 2. B：十一个被压平的生命周期事件 → 全部恢复

`composition._event` 现在**双写**：保留 `service_job`（既有查询不断），
同时按 `state` 记一条真实事件名（`model_queued` / `model_completed` / …）。
阶段可按事件名检索，`REQUIRED_FIELDS` 对这些名字开始生效。

## 3. C：七个零记录模块 → 补了 2 个（+1 部分）

| 模块 | 状态 | 事件 |
| --- | --- | --- |
| `fileio/attachments_store.py` | **已补并验证** | `material_attachment_miss`（attachment / cached） |
| `widgets/material_slots.py` | **已补（未单测）** | `material_slot_drop`（file_name / extension，不记全路径） |
| `components/material_preview.py` | **部分** | 首帧 / LOD / 窗口请求已加；读取器选择与失败原因未加 |
| `material_details.py` / `model_material_picker.py` / `model_worker.py` / `project_storage.py` | **未做** | 见 `audit/GAPS.md` 的落点 |

## 4. D：未验证项 → 补了 3 个

- **LOD 切换**：`material.lod_selected`（level / bindings / hidden / levels）
- **窗口显示/前台**：`material.preview_presented` 带 `frontmost='unknown'`——
  Qt 与系统都不给可靠答案，就不假装知道
- **回滚**：`model_tx.rollback`（patch_batch）、`model_tx.committed`（delete）
- 仍受阻：保存→重开往返（缺 managed v2 工程入口）、回收站/退出真实验证

## 5. E：诊断包 → 补了异常栈

`collect_bundle` 从保留日志里提取**脱敏后的调用栈**（≤8 条、每条 ≤4000 字节），
写进 `summary.exceptions`。实测：

```text
Traceback (most recent call last):
  File "<path:69a74c5488ea.py>", line 3, in run
ValueError: boom
```

用户目录变成一致的路径 token，异常类型与消息保留。另有 `summary.log_footprint`
（文件数 / 总字节 / 目录条目 / 轮转上限）。**未做**：`queue` / `resources` 摘要仍为
`not_supplied`（需要 service 提供稳定句柄，不能猜私有属性）。

## 6. F：可靠性 → 补了 2 项

回滚留痕、日志占用报告。另踩到一个**脱敏陷阱**：字段名 `bytes` 在隐私禁列里，
值会被替换成 `<redacted>`；第一版 `log_footprint` 因此显示 `"<redacted>"`，
改名 `total_bytes` 后正常。**写日志字段名先看禁列**（`bytes`/`data`/`text`/`payload`…）。

## 7. 验证

- `tests/model_library/test_logging_full_coverage.py`：**12 passed**
- 原生路径恢复：`test_native_jobs.py` + `test_f3d_backend.py` = **40 passed**；
  `test_integrated_runtime.py` = **5 passed**
- 全量 `tests/model_library` + 5 个诊断/日志套件：**709 passed, 9 skipped, 0 failed**

## 8. 回退

`artifacts/model-library-logging/l11/` 下本轮改动前的原件
（`composition.py`、`exporter.py`、`repository.py`）；其余文件的历史版本在
`l6/`、`l9/`、`l10/` 里。`native_jobs.py` 的 `import uuid` 是**修复**，不要回退。

---

# L12：最后四项落点 + 第四次同类事故（本轮）

## 0. 事故：`width`/`height` 不在作用域（第四次）

我在 `model_worker` 的 `handle()` 里插了一条 `model_cover_render_started`，
用了 `int(width)` / `int(height)` —— 那两个名字在该方法里**不存在**（渲染尺寸是
`payload.get('width')`）。`NameError` 让**真实封面渲染**失败，
`test_f3d_backend.py` + `test_native_jobs.py` 共 2 项变红。

修法：改用 `payload.get('width') or 512`，与 `render.render(...)` 本身取值一致。
修后 `test_f3d_backend.py` + `test_native_jobs.py` = **40 passed**。

这是同一类错误的第四次（前三次：`EVENT_IMPORT_*` 未导入、`binding_count` 重复
关键字、`uuid` 未导入）。四次都发生在"埋点写在没读透的函数里"。

**流程已改**：每次埋点后必须跑**含真实渲染/真实存储**的测试
（`test_f3d_backend.py`、`test_native_jobs.py`、`test_storage.py`），
不再只跑语法检查与源码断言。

## 1. 本轮补的四项

| 项 | 状态 | 落点 / 字段 |
| --- | --- | --- |
| 数值编辑的 UI 入口 | **已补并验证** | `material_library.save_settings` → `material_settings_edited`（`blend_mode` / `normal_convention` / `alpha_cutoff` / `tag_count` / `changed[≤8]`） |
| 材质库选择器（模型上下文） | **已补，链路未验证** | `model_material_picker._note_picker` → `material_picker.applied`（`selected` / `name` / `entries` / `visible` / `blocked`） |
| 原生封面 worker | **已补并验证（真实渲染）** | `model_worker.handle` → `model_cover_render_started`（`mode` / `width` / `height`） |
| 诊断包的队列/租约数字 | **已补并验证** | `diagnostics_dialog._service_queue` → 快照 `queue`：`futures` / `native_running` / `leases` / `imports_pending` / `preparing`，并标注 `basis='service-private-attributes'` |

选择器的**链路**为什么仍是"未验证"：测试里用替身驱动 `_note_picker` 时，
替换后的 sink 收不到调用，原因没有定位。按本项目口径，**不把没验证的写成已验证**，
所以那条测试只断言"调用点在 `_apply` 的第一位置"，并在 docstring 里写明它不证明什么。

## 2. 仍然确认做不了的（不是没时间，是没有实现路径）

| 项 | 原因 |
| --- | --- |
| `appearance.derived_maps` | 仓库里没有派生贴图（ORM/法线）生成代码，硬发就是假事件 |
| `project.save_snapshot` | 找不到"保存工程"的入口（`__main__` 只有恢复快照 `_write_recovery_snapshot`） |
| `project.save_confirmed` | 同上；保存持久化目前只能靠 `model_commit_durable` 间接推断 |
| `project_storage.py` 内部失败 | 该文件的附件登记/迁移在 `composition` 调用点可观测；函数内部失败没有记录，且它决定着保存时保留哪些附件行 |
| 保存→关闭→重开往返 | 缺可编程创建 managed v2 工程的入口 |
| 回收站/退出的真实验证 | 需要真实 scene 与关闭流程 |

## 3. 验证

- `tests/model_library/test_logging_remaining_gaps.py`：**7 passed**
  （设置编辑的真实字段、worker 事件位置、快照队列数字、契约声明；
  选择器只断言位置并注明未验证链路）
- 真实渲染回归：`test_f3d_backend.py` + `test_native_jobs.py` = **40 passed**
- 全量：`tests/model_library` + 3 个诊断套件，见 `l12/regression-l12.txt`

## 4. 回退

`artifacts/model-library-logging/l12/` 下本轮改动前的原件
（`material_library.py`、`model_material_picker.py`、`model_worker.py`、
`diagnostics_dialog.py`、`events.py`）。
