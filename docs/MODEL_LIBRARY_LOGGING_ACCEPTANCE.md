# 模型库日志与诊断：验收记录（L0）

- 记录日期：2026-10-27
- 环境：`.venv\Scripts\python.exe`（Python 3.12.8 / PyQt6 6.7.1 / VTK 9.7.1 / F3D 4.0.0）
- 运行方式：`QT_QPA_PLATFORM=offscreen`；未启动可见界面、未控制桌面、未接触用户正式工程
- 原始证据：`artifacts/model-library-logging/l0/`

**结论先说：本轮只完成 L0。** 任务书第十一节列的 11 项验收里，
**0 项通过、1 项部分通过（日志故障注入）、10 项受阻**；L1—L4 未开始。
下面把"跑了什么、结果是什么、缺什么条件"逐条写清。

---

## 1. 本轮实际执行的验证

| # | 验证 | 命令 | 结果 | 证据 |
| --- | --- | --- | --- | --- |
| V1 | 诊断包内容：UI 现状 vs 传入模型快照 | `l0/probe_bundle.py` | A（现状）**无** `model-diagnostic.json`；B（传快照）**有** | `l0/bundle-probe.json`、`l0/bundle-{a,b}.zip` |
| V2 | 真实日志的事件与身份填充 | `l0/probe_identity.py` | 30,461 行 / 553 条模型事件；6 个身份字段填充率 **0%** | `l0/event-inventory.json`、`l0/identity-fill.json` |
| V3 | 最小修补正/负分支 | `l0/verify_repair.py`、`l0/verify_negative.py` | 三条分支均按预期（含"快照抛异常时诊断包照常导出"） | `l0/repair-verification.json`、`l0/repair-negative.json`、`l0/bundle-*.zip` |
| V4 | 既有诊断/日志契约未破坏 | 见 §5 命令 | **53 passed** | 终端输出（本文 §5 复述） |

补充参照：仓库里已有的 `tests/model_library/acceptance/diagnostics_evidence.py`
会在**自己**的调用里传 `model_snapshot`，所以它产出的诊断包**有**
`model-diagnostic.json`（`l0/d3-baseline/bundle.zip`）。这与 V1 的 A 组对照，
正好证明"能力存在、UI 通路没接"，而不是"根本没有这个能力"。

---

## 2. 任务书第十一节 11 项验收状态

口径：**通过**＝有真实任务 + 落盘证据；**部分**＝有证据但只覆盖其中一部分；
**受阻**＝入口或环境不具备；**未测**＝具备但本轮没跑。

| # | 验收项 | 状态 | 说明 / 解锁条件 |
| --- | --- | --- | --- |
| 1 | 导入 FBX、OBJ、GLB、glTF | **受阻** | 需要打开真实工程会话并由 UI 触发导入；本轮未启动应用会话。样本与解析路径已就绪（`tests/model_library/acceptance/samples/`） |
| 2 | 多材质识别、第二个材质选择、对应部位高亮 | **受阻** | 选择/高亮是 UI 交互；多材质的**解析与提交/安装**已有真实日志（300+ 条），但"选择"动作没有事件（实现文档 D3/§5） |
| 3 | 球面缩略图成功及故意失败 | **受阻** | 需要原生 preview worker 与缓存目录；本轮未起原生进程 |
| 4 | 材质库封面：正常/未加载/缺失/损坏 | **受阻** | 需要材质库场景数据；缺 `kind` 区分（D5）导致四态无法从日志分辨 |
| 5 | 添加颜色/法线贴图、改参数、提交、保存重开 | **受阻** | 需要真实工程写入；本轮禁止改动用户工程，临时工程装配未做 |
| 6 | 复制粘贴与材质库快照替换 | **受阻** | 同上；且"复制/粘贴"本身没有事件 |
| 7 | 批量整理、重命名、删除与恢复 | **受阻** | `assets_moved`/`models_deleted` 有事件，但批量逐项结果与回收站/恢复无事件 |
| 8 | 单个和批量原件导出并核对哈希 | **受阻** | `export_originals` 有 queued/completed/failed，缺清单冻结与哈希核验分段 |
| 9 | 快速切模型/切工程、取消后晚到、乱序结果 | **受阻** | 需要 UI 会话；`stale` 路径有代码与契约，无本轮实测 |
| 10 | 原生进程崩溃、超时、自动重启 | **受阻** | 需要真实原生进程；`WORKER_LOST`/`exit_code`/`log_tail` 已实现但未实测 |
| 11 | 数据库提交失败、日志队列满、磁盘写入失败、异常退出 | **部分** | 日志队列满/写入失败/字段截断有既有故障注入测试且本轮回归通过（`tests/model_library/acceptance/test_log_fault_injection.py`）；数据库提交失败与异常退出未实测 |

---

## 3. 性能：现有基线（改动前）

来源：`l0/d3-baseline/d3-evidence.json`（`tests/model_library/acceptance/diagnostics_evidence.py`
产出，5 轮 × 2000 事件/轮，同一进程、同一机器）：

| 模式 | 每事件耗时 | P95 | 相对 off |
| --- | --- | --- | --- |
| off（trace 关闭，调用仍在） | 5.13 µs | 5.25 µs | — |
| normal | 8.86 µs | 8.93 µs | +72.71% |
| diagnostic | 9.26 µs | 9.57 µs | +80.51% |
| 取一次诊断快照 | 56.9 µs（单次） | — | — |

说明与限制：

1. 这是**单事件口径**（`record_model_event` 一次调用 + 入队），不含落盘 I/O 等待，
   也**不是**端到端用户操作耗时。
2. 本轮唯一代码改动是诊断导出路径（导出时才取一次快照，56.9 µs 量级），
   **不影响业务热路径**；因此"改动前后对照"在诊断导出路径上是
   V3 的三条分支，而不是这张表。
3. 任务书要求的 L4 对照（同一数据、交错重复、P50/P95/P99 + 最长 GUI 停顿 +
   内存峰值 + 日志增长）**未做**。开销预算也未冻结——按任务书要求，
   预算必须在拿到用户操作级基线之后才能定，本轮没有该基线，故不宣称任何百分比。

---

## 4. 仅凭一份诊断包，能定位什么、不能定位什么

问题清单来自 `l0/d3-baseline/d3-evidence.json` 的 `problem_index`，
判定依据是真实产出的诊断包（`l0/bundle-b-with-snapshot.zip`、
`l0/d3-baseline/bundle.zip`）的实际内容。

| 问题 | 现在能否定位 | 依据 | 还缺什么 |
| --- | --- | --- | --- |
| 用户做了什么 | **部分** | `summary.problem`（标记时间 + 用户描述）、`origin_action_id`/`trace_id`（26.8% 覆盖） | 操作名与目标对象不稳定；无统一 action 索引 |
| 停在哪一步 | **部分** | `model_service_job.state`（queued/completed/failed/stale）+ `material.*` 阶段事件 | 中间阶段（读文件/哈希/解析/事务）没有事件位（D3） |
| 哪个对象出问题 | **部分** | `model_id` 75.9%；`material_id`（截断的拼接串） | `binding_id`/`working_material_id` 为 0%（D4） |
| 为什么失败 | **部分** | `error_message` + 脱敏栈（`diagnostics.Redactor`） | 缺 `error_code`；异常摘要没有阶段字段 |
| 数据是否已保存 | **否** | `model_commit_confirmed` 存在，但"数据库提交成功"与"场景已同步"共用一条 | 需要拆成两条事件；需要"草稿是否保留"字段 |
| 如何复现 | **部分** | 版本/平台/能力/队列计数/日志完整性 | 缺操作参数摘要（当前只白名单，不记录参数形态） |
| 日志自身是否完整 | **是** | `pipeline.model_log_completeness`（dropped/failures/pending）+ `trace_stats` | — |
| 活动任务与队列 | **是**（脚本路径）/ **否**（UI 路径，修复前） | `l0/d3-baseline/bundle.zip` 的 `model-diagnostic.json` 含 queue submitted/started/completed；修复前 UI 导出没有该文件 | 修复后 UI 路径已有；queue/resources/job 句柄仍未接入（§4 of IMPLEMENTATION） |

**直答**：一个没参与实现的人，拿到**修复后**的诊断包，能回答"日志是否完整、
当前有哪些任务在跑、属于哪个模型、停在哪个大阶段"；**不能**回答
"这个材质的具体使用点做了什么、数据库是否已提交、窗口是否真的显示、
失败发生在读文件还是解码"。后者需要 L1/L2 的埋点，本轮没有做。

---

## 5. 复现命令与结果

```powershell
$env:QT_QPA_PLATFORM='offscreen'

# V1 诊断包 A/B
.venv/Scripts/python.exe artifacts/model-library-logging/l0/probe_bundle.py
#   A entries=24  has_model_diagnostic=False  omissions=[snapshot_not_supplied]
#   B entries=25  has_model_diagnostic=True   omissions=[]

# V2 真实日志统计（只读，不复制原文）
.venv/Scripts/python.exe artifacts/model-library-logging/l0/probe_identity.py
#   model events=553  record shape (has_event,has_kind): 30431 / 120
#   0% 字段: parent_span_id, working_material_id, source_material_id,
#            binding_id, worker_instance_id, process_id

# V3 修补正/负分支
.venv/Scripts/python.exe artifacts/model-library-logging/l0/verify_repair.py
.venv/Scripts/python.exe artifacts/model-library-logging/l0/verify_negative.py
#   with-service/without-window: has_model_diagnostic=True, omissions=[]
#   snapshot-raises: bundle_ok=True, model_snapshot={state: failed}

# V4 既有契约回归
.venv/Scripts/python.exe -m pytest -q tests/test_diagnostic_bundle.py `
  tests/test_diagnostic_safe_metadata.py tests/test_performance_trace.py `
  tests/model_library/test_multimaterial_event_integration.py `
  tests/model_library/acceptance/test_logging_contract.py `
  tests/model_library/acceptance/test_log_fault_injection.py
#   53 passed in 61.00s
```

---

## 6. 失败 / 受阻 / 未测清单

- **失败**：无（本轮改动经 V3 三分支验证；V4 回归全绿）。
- **受阻**（入口或环境不具备，需解锁条件）：验收项 1—10。
- **未测**（具备但没跑）：
  - L4 性能对照（改动前后、用户操作级 P50/P95/P99、最长 GUI 停顿、内存峰值、日志增长）；
  - 退出路径的"有限等待 + 未完成刷新数"取证；
  - 轮转失败（多进程/权限）时的计数与上报；
  - 数据库提交失败、异常退出后的残缺日志可读性；
  - 原生进程崩溃/超时/重启。
- **本轮已知副作用（仅限该子进程）**：`diagnostics_evidence.py` 结束后把该进程的
  trace 全局模式留在 `diagnostic`（`mode_after: "diagnostic"`），并且不恢复。
  它运行在独立子进程里，进程退出后对其它进程没有影响（已复核：新进程
  `performance_trace.mode()` 仍为 `normal`）。该行为记为一个 P3 观察项——
  任何**在同一进程内**依次跑多个诊断脚本的用法都会被它的残留模式影响。

---

## 7. 回退

- 代码：把 `artifacts/model-library-logging/l0/diagnostics_dialog.py.before`
  拷回 `prism/widgets/diagnostics_dialog.py` 即可。
- 证据：`artifacts/model-library-logging/l0/` 全部为新增，删除即可；
  未改动任何既有 artifacts 目录。

---

# L1 / L2 验收记录（第二轮）

## 1. 本轮新增的实际验证

| # | 验证 | 命令 | 结果 | 证据 |
| --- | --- | --- | --- | --- |
| V5 | 真实写入路径 → 真实 trace writer → 真实 JSONL | `l1/probe_l1.py` | 正常：`model_commit_durable` → `model_project.model_applied`；失败：`model_commit_durable` → `model_commit_apply_failed`（`durable=true`, `error_code=RuntimeError`） | `l1/l1-probe.json` |
| V6 | 主体命名 + 集合摘要 | `l1/verify_subject_scope.py` | 映射全对；显式 subject 优先；1000 个材质时摘要长度不变、明细固定 8 条、顺序稳定 | `l1/subject-and-scope.json` |
| V7 | 交错重复开销对照（A/B×3 轮） | `l1/ab_overhead.ps1` | off 6.78→6.89 µs（+1.6%）；normal 12.72→13.12 µs（+3.1%） | `l1/overhead-ab.tsv` |
| V8 | 增量回归（5 套件） | `l1/ab_crash.ps1` | before 63 passed EXIT=0；after 63 passed EXIT=0 | `l1/ab-before.txt`、`l1/ab-after.txt` |

V5 的"真实"指：临时工程 SQLite（`ensure_model_tables` 建表）+ 真实
`ModelLibraryService.rename/patch_metadata_batch` + 真实 `PerformanceTrace`
writer + 真实 `.jsonl` 落盘。**没有 mock `record_model_event`**。

## 2. 性能：交错对照取代了跨运行对照

- 跨运行对照曾给出 +90%（off 5.13→9.81 µs），但**该数字作废**：
  before 基线取自 `l0/d3-baseline`，而 after 与它不在同一时间窗口，
  这台机器上同时有其他项目在跑测试（同一 before 代码在 V7 里测到 6.78 µs，
  与基线 5.13 µs 相差 32%）。跨运行比较只能测到机器负载漂移。
- 交错重复（3 轮 × before/after 交替，2000 事件/轮）：

  | 模式 | before 中位 | after 中位 | 变化 | 轮内范围 |
  | --- | --- | --- | --- | --- |
  | off | 6.78 µs | 6.89 µs | +1.6% | before 6.74–7.40 / after 6.76–6.98 |
  | normal | 12.72 µs | 13.12 µs | +3.1% | before 12.41–13.55 / after 12.69–13.96 |

- 判定：差异与轮内波动同量级，**没有可分辨的额外开销**。
  但按任务书要求，这**不能**被表述成"低于 X%"的达标结论——开销预算仍未冻结
  （需要用户操作级基线，L4 未开始）。
- 仍然是**单事件口径**，不是端到端用户操作耗时。

## 3. 11 项验收的状态变化

| # | 验收项 | 上轮 | 本轮 | 变化原因 |
| --- | --- | --- | --- | --- |
| 5 | 贴图/参数/提交/保存重开 | 受阻 | **受阻** | 埋点已就位（durable/applied 拆分经 V5 验证），但"保存重开"仍需真实工程会话 |
| 7 | 批量整理/删除/恢复 | 受阻 | **部分受阻** | 批量重命名路径的真实落盘已在 V5 覆盖（`patch_metadata_batch`），回收站/恢复仍无事件 |
| 11 | 数据库提交失败/队列满/磁盘写失败/异常退出 | 部分 | **部分** | 新增"提交成功但场景同步失败"的真实分支证据（V5 失败场景）；磁盘满与异常退出仍未测 |
| 其余 8 项 | 受阻 | 受阻 | 未变 | 需要 UI 会话或原生进程 |

## 4. 仍无法从日志判断的事项（更新）

已能判断：

- 一次元数据/外观写入**是否已落库**（`model_commit_durable`）；
- **场景是否已同步**，失败时是哪个错误（`model_project.model_applied` /
  `model_commit_apply_failed`）；
- 图片任务**是封面、交互预览还是贴图槽**（`subject`）；
- 多材质操作**影响了几个材质**（`material_count` + 摘要），不再只有截断的 id 串；
- 图片**字节是否已落盘**（`thumbnail.cache_written`）与**界面是否接受**
  （`ui.image_accepted`）。

仍不能判断：

- **具体使用点**（`binding_id` 填充率仍为 0%）——"改了这条椅腿"仍无法定位；
- 缩略图**未加载 / 缺失 / 损坏**三态（只完成了主体区分）；
- 事务**回滚**、版本冲突、保存重开；
- 窗口是否真的显示/在前台；
- 导入的依赖发现、哈希核验、复制阶段（契约有事件名，**仍无调用点**）。

## 5. 失败 / 受阻 / 未测（本轮增量）

- **失败**：无（V5/V6/V8 全通过）。
- **既有崩溃（非本轮引入）**：7 个套件组合运行时进程在 106 个测试全绿后
  `ACCESS_VIOLATION` 退出；A/B 复现于改动前后；去掉 `test_ai09_*` 后两边都正常。
  见 `l1/ab7-before.txt`、`l1/ab7-after.txt`。列为待修，未定位。
- **未测**：用户操作级端到端性能；`binding_id` 贯通后的实际事件；缩略图三态；
  事务回滚；磁盘满/异常退出。

## 6. 复现命令（本轮增量）

```powershell
$env:QT_QPA_PLATFORM='offscreen'

# V5 真实路径落盘
.venv/Scripts/python.exe artifacts/model-library-logging/l1/probe_l1.py
#   apply-ok    : model_commit_durable -> model_project.model_applied
#   apply-fails : model_commit_durable -> model_commit_apply_failed (durable=true)

# V6 主体与集合摘要
.venv/Scripts/python.exe artifacts/model-library-logging/l1/verify_subject_scope.py

# V7 交错开销对照（会临时切换代码版本，结束时恢复）
powershell -NoProfile -File artifacts/model-library-logging/l1/ab_overhead.ps1
# V8 增量回归 A/B
powershell -NoProfile -File artifacts/model-library-logging/l1/ab_crash.ps1
```

## 7. 回退（本轮增量）

`artifacts/model-library-logging/l1/` 下五个 `.before` 文件成套拷回；
`patched/` 里保留改动后的副本，便于反向切换。

---

# L2 收尾 / L3 / L4 验收记录（第三轮）

## 1. 本轮新增验证

| # | 验证 | 命令 | 结果 | 证据 |
| --- | --- | --- | --- | --- |
| V9 | 使用点集合摘要 + 封面缓存命中 | `pytest tests/model_library/test_logging_pipeline_contract.py` | 摘要顺序稳定、长度不随规模变化；`cache_hit` 两态可区分 | `l4/pytest-pipeline.txt`（14 passed） |
| V10 | 轮转失败计数 + 尾部残缺 | `l3/probe_l3.py` | `failures 0→1, last_error=PermissionError`，**不抛给调用方**；`101 tail_complete=True` / `102=False`，summary 命中 102 | `l3/l3-probe.json`、`l3/bundle-torn-tail.zip` |
| V11 | 稳态与突发开销（交错重复） | `l4/bench_steady.py`、`l4/bench_pipeline.py` | 稳态 off 7.05 / normal 11.85 / diagnostic 11.97 µs（P95 7.33 / 25.24 / 14.73），557–575 B/事件，`dropped=0`；突发 10000 事件丢 48085（96%） | `l4/overhead-steady.json`、`l4/overhead.json` |
| V12 | 独立验收固化为 pytest | 同上 V9 | 14 项覆盖契约/真实写入/主体/范围/完整性/轮转/快照 | `tests/model_library/test_logging_pipeline_contract.py` |
| V13 | 增量回归 | 6 个套件 | **64 passed, exit=0** | `l4/regression-l234.txt` |

## 2. 11 项验收状态（第三轮）

| # | 验收项 | 状态 | 变化 |
| --- | --- | --- | --- |
| 4 | 材质库封面：正常/未加载/缺失/损坏 | **部分** | 新增"缓存命中 vs 新写"（`cache_hit`）；"未加载/缺失/损坏"三态仍缺 |
| 5 | 贴图/参数/提交/保存重开 | 受阻 | 写入链路（durable/applied）已有真实证据；"保存重开"仍需 UI 会话 |
| 7 | 批量整理/删除/恢复 | 部分受阻 | 批量路径已覆盖；回收站/恢复仍无事件 |
| 11 | 提交失败/队列满/磁盘写失败/异常退出 | **部分（增强）** | 新增：轮转失败计数、尾部残缺检测、突发丢包计数（48085）都可核实 |
| 其余 | 受阻 | 未变 | 需要 UI 会话或原生进程 |

## 3. 性能结论（日志管道，非用户操作）

稳态（`dropped=0`）：

| 模式 | median | p95 | 字节/事件 |
| --- | --- | --- | --- |
| off | 7.05 µs | 7.33 µs | 42.2 B |
| normal | 11.85 µs | 25.24 µs | 557.0 B |
| diagnostic | 11.97 µs | 14.73 µs | 575.0 B |

对照冻结预算 `l4/BUDGET.md`：LP-1（≤40 µs）✓、LP-2（≤900 B）✓、
LP-3（≤2 ms，实测 0.1–0.51 ms）✓、LP-4（≤200 µs，实测 41–57 µs）✓。

**突发行为必须一并读**：10000 个事件连发时队列（384+128）只能承接约 1900 条，
96% 被丢弃并计入 `dropped_total`。一次用户操作只有个位数事件，不受影响；
但"用日志做高频采样"不可行，这一点写进了预算文件的反证条件。

两个作废数字（留痕）：跨运行对比的 +90%（机器漂移）、第一版稳态的 264 µs/事件
（计时窗口把 50 ms settle sleep 算进去了）。

## 4. 仍无法从日志判断的事项（第三轮更新）

已能判断（新增）：

- 一次多材质操作影响了**多少个使用点**（`binding_count` + 摘要 + 有界明细）；
- 封面是从**缓存命中**还是**新写入**（`cache_hit`）；
- 日志**能不能轮转**（`rotation_failures`），以及**尾部是否完整**
  （`manifest.logs[].tail_complete`、`summary.log_integrity`）；
- 突发压力下**丢了多少、按类别丢的**（`dropped_total` / `dropped_by_kind`）。

仍不能判断：

- **具体是哪个 binding**（只有集合摘要，没有逐点事件）；
- 缩略图**未加载 / 缺失 / 损坏**三态；
- 事务回滚、版本冲突、保存重开；
- 窗口是否真的显示/在前台；
- 导入的依赖发现/哈希核验/复制分段；
- **任何用户操作级耗时**（L4 的用户操作口径整块未做）。

## 5. 失败 / 受阻 / 未测（本轮增量）

- **失败**：无。V9–V13 全通过（14 + 64 passed）。
- **既有崩溃（非本轮引入）**：7 套件组合时退出期 `0xC0000005`，改动前后均复现
  （`l1/ab7-*.txt`）。本轮新增的验收文件单独运行正常。
- **未测**：用户操作级端到端性能与内存峰值；缩略图三态；逐点 binding 事件；
  事务回滚；磁盘满；异常退出后的残缺日志（尾部检测已就位，但没有真实崩溃样本）。

## 6. 复现命令（本轮增量）

```powershell
$env:QT_QPA_PLATFORM='offscreen'

.venv/Scripts/python.exe artifacts/model-library-logging/l3/probe_l3.py
.venv/Scripts/python.exe artifacts/model-library-logging/l4/bench_steady.py
.venv/Scripts/python.exe artifacts/model-library-logging/l4/bench_pipeline.py
.venv/Scripts/python.exe -m pytest tests/model_library/test_logging_pipeline_contract.py
.venv/Scripts/python.exe -m pytest tests/model_library/test_logging_pipeline_contract.py `
  tests/model_library/acceptance/test_logging_contract.py `
  tests/model_library/acceptance/test_log_fault_injection.py `
  tests/test_diagnostic_bundle.py tests/test_diagnostic_safe_metadata.py `
  tests/test_performance_trace.py
# 64 passed
```

## 7. 回退（本轮增量）

`artifacts/model-library-logging/l2/`（`service.py`、`events.py`、`application.py` 的
`.before`）与 `l3/`（`logging.py`、`diagnostics.py`、`diagnostic_snapshot.py` 的
`.before`）。`events.py` 被 L1、L2 各改一次，回退用 `l2/events.py.before`
（它等于 L1 之前的状态）。

---

# L5 验收记录（用户操作级：受阻 + 一个缺陷修复）

## 1. 状态

| 项 | 状态 | 说明 |
| --- | --- | --- |
| 四格式导入的端到端旅程 | **受阻** | 四次尝试全部失败于 `managed\.prism-storage.lock` 缺失（存储未初始化），非导入逻辑问题 |
| 失败事件的错误分类 | **已修复** | `error_code` / `source_name` 现在进入事件；见 DEF-7 |
| 既有退出崩溃 | **结论更正** | 已定位为 QWebEnginePage 延迟析构（`tests/conftest.py:146`），非本轮引入 |

## 2. 证据

- `l5/import-journey.json`：四次尝试的 `terminal state=failed`、错误消息、
  原件哈希未变、日志字节数、RSS 变化。
- `l5/logs-*/`：四次尝试的真实 trace（含 `model_service_job` 失败事件）。
- 修复验证：同一段失败现在带 `error_code=unexpected_error`、`source_name=Box.glb`。

## 3. 这轮**没有**拿到的数字（明确未测）

- 四格式导入的**成功**端到端耗时（受阻于工程创建入口）；
- 封面生成、卡片刷新、材质面板、导出的用户操作耗时；
- 最长 GUI 停顿（无 GUI 会话）；
- 任何内存/显存峰值（只有 RSS 采样，且本轮导入都失败，数值无意义）。

## 4. 复现命令

```powershell
$env:QT_QPA_PLATFORM='offscreen'

# 四次导入（会如实显示 failed 与原因；不要把这四次当成成功样本）
.venv/Scripts/python.exe artifacts/model-library-logging/l5/import_journey.py

# 失败事件分类验证
.venv/Scripts/python.exe artifacts/model-library-logging/l5/diag_failure_code.py
```

## 5. 回退

`artifacts/model-library-logging/l5/service.py.before` 拷回 `prism/model_library/service.py`。

---

# L6 验收记录（操作分段 + 封面决策）

## 1. 本轮实际验证

| # | 验证 | 命令 | 结果 | 证据 |
| --- | --- | --- | --- | --- |
| V14 | 导入四段在**失败**场景下完整 | `l6/verify_import_stages.py` | `dependencies_detected → copied → validated`，无 `committed`；`stage_order_ok=true` | `l6/import-stages.json`、`l6/logs-import/` |
| V15 | 封面四种决策分支 | `pytest tests/model_library/test_logging_stage_coverage.py` | 能力关/只读/已关闭/正常决策 + 路由名，7 项全绿 | `l6/pytest-stages.txt` |
| V16 | 埋点常量完整性 | 同上 + AST 审计 | importer/exporter 用到的事件常量全部可解析 | 审计输出 |
| V17 | 增量回归 | 9 套件 | 见 `l6/regression-full.txt` | 同 |

## 2. 状态

- 导入分段：**完成并验证**（真实 trace 落盘，失败场景也完整）。
- 导出分段：**代码完成，未真实运行**（批量导出需要工程里的模型；本轮没有可用工程）。
- 封面决策：**完成并验证**（四个分支 + 路由名）。
- 原生实例身份：**字段完成，未真实验证**（无原生进程）。

## 3. 本轮修掉的一个真实回归（自己引入、自己修）

埋点初版漏导入事件常量 → `NameError` → **每次导入都以 `unexpected_error` 失败**。
已修复并加入三条防护（常量声明测试、AST 审计、阶段落盘测试）。
教训写进了实现文档 §5：埋点必须证明"它自己不会让业务失败"。

## 4. 仍未覆盖（任务书三节逐项）

- 材质库选择器（打开/查询/可见图片/缓存/原件/回传/选择/关闭）：**缺失**
- 交互预览（首帧、窗口显示/前台、LOD、拾取、高亮、关闭）：**缺失**（除 stale/install）
- 材质编辑动作（选择、数值、贴图三态、通道、开关、复制粘贴、库替换、独立、恢复）：**部分**（只有 parse/commit/install）
- 持久化（事务开始/回滚/版本冲突/保存重开）：**部分**（durable/applied 已有，回滚与重开缺）
- 删除与恢复（回收站/恢复/引用变化）：**部分**（只有 `models_deleted` 计数）
- 生命周期（切工程/另存/退出/资源释放）：**部分**（scope_bound / discarded）
- 缩略图"未加载/缺失/损坏"三态：**部分**（决策与缓存命中已分，解码失败缺）

## 5. 复现命令

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.venv/Scripts/python.exe artifacts/model-library-logging/l6/verify_import_stages.py
.venv/Scripts/python.exe -m pytest tests/model_library/test_logging_stage_coverage.py
```

## 6. 回退

`artifacts/model-library-logging/l6/*.before` 成套拷回（`events.py` 与 `service.py`
本轮再改一次，注意用 `l6/` 下的版本而不是更早的）。

---

# L7 验收记录（材质库选择器）

## 1. 验证

| # | 验证 | 命令 | 结果 |
| --- | --- | --- | --- |
| V18 | 五种读取结果 + 回传拆分 | `pytest tests/model_library/test_logging_material_selector.py` | **6 passed**（`l7/pytest-selector.txt`） |
| V19 | 增量回归 | 8 套件（material_cards / lifecycle / slots / auto / ui / 三个日志套件） | 见 `l7/regression-l7.txt` |

## 2. 状态

- 五态读取结果：**完成并验证**
- 回传拆分（requested / pending / ready）：**完成并验证**
- 打开 / 查询 / 选择 / 关闭：**代码完成，未真实 UI 验证**

## 3. 仍未覆盖（任务书三节）

- 交互预览：首帧、窗口显示/前台、LOD、拾取、高亮
- 材质编辑动作：数值、贴图三态、通道、开关、复制粘贴、库替换、独立、恢复
- 持久化：事务开始/回滚/版本冲突/保存重开
- 删除与恢复：回收站、恢复、引用变化
- 生命周期：切工程、另存、退出、资源释放
- 材质球"原件读取→生成→入库"的生成侧分段

## 4. 复现

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.venv/Scripts/python.exe -m pytest tests/model_library/test_logging_material_selector.py
```

## 5. 回退

`artifacts/model-library-logging/l7/*.before` 拷回对应文件。

---

# L8 验收记录（材质编辑动作）

## 1. 验证

| # | 验证 | 结果 |
| --- | --- | --- |
| V20 | 7 个编辑动作事件 + 无内容泄露 | `pytest tests/model_library/test_logging_material_edits.py` → **13 passed** |
| V21 | 增量回归（多材质全部套件 + 4 个日志套件） | 375 passed / 8 skipped / **5 failed** |
| V22 | 那 5 个失败是否本轮引入 | **既有**：A/B 前后都 5 failed 且失败项相同（`l8/ab-integrated-*.txt`） |

## 2. 状态

- `patch` / `make_unique` / `restore_material` / `restore_model`：**完成并验证**
- `replace_surface` / `replace_full` / `paste`：**代码完成，未单测**
- 选择：列表选择已在 L7 覆盖；**视口拾取仍缺**

## 3. 本轮修掉的回归（自己引入、自己修）

`_note_edit` 的固定字段与 `restore_model` 传入的 `binding_count` 冲突 →
`TypeError` → "恢复全模型"会失败。已改为字段合并式；与 L6 的 `NameError`
一起记为"埋点必须不可能抛异常"的同类教训。

## 4. 既有失败（非本轮，已 A/B 证明）

`test_integrated_runtime.py` 5 项：需要真实原生 worker（f3d），属 AI 2 域。

## 5. 仍未覆盖（任务书三节）

- 交互预览：首帧、窗口显示/前台、LOD、视口拾取、高亮
- 持久化：事务开始/回滚/版本冲突/保存重开
- 删除与恢复：回收站、恢复、引用变化
- 生命周期：切工程、另存、退出、资源释放
- 材质球生成侧（原件读取 → 渲染 → 入库）分段

## 6. 复现

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.venv/Scripts/python.exe -m pytest tests/model_library/test_logging_material_edits.py
powershell -NoProfile -File artifacts/model-library-logging/l8/ab_integrated2.ps1
```

## 7. 回退

`artifacts/model-library-logging/l8/application.py` 与 `l8/events.py`
（本轮改动前的原件）拷回对应路径。

---

# L9 验收记录（预览 / 持久化 / 删除恢复 / 生命周期）

## 1. 验证

| # | 验证 | 结果 |
| --- | --- | --- |
| V23 | 事务开始 + 真实版本冲突 | `pytest tests/model_library/test_logging_lifecycle_and_tx.py` → **13 passed** |
| V24 | 冲突后数据未被改动 | 同一测试内断言 `get_payload` 仍是原值 |
| V25 | 预览事件与拾取 | 同上（`material_preview.event` ×2、`picked.binding_id`） |
| V26 | 11 套件回归 | **160 passed, exit=0** |

## 2. 状态

- **完成并验证**：事务开始、版本冲突（四入口）、预览事件转发、视口拾取、高亮
- **代码完成未验证**：回收站/恢复、退出、租约释放
- **未做**：首帧、窗口前台、LOD 切换、保存重开、另存为、材质球生成三段

## 3. 本轮修掉的真实漏覆盖

只埋 `_write_payload` 时，走 `update()` 的过期写入在日志里毫无痕迹
（四个入口各自查 revision）。测试首跑失败暴露了这一点，现四入口都记录并带 `path`。

## 4. 复现

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.venv/Scripts/python.exe -m pytest tests/model_library/test_logging_lifecycle_and_tx.py
```

## 5. 回退

`artifacts/model-library-logging/l9/*`（本轮改动前原件）拷回对应路径。

---

# L10 验收记录（首帧 / 保存重开 / 材质球生成）

## 1. 验证

| # | 验证 | 结果 |
| --- | --- | --- |
| V27 | 工程绑定（真实临时 SQLite） | `pytest tests/model_library/test_logging_session_marks.py` → **7 passed** |
| V28 | 占位图 vs 真图可区分 | 同上（`material_thumbnail_stored.real`） |
| V29 | 首帧事件位置与"只发一次" | 源码断言（无真实 worker） |
| V30 | 12 套件回归 | **158 passed, exit=0** |

## 2. 状态

- **完成并验证**：工程绑定、缩略图入库（真图/占位）、缩略图五态读取（L7）
- **代码完成，未真实验证**：首帧
- **仍受阻**：保存→关闭→重开往返（缺 managed v2 工程入口）
- **未做**：窗口前台、LOD 切换、回滚分支

## 3. 本轮补上的一个"看起来正常"的坑

`update_thumbnail` 无论有没有真图都会清 `thumbnailPending` 并塞占位图 →
"卡片一直白球"与"图还没到"在日志里无法区分。现在 `real=false` 直接指向占位图。

## 4. 复现

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.venv/Scripts/python.exe -m pytest tests/model_library/test_logging_session_marks.py
```

## 5. 回退

`artifacts/model-library-logging/l10/*`（本轮改动前原件）拷回对应路径。

---

# L11 验收记录（A—F 六项）

## 1. 验证

| # | 验证 | 结果 |
| --- | --- | --- |
| V31 | A/B/D/E/F 埋点与契约 | `pytest tests/model_library/test_logging_full_coverage.py` → **12 passed** |
| V32 | 异常栈脱敏（用户目录消失） | 同上 |
| V33 | 原生路径恢复 | `test_native_jobs.py` + `test_f3d_backend.py` → **40 passed** |
| V34 | 之前被判为"既有"的 5 项 | `test_integrated_runtime.py` → **5 passed**（修复 uuid 后） |
| V35 | 全量 | **709 passed, 9 skipped, 0 failed** |

## 2. 结论更正（重要）

L8 曾判定 `test_integrated_runtime.py` 的 5 项失败"与埋点无关、属 AI 2 域"，
并用 A/B 作证。**该结论作废**：缺陷由 L6 的 `import uuid` 缺失引入，
A/B 两版都含该缺陷所以都红。修复后立即全绿。

方法论已写入实现文档：A/B 的 before 必须取"最近一次已知良好版本"，
否则会把旧回归误判为既有问题；此后每次改动都以全量结果与上一轮对比。

## 3. 完成度

| 项 | 完成 |
| --- | --- |
| A 死事件 | 4/7 |
| B 压平事件 | 11/11 |
| C 零记录模块 | 2/7（+1 部分） |
| D 未验证项 | 3/5 |
| E 诊断包 | 异常栈 ✅；queue/resources ❌ |
| F 可靠性 | 2/2 |

## 4. 仍未做

`appearance.derived_maps`、`project.save_snapshot/confirmed`、
`material_details`/`model_material_picker`/`model_worker`/`project_storage` 埋点、
queue/resources 摘要、保存→重开往返、回收站/退出的真实验证。

## 5. 复现

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.venv/Scripts/python.exe -m pytest tests/model_library/test_logging_full_coverage.py
.venv/Scripts/python.exe -m pytest tests/model_library/test_native_jobs.py tests/model_library/test_f3d_backend.py tests/model_library/multimaterial/test_integrated_runtime.py
```

## 6. 回退

`artifacts/model-library-logging/l11/*`（本轮改动前原件）；`native_jobs.py` 的
`import uuid` 是修复，不回退。

---

# L12 验收记录（最后四项落点）

## 1. 验证

| # | 验证 | 结果 |
| --- | --- | --- |
| V36 | 设置编辑记录真实字段 | `pytest tests/model_library/test_logging_remaining_gaps.py` → **7 passed** |
| V37 | 原生封面 worker（真实渲染） | `test_f3d_backend.py` + `test_native_jobs.py` → **40 passed** |
| V38 | 诊断包队列/租约数字 | 同上（`queue.state == 'measured'`，`basis` 标明来源） |
| V39 | 全量 | 见 `l12/regression-l12.txt` |

## 2. 本轮事故（第四次同类）

`model_worker` 的埋点用了作用域里不存在的 `width`/`height` → 真实封面渲染失败
（2 项红）。已修，真实渲染测试恢复 40 passed。

**流程改动**：埋点后必须跑含真实渲染/真实存储的测试，不只跑语法与源码断言。

## 3. 完成度（相对 `audit/GAPS.md`）

| 项 | 现在 |
| --- | --- |
| A 死事件 | 4/7（其余 3 项**确认无实现路径**） |
| B 压平事件 | 11/11 |
| C 零记录模块 | 4/7（`attachments_store`、`material_slots`、`material_details`、`model_worker`；`model_preview` 部分） |
| D 未验证项 | 3/5 |
| E 诊断包 | 异常栈 ✅、队列/租约 ✅、`resources` 缓存字节 ❌ |
| F 可靠性 | 2/2 |

## 4. 仍未做

`appearance.derived_maps`（无实现）、`project.save_snapshot/confirmed`（无入口）、
`project_storage` 内部失败、`model_material_picker` 的链路验证、回收站/退出真实验证、
保存→重开往返。

## 5. 复现

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.venv/Scripts/python.exe -m pytest tests/model_library/test_logging_remaining_gaps.py
.venv/Scripts/python.exe -m pytest tests/model_library/test_f3d_backend.py tests/model_library/test_native_jobs.py
```

## 6. 回退

`artifacts/model-library-logging/l12/*`（本轮改动前原件）。
