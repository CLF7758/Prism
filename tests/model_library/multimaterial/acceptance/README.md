# 模型多材质：独立验收套件（AI 4）

这份套件只做一件事：**用不依赖被测实现的证据**判断多材质编辑做到了哪一层。
它不实现业务功能，不改 `prism/`，也不改其他角色的测试。

## 目录

```text
tests/model_library/multimaterial/acceptance/
├── material_reference.py      独立参考读取器：只读文件字节，不 import prism/f3d/vtk
├── fixtures.py                结构夹具生成器（glTF/GLB/OBJ+MTL + 确定性 PNG）
├── build_expectations.py      期望集生成器 + 声明/读取一致性核对
├── capture_baseline.py        D0 冻结：运行时、机器、源码哈希、样本清单、既有基线
├── conftest.py                会话夹具（期望集只在一个会话里构建一次）
├── test_d1_expectation_set.py D1：期望集自身是否独立、可复现、覆盖到位
├── test_d1_against_product.py D1 门：产品 catalogue 与独立期望逐项比对（用例从期望集自动取）
├── test_d1_negative_cases.py  反例：编码碰撞、四类"没有材质"、FBX 双路径声明
├── test_d2_payload_and_originals.py  原件哈希不变 + 载荷往返无损
├── test_d2_sequencing.py      序列反例规格：预览乱序/取消晚到/提交交错/选择同步/快照
├── test_gate_status.py        D2–D5 状态：哪些测不了、为什么、反证条件
├── test_d6_logging.py         D6：DEF-1a 契约与传递、隐私、开销（两个口径）、DEF-1b 状态
benchmarks/
├── multimaterial_budget.md    性能预算冻结稿（先定门槛后看结果）
└── multimaterial_bench.py     分阶段基准；没实现的阶段输出 blocked 与原因
artifacts/model-multimaterial/
├── d0/  d1/  d2/  d5/  d6/    原始输出、期望集、状态文件、缺陷证据
```

## 怎么跑

```powershell
$env:QT_QPA_PLATFORM='offscreen'

# 1. 冻结（环境、机器、源码哈希、样本核对、既有基线）
.venv/Scripts/python.exe tests/model_library/multimaterial/acceptance/capture_baseline.py

# 2. 生成 D1 独立期望集（失败即表示参考读取器或夹具声明有问题）
.venv/Scripts/python.exe tests/model_library/multimaterial/acceptance/build_expectations.py

# 3. 验收套件
.venv/Scripts/python.exe -m pytest -q tests/model_library/multimaterial/acceptance

# 4. 性能（未实现的阶段会写 blocked + 原因，不会编数字）
.venv/Scripts/python.exe benchmarks/multimaterial_bench.py --repeat 20
```

## 反例清单（第二轮）

这些不是happy path，每一条都对应一个"实现看起来做了、用户却会踩到"的错法：

| 反例 | 夹具 | 判据 |
| --- | --- | --- |
| 替换字符碰撞：三个名称解码后同名 | `obj_encoding_collision` | 3 个定义、3 个不同身份、3 个使用点分别绑定 |
| 文件从未声明材质 | `obj_no_usemtl` | 使用点保留（2 个无材质），定义计入"未使用" |
| `usemtl` 出现在第一个材质之前 | `obj_unassigned_before_usemtl` | 该面块记为无材质，另一个面块正常绑定 |
| `usemtl` 指向未定义材质 | `obj_dangling_material` | 使用点保留，并报出悬空引用 |
| `mtllib` 声明的文件缺失 | `obj_missing_mtl` | 0 个定义、使用点保留、指名缺失的 `materials.mtl` |
| FBX 同时声明绝对路径与相对路径 | 真实样本 `polyhaven_barrel_01.fbx` | 两条都要可见 + 记录采用哪一条（当前 FAIL：DEF-5） |
| 同一文件里同时有未指定、悬空、正常绑定 | `obj_mixed_assignment` | 三个 site 都在；**逐点状态**待契约提供状态字段（当前 skip，不按并集签通过） |
| FBX 两条声明都指向存在的不同文件 | `fbx_path_conflict`（派生自真实 ASCII FBX） | 冲突必须可见 + 采用结果要说明（当前失败：DEF-5） |
| FBX 两条声明都指向不存在的文件 | `fbx_candidates_all_missing`（同上） | `selected` 必须为空，不能把首选字符串当成已采用文件 |
| 预览乱序 / 取消晚到 / 提交交错 / 选择不同步 / 源库变更后用快照 | 需要调度与事务，无夹具 | 规格写在 `artifacts/model-multimaterial/d2/sequencing-status.json`，接通前 skip |

身份规则：**材质身份来自原始字节**（`name_bytes_sha256`），显示名只用于展示。
两个不同字节序列可以解码成同一个字符串，按显示名做身份会把两个材质合并。

贴图规则：一处贴图引用要能回答四件事 —— **谁声明的**（对象 + 字段）、
**每条声明解析到了什么**（存在与否、摘要）、**采用了哪个及为什么**、
以及**全部缺失时明确没有采用任何文件**。`selected` 为空不等于"用首选路径"。

性能口径：`benchmarks/multimaterial_bench.py` 的合成部分只测**清单编解码**
（`catalog_to_payload` → JSON 编解码 → `catalog_from_payload`），
名字是 `synthetic-catalog-codec`；它没有测数据库索引、查询、写入、恢复、WAL，
不能用来判断存储布局或十万级索引性能。

## 几条不能破的规矩

1. **期望不能来自被测实现。** `material_reference.py` / `fixtures.py` /
   `build_expectations.py` 一旦 import `prism`，`test_no_expectation_comes_from_the_implementation`
   就会失败。
2. **夹具声明与读取结果必须一致。** 声明是作者意图，读取是仪器读数；两者不符
   说明仪器坏了，先修仪器。
3. **解析失败不等于没有材质。** 坏文件必须报错，不能被读成「0 个材质」。
4. **没测就写没测。** `test_gate_status.py` 写出的状态文件里，每个未测项都带
   原因和反证条件；状态写成 blocked 时，服务能力位也必须是全 False。
5. **性能门槛先冻结。** 数字不得在看到结果之后再调（`benchmarks/multimaterial_budget.md`）。

## 目前能证明到哪一层

- 解析层（文件字节 → 材质清单）已由独立期望集逐项核对。
- D6 的日志**契约与传递**已复核通过（DEF-1a）；**真实编辑链关联**未测（DEF-1b）。
- FBX 双路径：**声明集合已由产品记录**（`declared_paths`）；
  仍缺"采用理由"与"候选冲突标记"——这是当前唯一失败的 D1 检查（DEF-5）。
- 同一文件内逐点区分「未指定 / 悬空」：待契约提供可机读状态（DEF-6），
  该项 skip，不按并集签通过。
- 序列类反例：5 条带反证条件的 blocked 规格。
- 局部编辑、库替换、撤销、保存、原生局部显示、SSS/置换/线框：**未测**，
  因为服务能力全关（`capabilities()` 全 False，`commit` 抛
  `ModelFeatureUnavailable`）。
- 详见交付报告与本目录 `artifacts/` 下的原始输出。
