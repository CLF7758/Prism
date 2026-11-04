# AI 4：模型多材质独立验收、性能与诊断

## 1. 独立角色

Prism 已有模型库、材质库、托管工程。新增任务是模型内部多材质编辑。你验证真实用户结果、数据正确性和资源边界，不替实现方写业务功能，不把单测数量当成果。

目录 `C:/Users/admin/Desktop/Prism-main/Prism-main`，Python `.venv/Scripts/python.exe`。先读 `docs/MODEL_MULTI_MATERIAL_FRAMEWORK_20261027.md`。主对话负责集成与发布，你交独立证据。

## 2. 当前基线与文件所有权

新 multimaterial package 只有契约、Protocol、默认未实现服务；右侧容器全禁用。旧通道诊断、旧 146 passed 和框架测试不能计为新编辑已通过。

可写 `tests/model_library/multimaterial/acceptance/`、`benchmarks/`、`artifacts/model-multimaterial/`、本任务书执行记录。不改 prism/、其他角色测试或独立期望来迁就实现。

已有真实样本在 `E:/Prism-stress-100k`；先核实清单和哈希，不改变原始资源。不要直接用用户正式工程做故障注入或迁移。默认离屏，禁止控制桌面，不能偷偷启用可见原生测试。

## 3. D0 冻结验收与参考环境

记录源码相关文件哈希、Python/PyQt/F3D/VTK、CPU/RAM/GPU/驱动、磁盘、缓存条件。执行既有基线，原始失败保留，区分旧契约、脚本错误、实现缺陷、待调查。

对每一项建立独立期望、接受条件、反证条件。数据与范围正确性是硬门；性能阈值在实现前结合参考机与负载冻结，不能看到结果才调门槛。

## 4. D1 独立期望集

至少覆盖以下结构，四格式各有真实样本与适用边界：

1. 3 个明确不同材质；同名不同定义。
2. 4 个部位共享一材质；同 mesh 多节点实例；一个 mesh 多 primitive/面组。
3. 无材质、未使用定义、纯数值、缺图与坏文件。
4. 多 UV、非默认变换、双面、RGB/A 共图、ORM、不同法线约定。
5. 内嵌图片、外部 BIN/图、绝对作者路径、同名依赖冲突。
6. alpha mask/blend 与透明重叠；真正 SSS、透射、薄透光分别记录；真实置换与 bump 分开。
7. 源新版本材质重排、增删、改名和无法匹配。

期望从原始 JSON/MTL/FBX 连接、独立工具/作者节点形成，包含来源、许可、SHA-256、预期材质/分配/纹理解释。不能用被测解析器输出当答案。合成结构夹具允许，但不能自行加 SSS 后冒充作者真实样本。

## 5. D2 端到端用户任务

按格式执行并逐步保留实际结果：

导入 → 原材质正确显示 → 选第二个材质 → 改它且其他不变 → 共享材质独立一个部位 → 从库换表面保留旧 alpha → 预览取消 → 再应用 → 撤销重做 → 保存重开 → 删除源库材质后重开 → 清缓存重建 → 工程搬迁重开 → 原件导出哈希。

同时检查仅数值材质、重命名不改绑定、CLEAR 与 INHERIT 不混、旧全模型覆盖迁移不污染原材质。列表、侧栏、原生模型、封面均读取同一已提交状态，草稿/高亮/通道不进入正式封面。

API 成功、截图好看或实现方自述都不足；图像比较需固定相机/灯光并结合独立绑定/数值核对。Qt 替身只能证明对应层，不能签端到端通过。无法打开真实 UI 就明确记未测。

## 6. D3 故障、持久化与资源

故障矩阵：新增贴图暂存时取消；写入失败/磁盘满；状态提交中断；提交后丢 UI 回执；保存中继续编辑；旧回调晚到；切项目；渲染进程崩溃/卡住；缺图；源版本变化；删除模型/回收站/历史释放。

断言：无半事务、已确认修改不丢、未确认不假成功、新修订不被旧保存清脏、只清自身暂存。Undo/Redo 所需图在清理后仍存在；历史释放才允许回收。源库删除不破坏已用快照。原件各文件始终哈希不变。

完整保存、局部保存、另存为、目录工程转换、搬迁分别验。未知嵌套字段/扩展保存重开不丢。旧版程序拒写新结构必须实际验证，不因文档承诺就签通过。

## 7. D4 高级效果独立结算

- 法线：局部覆盖、方向、强度、多 UV，对非目标无影响。
- SSS：记录源证据、目标是否保留、实际算法与厚度/尺度行为。alpha、玻璃、薄表面透光单独列，不能计为真实散射通过。
- 置换：对派生几何与原件分别核对；证明位移，不只光照变化；检查边界、资源和恢复。
- 纯线框：证明无面填充；说明源拓扑边/三角化边；不把 show_edges 算通过。

每项状态只能是通过/失败/受阻/未测，保留阻断原因和反证条件。禁用入口不是功能完成。

## 8. D5 性能、资源与存储成本

模型库 100/1000/1万/10万；单模型 1/10/100/1000 材质。声明模型几何、纹理数量/分辨率/独立文件比例、分配分布、历史长度、冷暖缓存及机器。索引合成集与真实混合集分开。

测解析、选材质、草稿准备、原生安装、首帧、提交、封面、保存、退出。分 GUI、后台、I/O、等待、原生、GC、日志开销。

总方案候选预算：暖选材质属性 P95≤100ms、已准备到画面 P95≤100ms、GUI 最长停顿≤50ms；先冻结条件再计分。冷准备不能排除在报告外。记录 P50/P95/P99、最长停顿、CPU/RAM/VRAM 峰值、50 次往返后驻留/句柄/临时文件趋势。

对 AI 1 的稀疏 JSON/按材质拆表实验独立复核，测单槽读写放大、WAL、工程增长、保存恢复成本。重复对照，至少两轮独立运行；不把缓存命中率当响应结果。

不得靠缩减数据、降画质、关闭 GC 或不计准备时间达标。十万真实混合数据不存在就如实未测，不能让索引基准代替。

## 9. D6 日志与缺陷交付

核对复用 model_library.events 的完整身份、请求、修订、材质、阶段、耗时和错误。一次用户动作能关联解析/草稿/提交/安装，旧结果拒绝可定位。不要求每帧刷日志；诊断包不得塞原模型/贴图。

日志开销比较 off/normal，明确单事件与端到端两个口径，不用其中较好结果覆盖另一个。缺陷写最短序列、期望、实际、证据、版本/范围、反证条件、负责域、阻断等级。

## 10. 交付模板

```text
角色：AI 4；D0—D6 每项状态：
源码/环境/运行时/机器与负载：
独立期望集版本及来源：
真实旅程每格式结果：
持久化/故障/原件哈希证据：
高级效果分别结论：
性能原始样本/分位/峰值/趋势：
缺陷列表与反证条件：
未测/跳过/受阻，不可发布项：
原始命令、日志、诊断包路径：
结论能证明到哪一层：
```

执行记录：本任务书建立时 D0—D6 均未完成；框架不代表验收通过。

---

## 执行记录（2026-10-27，AI 4 第一轮）

```text
角色：AI 4（独立验收、性能与诊断）
D0 冻结            通过（环境/机器/源码哈希/样本清单/既有基线已落盘）
D1 独立期望集      通过（19 个结构夹具 + 17 个真实上游样本，声明与独立读取 0 项不符）
D1 对产品的门      通过（99 项比对里解析层语义一致；会话期间抓到的 3 个缺陷已被实现方修复）
D2 真实旅程        受阻（服务 capabilities 全 False；只证到解析层与载荷往返）
D3 故障/持久化     受阻（无事务、无租约、无源库快照，故障无处注入；原件哈希不变已证）
D4 高级效果        未测（无原生局部材质端口；SSS/置换无真实样本）
D5 性能            部分测（解析层已测；暖选/安装/首帧/提交/封面/保存/退出全部 blocked）
D6 日志与缺陷      部分通过（隐私与掩码通过；材质身份字段缺失=DEF-1 未关闭）
```

### 源码 / 环境 / 运行时 / 机器与负载

- 源码：无 Git，按相关文件记录 SHA-256 → `artifacts/model-multimaterial/d0/source-hashes.json`。
  判定时的实现文件：`prism/model_library/multimaterial/{catalog.py,contracts.py,ports.py,service.py,inspectors/*}`。
  实现方在本次会话内持续修改（同段 `inspectors/fbx.py` 35000 → 39728 字节），
  所以下面每条结论都对应那一时刻的哈希，不是永久结论。
- 运行时：Python 3.12.8 / PyQt6 6.7.1（PyQt6-Qt6 6.7.0）/ VTK 9.7.1 /
  pytest 9.1.1 / numpy 2.5.4 / pillow 13.0.0；`f3d` 发行版 4.0.0 **在本环境
  import 失败**（PyQt6 先导入时 pyf3d DLL 初始化失败），见缺陷文档「环境发现 3」。
- 机器：i9-13900K（24 核 / 32 线程）、137.2 GB RAM、Windows 11 10.0.22621、
  C: 剩余 212 GiB / D: 6.4 TiB / E: 1.2 TiB。GPU 列表里有虚拟显示适配器（详见
  `d0/machine.txt`），因此**没有可用硬件加速保证**，任何渲染性能结论只对这台机器成立。
- 负载：单进程、无并发测量；`QT_QPA_PLATFORM=offscreen`、`PYTHONHASHSEED=0`。

### 独立期望集版本及来源

- 生成器：`tests/model_library/multimaterial/acceptance/build_expectations.py`
  → `artifacts/model-multimaterial/d1/EXPECTATIONS.json`（schema 1，附 `REPORT.md`）。
- 结构夹具 19 个（glTF/GLB/OBJ+MTL，含确定性 PNG）：三材质、四部位共享一材质、
  同名两定义（glTF 与 MTL 各一）、单 mesh 三 primitive、无材质+未使用定义+纯数值、
  悬空材质引用、缺图（glTF/OBJ）、坏 JSON、坏 FBX 容器、多 UV + `KHR_texture_transform`
  + 双面 + RGB/A 共图 + ORM 共图、`MASK`/`BLEND` 重叠、透射 / 体积 / 普通半透明三分、
  GLB 内嵌贴图、绝对作者路径（glTF/OBJ）、源版本重排改名、源版本完全无法匹配、
  bump 与 normal 分开。
- 真实上游样本 17 个（Khronos glTF/GLB、assimp OBJ/FBX、Poly Haven CC0），
  来源、许可、SHA-256 全部随期望集落盘；清点复核 157 个文件 0 不符 0 缺失。
- 独立性由测试守住：`material_reference.py` / `fixtures.py` / `build_expectations.py`
  一旦 import `prism`/`f3d`/`vtk`/`assimp`/`trimesh`，检查直接失败。
- 交叉核对：材质计数用**另一条代码路径**（JSON 直读 / `newmtl` 正则 / 二进制节点名
  上界计数）复算，17 个样本全部一致。

### 真实旅程每格式结果

| 格式 | 解析（材质数/使用点/缺图/未使用） | 局部编辑→撤销→保存重开→搬迁→导出 |
| --- | --- | --- |
| glTF | 通过（6 个真实样本 + 14 个夹具逐项比对） | 受阻：服务无提交 |
| GLB | 通过（内嵌贴图、ORM、法线） | 受阻 |
| OBJ+MTL | 通过（多材质、同名、缺图、UTF-16、悬空引用） | 受阻 |
| FBX | 通过（二进制 + ASCII，含两材质共用四节点、7 个 PBR 贴图槽、绝对作者路径；损坏容器正确报错） | 受阻 |

「受阻」的原因统一是 `MultiMaterialService.capabilities()` 全 False、
`inspect/preview/commit/cancel` 抛 `ModelFeatureUnavailable`。

### 持久化 / 故障 / 原件哈希证据

- 原件哈希不变：把所有夹具与上游样本按字节哈希，跑完 20 个模型的解析后重算，
  0 个文件变化 → `artifacts/model-multimaterial/d1/original-integrity.json`。
- 载荷往返无损：`catalog_to_payload` → JSON 文本 → `catalog_from_payload`，对每个
  夹具逐字段核对定义、使用点、贴图（槽/资源/通道/UV 套/色彩空间/法线约定）与
  `unknown_json`，18 项全通过 → `d1/payload-round-trip-*.json`。
  这**只**证明载荷形状无损，不证明工程保存会调用它，更不证明重开。
- 故障矩阵 12 项全部 `blocked`（无事务可中断）→ `d2/fault-matrix-status.json`。

### 高级效果分别结论

全部 `blocked`/未测（无原生局部材质端口），状态文件 `d2/effects-status.json`。
可核对的只有**源文件层面**的分辨：`gltf_transmission_volume` 夹具把
「透射 / 体积 / 普通 alpha 混合」做成三个不同材质，期望集里明确记录
`PlainBlend` 不得被算作散射；置换记为未覆盖（无真实置换样本），
bump 与 normal 在 `obj_bump_vs_normal` 里分成两个材质并记录
`normal_convention=height` 与 `-bm 0.5`。

### 性能原始样本 / 分位 / 峰值 / 趋势

- 预算先冻结：`benchmarks/multimaterial_budget.md`（P-1 暖选≤100ms P95、
  P-2 准备到画面≤100ms P95、P-3 GUI 最长停顿≤50ms，另有候选 C-1 解析：
  冷≤250ms、暖≤50ms P95，需主对话确认）。冻结时还没有任何 P-1/P-2/P-3 数字。
- 已测（解析层，20 次/样本，wall clock）：3 材质夹具冷 p50 23.5ms / 暖 p50 5.8ms /
  暖 p95 6.9ms；四部位共享夹具冷 11.4ms / 暖 2.7ms；DamagedHelmet.glb 冷 7.4ms /
  暖 p95 7.7ms；AlphaBlendModeTest.gltf（6 材质 5 贴图）暖 p95 22.9ms；
  Poly Haven 二进制 FBX 暖 p95 7.5ms → `d5/parse-baseline.json`。
  **口径**：这是后台解析成本，不是用户等待；C-1 现状达标。
- 未测：选材质/草稿/原生安装/首帧/提交/封面/保存/退出（服务未接入），
  库 100/1000/1 万/10 万（十万级真实数据不可用），冷准备与 50 次往返趋势、
  内存/句柄/临时文件曲线、WAL 与工程增长。
- 日志开销（D6，两个口径都记）：事件构造 4.6–4.8 µs/事件，off 与 normal 相同，
  因为没有活动 trace 写入者 → 带写入端的端到端开销**未测**，
  见 `d6/logging-overhead.json` 的 `verdict`。

### 缺陷列表与反证条件

- DEF-1（中，未关闭）：`model_library.events` 没有材质级身份字段
  （`material_id`/`binding_id`/`catalog_revision`/`state_revision`/`draft_generation`），
  一次材质编辑只能定位到模型，无法定位到材质与代次，也无法说明「旧结果为何被拒」。
  反证条件见 `artifacts/model-multimaterial/d6/DEFECTS.md`。
- DEF-2/3/4（高，会话期间先复现、已被实现方修复）：ASCII FBX 材质名丢失、
  3ds Max PBR FBX 贴图槽全丢、损坏 FBX 容器被读成「0 材质」。现在都有回归检查守着。
- 待确认约定 3 条（FBX 取 `FileName` 还是 `RelativeFilename`、OBJ 无 `usemtl`
  的面块记为无材质使用点、缺材质库与缺材质定义要分开提示）。
- 环境发现：`f3d` 在 `PyQt6` 之后导入失败（可复现），影响原生路线的实验与验收
  脚本，不影响产品主进程。

### 未测 / 跳过 / 受阻，不可发布项

- 未测：局部编辑、共享材质独立、库替换（表面/完整）、撤销重做、保存重开、
  搬迁、清缓存、源库删除后重开、封面、拾取高亮、SSS/置换/线框、
  十万级性能、带写入端的日志开销。
- 受阻原因统一为：服务能力全关 + 无原生局部材质端口 + 十万级真实数据缺失。
- 不可发布项：以上任何一项都**不能**写进发布说明；当前可发布的只有解析层结论。

### 原始命令、日志、诊断包路径

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.venv/Scripts/python.exe tests/model_library/multimaterial/acceptance/capture_baseline.py
.venv/Scripts/python.exe tests/model_library/multimaterial/acceptance/build_expectations.py
.venv/Scripts/python.exe -m pytest -q tests/model_library/multimaterial/acceptance
.venv/Scripts/python.exe benchmarks/multimaterial_bench.py --repeat 20
```

- D0：`artifacts/model-multimaterial/d0/`（`SUMMARY.md`、`environment.json`、
  `machine.txt`、`source-hashes.json`、`sample-integrity.json`、
  `baseline-framework-handoff.txt`、`baseline-model-library-suite.txt`、`baseline-runs.json`）
- D1：`artifacts/model-multimaterial/d1/`（`EXPECTATIONS.json`、`REPORT.md`、
  `fixtures/`、`product-comparison-*.json`、`original-integrity.json`、
  `payload-round-trip-*.json`）
- D2/D4：`artifacts/model-multimaterial/d2/`（`journey-status.json`、
  `fault-matrix-status.json`、`effects-status.json`）
- D5：`artifacts/model-multimaterial/d5/`（`performance-status.json`、`parse-baseline.json`）
  + `benchmarks/multimaterial_budget.md`、`benchmarks/multimaterial_bench.py`
- D6：`artifacts/model-multimaterial/d6/`（`logging-contract.json`、
  `logging-overhead.json`、`acceptance-run.txt`、`DEFECTS.md`）
- 套件说明：`tests/model_library/multimaterial/acceptance/README.md`

既有基线（与本次新增严格分开）：`framework-handoff` 52 passed（13.49s）、
`model-library-suite`（`--ignore=tests/model_library/multimaterial`）
273 passed / 1 skipped（120.77s）。新增验收套件本身：97 passed / 1 skipped /
1 failed（唯一失败项即 DEF-1）。

### 结论能证明到哪一层

能证明：四种格式的文件字节 → 材质清单（定义/使用点/贴图槽/通道/UV 套/色彩空间/
缺图/未使用/悬空/内嵌/绝对路径）与独立期望逐项一致；解析不改原件；清单载荷
往返无损；损坏文件被报为损坏；性能门槛已冻结且解析层在门槛内。

不能证明：任何局部编辑行为、任何持久化与撤销、任何原生显示、任何高级效果、
任何窗口期性能。这些不是「小瑕疵」，而是本轮**没有实现**，因此多材质编辑
整体仍不可发布。

---

## 执行记录（2026-10-27，AI 4 第二轮）

按主对话回复执行：保留 D0/D1 旧基线；D2—D4、完整交互性能维持受阻/未测；
DEF-1 拆成「字段声明与传递」与「真实编辑链关联」；补六类独立反例；
FBX 两种路径声明都保留；474 条样本只按"声明路径下不存在"表述；
环境采集追加干净子进程先 import f3d 的结果；生产端口未接通的项仍记未测，
不用替身补成通过。

```text
D0 冻结            通过（重采集；f3d 补干净子进程探针，原失败记录保留）
D1 期望集          通过（24 个结构夹具 + 17 个真实样本，声明/读取 0 项不符）
D1 产品门          44 项结构化比对通过；反例新增 6 类，其中 1 类暴露 DEF-5
D2 真实旅程        受阻（服务能力全关）；5 条序列反例已写成规格 + blocked
D3 故障/持久化     受阻；原件哈希不变、载荷往返无损仍通过
D4 高级效果        未测
D5 性能            解析层已测；合成索引单独成节；暖选/安装等仍 blocked
D6 日志             DEF-1a 关闭（声明+传递+进入记录路径）；DEF-1b 未测
```

### DEF-1 拆分结果

- **DEF-1a（已关闭）**：`IDENTITY_FIELDS` 与导出的
  `model_event_contract()['material_identity_fields']` 均含
  `material_id`/`binding_id`/`catalog_revision`/`state_revision`/
  `draft_generation`/`gesture_id`；`material_event_schema=model_material_event/1`；
  14 个 `material.*` 事件各自声明必需字段且含身份根字段；在
  `model_identity(...)` 上下文里构造事件，五个材质字段 + `gesture_id` 全部进入
  载荷、`draft_generation` 保留、不再报缺字段；`record_model_event` 让
  `recorded` +1、`failures` 不变。
  证据：`d6/logging-contract.json`、`d6/logging-identity-carriage.json`、
  `d6/logging-counter.json`。
- **DEF-1b（未关闭）**：真实编辑链的关联与落盘依赖提交/安装接通，
  当前 skip 并写明反证条件（`d6/def1b-status.json`）。
  **不因为字段测试变绿就关闭整项。**

### 本轮新增的独立反例

| 反例 | 夹具/样本 | 结论 |
| --- | --- | --- |
| 非 UTF-8 编码、替换字符碰撞 | `obj_encoding_collision`（`Wood\xff` / `Wood\xfe` / `Wood\xef\xbf\xbd`） | 通过：产品给出 3 个互不相同的 `material_id`（含字节摘要），3 个使用点分别绑定；独立读取器按原始字节给出同一结论 |
| 未指定与悬空材质，使用点不能消失 | `obj_no_usemtl`、`obj_unassigned_before_usemtl`、`obj_dangling_material`、`obj_missing_mtl` | 通过：四类情形的（定义数, 使用点数, 无材质使用点数, 未使用定义数）签名互不相同，使用点全部保留 |
| 缺 MTL 必须指名 | `obj_missing_mtl` | 通过：产品 notes 指明 `materials.mtl` |
| 同目标下预览乱序、取消后结果晚到、A 提交期间编辑 B、点选同步、源库变化后用快照 | 无夹具（需要调度与事务） | 受阻：5 条规格已写入 `d2/sequencing-status.json`，逐条含"必须成立""如何反证""观测点""被什么挡住" |
| FBX 两种路径声明均保留 | `polyhaven_barrel_01.fbx` | **失败（DEF-5）**：产品只保留 `RelativeFilename`，`FileName` 的绝对路径与"采用哪个字段"都丢失 |

### 期望集侧的修正（凭原始证据，不为迁就实现）

1. **材质身份改为按原始字节**：`MaterialDefinition` 新增
   `name_bytes_sha256` / `name_bytes_hex`；OBJ 的 `usemtl` 与 MTL 的 `newmtl`
   按原始字节匹配，显示名只用于展示。理由：`b'Wood\xff'` 与 `b'Wood\xfe'`
   在 UTF-8 替换解码下同名，按显示名建立身份会把两个材质合并。
2. **四类"没有材质"分开建模**：`assignment_summary()` 输出
   `has_material_statement` / `sites_without_material` /
   `dangling_sites` / `missing_material_libraries`，不再混成一类。
3. **缺 `.mtl` 与缺贴图分开比对**：`unresolved_resources` 表达贴图资源，
   缺材质库另有断言（`test_missing_mtl_is_reported_as_a_missing_library`）。
4. **FBX 双路径记录**：参考读取器同时保留 `FileName` 与 `RelativeFilename`、
   记录采用的字段并标记歧义。
5. **产品门的口径对齐（不是放宽期望）**：产品把"未指定"与"悬空"都记为
   `original_material_id=None`，比对时用两者并集；拆分仍由独立反例断言。
6. 产品门不再手写用例清单，改为从期望集自动取全部用例，并新增
   `test_gate_covers_every_expectation_case` 防止新夹具漏检。

### 环境采集（f3d）

- 原记录保留：本进程先 `import PyQt6` 再 `import f3d` → `ImportError:
  DLL load failed while importing pyf3d`。
- 新增干净子进程探测（`-I -c`，先 `import f3d`）：`import f3d` 成功，
  其后 `from PyQt6.QtCore import QT_VERSION_STR` 也成功（6.7.1）→ 失败由
  **导入顺序**决定，不是"原生不可用"。
- 附带发现：`f3d.Engine` 在 4.0.0 的 Python 绑定里没有 `getLibInfo`
  （AttributeError），F3D 只能按发行版号记录。
- 证据：`d0/environment.json` 的 `f3d.import` 与 `f3d.clean_child`。

### 474 条样本的表述修正

`stress_project.verdict` 改为：清单声明的 474 条路径在本机均不存在
（verified=0、mismatch=0）；**不能**推断已被删除，也**不能**推断工程内没有原件；
可确认的只有"按声明路径取不到字节"，故不能用于"原件哈希不变"的核对。
十万级真实混合数据继续记未测。

### 性能（D5）

- 真实解析层（12 次/样本）：3 材质夹具暖 p50 5.7ms / p95 6.3ms；
  四部位共享 2.7ms；DamagedHelmet.glb 7.6ms；AlphaBlendModeTest.gltf 21.5ms；
  Poly Haven 二进制 FBX 7.5ms（`d5/parse-baseline.json`）。
- **合成索引单独一节**（`basis=synthetic-index`，明确写"不能代替真实混合数据"）：
  1/10/100/1000 材质，载荷 799B / 6.6KB / 64.9KB / 655KB，
  单材质约 649–799 B，往返 p50 0.05 / 0.22 / 2.08 / 22.4 ms。
- 暖选材质、草稿准备、原生安装、首帧、提交、封面、保存、退出：全部 blocked，
  原因逐条写在 `d5/performance-status.json`；预算仍以
  `benchmarks/multimaterial_budget.md` 的冻结稿为准。

### 本轮计数与证据路径

- 新增验收套件：**120 passed / 7 skipped / 1 failed**（唯一失败 = DEF-5）。
  7 个 skip 是：1 个按设计不可读的夹具往返、5 条序列反例、DEF-1b。
- 既有基线（与新增分开）：`framework-handoff` 52 passed（13.33s）、
  `model-library-suite`（`--ignore=tests/model_library/multimaterial`）
  276 passed / 1 skipped（118.66s）。
- 新增/更新证据：`d1/encoding-collision.json`、`d1/no-material-*.json`、
  `d1/no-material-signatures.json`、`d1/fbx-path-declarations.json`、
  `d1/product-comparison-*.json`（44 项）、`d2/sequencing-status.json`、
  `d5/parse-baseline.json`（含 synthetic 一节）、
  `d6/{logging-contract,logging-identity-carriage,logging-counter,def1b-status}.json`、
  `d6/DEFECTS.md`、`d6/acceptance-run.txt`。

### 仍然不能证明的事

任何局部编辑、库替换、撤销重做、保存重开、搬迁、原生局部显示、
SSS/置换/线框、十万级性能、带写入端的日志开销。这些仍属**未实现或未接通**，
不能用替身、截图或单测数量替代。

---

## 执行记录（2026-10-27，AI 4 第三轮）

按主对话第二轮回复的五条修正执行。**未修改业务实现**；`prism/` 下所有文件
本轮零改动。实现方在同期继续改 `inspectors/fbx.py` 与 `catalog.py`，判定所依据的
快照与哈希写在本节与 `d6/DEFECTS.md`。

```text
D0 冻结            F3D 版本采集已修正（get_lib_info 可用）；导入顺序失败记录保留
D1 期望集          27 个结构夹具 + 17 个真实样本；声明/读取 0 项不符
D1 反例            编码碰撞、四类无材质、混合文件、FBX 声明/候选/采用 全部有独立断言
D1 产品门          128 passed / 8 skipped / 1 failed（失败 = DEF-5 剩余部分）
D2/D3/D4           仍受阻/未测（服务能力全关）
D5                 真实解析层已测；合成**清单编解码**单列（已改名）
D6                 DEF-1a 已关闭；DEF-1b 未测
```

### 1. DEF-5 按当前事实重写

- **上轮表述作废**：不再写"另一条路径完全丢失"。产品现在把两条声明都写进
  `TextureUse.unknown_json.declared_paths`（`field` + `path`），已复核。
- 按四问逐项核对（`declared 来自哪个对象/字段` / `各候选解析结果` /
  `采用哪个资源及理由` / `全缺失时如何表示`）：
  - 声明集合：**有**（字段级；缺对象与对象 id）
  - 各候选解析结果：**缺**（只有布尔 `missing`/`absolute` 与一条 `source_locator`）
  - 采用结果：**有**（`resource_id`；缺失时为空）
  - 采用理由：**缺**
  - 候选冲突（两条都存在且内容不同）：**缺标记**
  - 全缺失时 `selected` 为空：**通过**（`resource_id=''`，不把首选字符串当已采用）
- 期望侧同步升级为五件套：`declarations[]`（对象 kind/id/name + 字段 + 值 +
  absolute）、`candidates[]`（value/resolved/exists/absolute/digest）、
  `selected_resource_id`/`selected_path`、`selection_reason`、`candidate_conflict`。
- 判定快照：`inspectors/fbx.py` `7b044ede14eb4ae9`（41943 B）、
  `catalog.py` `efee13ad65fca41a`（21433 B）。
- 证据：`d1/fbx-path-declarations.json`、`d1/fbx-path-conflict.json`、
  `d1/fbx-candidates-missing.json`。

### 2. D5 名称修正

`measure_synthetic_index` → `measure_synthetic_catalog_codec`，
`basis` → `synthetic-catalog-codec`，并写明它只测
`catalog_to_payload → JSON 编解码 → catalog_from_payload`，
`is_storage_measurement=False`，`does_not_cover` 明确列出
「数据库索引、查询、写入、恢复、WAL、单槽读写放大、工程增长、十万级索引性能」。
**原始数字保留不变**（1/10/100/1000 材质：799 B / 6.6 KB / 64.9 KB / 655 KB；
往返 p50 0.07 / 0.43 / 3.68 / 21.68 ms）。预算冻结稿同步改正。

### 3. F3D 版本采集修正

- Python 接口是 `f3d.Engine.get_lib_info()`，返回 `LibInformation` 对象
  （snake_case 属性，不是 dict）。`getLibInfo` 只是 C++ 名字，不存在 ——
  **不能**据此说"只能记录安装包版本"，上轮结论作废。
- 干净子进程（先 import f3d）实测：
  `version_full = 4.0.0`、`vtk_version = 9.7.1-1746-gdbb73e9cc8 (date: 20261016)`、
  `build_date = 2026-10-20 22:36:14`、`compiler = MSVC 19.44.35229.0`、
  `license = BSD-3-Clause`、`modules = {FFmpeg, ImGui, OpenEXR, WebP: true,
  Raytracing: false}`。
- 原失败记录保留：本进程先 `import PyQt6` 再 `import f3d` 仍
  `ImportError: DLL load failed`；其后 `import PyQt6` 在干净子进程里成功（6.7.1）。
- 证据：`d0/environment.json` 的 `f3d.import`（保留）、
  `f3d.clean_child.lib_info`、`f3d.version_source`。

### 4. 未指定/悬空的验收边界收紧

- 新增混合夹具 `obj_mixed_assignment`：同一文件里同时有
  未指定（usemtl 之前的面块）、悬空（`usemtl Gone`）、正常绑定（`usemtl Present`）。
- **能签的**：使用点不丢（3 个 site，1 绑定 + 2 无材质）——
  `test_mixed_assignment_keeps_every_site`。
  四类情形结构签名互不相同 —— `test_the_four_no_material_situations_stay_distinct`
  （该测试自己写明"只证明形态不同，不证明同一文件内逐点区分"）。
- **不能签的**：逐点状态。产品把两者都记为 `original_material_id=None`，
  `BindingSite` 没有可机读状态字段 →
  `test_mixed_assignment_states_per_binding` **skip**，原因写明"待契约贯通"，
  记录在 `d1/mixed-assignment-states.json` 与 DEF-6。
  **没有用并集比较把这一项签成通过。**

### 5. 新增/保留的检查

- 新增夹具：`obj_mixed_assignment`、`fbx_path_conflict`（派生自真实 ASCII FBX，
  只改路径字符串）、`fbx_candidates_all_missing`（同上）。
- 保留：编码碰撞、缺 MTL 指名、期望集自动取用例（`test_gate_covers_every_expectation_case`）。
- 产品门用例从 41 增至 44（新增 3 个夹具自动纳入）。

### 本轮计数与证据

- 新增验收套件：**128 passed / 8 skipped / 1 failed**
  （失败 = DEF-5 的"候选冲突不可见"；8 个 skip = 5 条序列反例 + DEF-1b +
  1 个按设计不可读夹具往返 + 1 条待契约贯通）。
- 既有基线（与新增分开）：`framework-handoff` 52 passed、
  `model-library-suite` 276 passed / 1 skipped。
- 新证据：`d1/mixed-assignment-sites.json`、`d1/mixed-assignment-states.json`、
  `d1/fbx-path-declarations.json`、`d1/fbx-path-conflict.json`、
  `d1/fbx-candidates-missing.json`、`d5/parse-baseline.json`（改名后的 synthetic 节）、
  `d0/environment.json`（F3D 版本）、`d6/DEFECTS.md`、`d6/acceptance-run.txt`。

### 待验清单（下一轮按此推进，未接通项继续明确受阻）

| 项 | 现在 | 解锁条件 |
| --- | --- | --- |
| DEF-5 剩余（采用理由 + 候选冲突标记） | 失败（1 项） | `TextureUse` 增加候选/采用三元组契约 |
| DEF-6 逐点状态（未指定 vs 悬空） | skip（待契约贯通） | `BindingSite` 提供可机读状态 |
| DEF-1b 编辑链日志关联 | skip | 提交/安装接通 + 活动 trace writer |
| 5 条序列反例（乱序/取消晚到/提交交错/选择同步/快照） | skip | 服务调度与事务接通 |
| D2 真实旅程、D3 故障矩阵、D4 高级效果 | 受阻 | 服务能力位打开 + 原生局部材质端口 |
| 十万级真实混合集性能 | 未测 | 真实混合数据可用（当前声明路径下取不到字节） |

### 仍然不能证明的事

任何局部编辑、库替换、撤销重做、保存重开、搬迁、原生局部显示、
SSS/置换/线框、十万级性能、带写入端的日志开销、存储布局与索引性能。
这些仍属**未实现或未接通**，本轮没有用替身、截图或单测数量替代。
