# AI 1 · A0 基线与归属（模型多材质数据与事务）

日期：2026-10-27。角色：AI 1（数据与事务）。本文件只记录**已核实**的事实、真实命令与原始日志路径；未做的项写“未做”。

源码目录 `C:/Users/admin/Desktop/Prism-main/Prism-main`，Python `.venv/Scripts/python.exe`（3.12），离屏 `QT_QPA_PLATFORM=offscreen`，当前无 Git。证据目录 `artifacts/model-multimaterial-ai1-20261027/`。

## 1. 接手时读过的文件与 SHA-256

| 文件 | SHA-256（接手时快照） |
| --- | --- |
| docs/MODEL_MULTI_MATERIAL_DESIGN_20261027.md | 868E3991A02332071E1BAC0A08994383D843418F45F375C9227FF604C74BB8BB |
| prism/model_library/contracts.py | 0753DD06B1265DAF459A5EEF14BE676EC24E20679BE56D75220745C9E9BCDD59 |
| prism/model_library/AGENTS.md | A9F73B51F7C7B4714233E2278650E58C07BFC6C602D1B9D93168D63C7EEABD28 |
| prism/model_library/appearance.py | 4CB42EDA0121A5CB49C16D6765F9A170565D006EE5E673BA696EE6670B880AD1 |
| prism/model_library/service.py | B4BBD2CBF3B799ED24BA40F24D6E16DB73AF57BE27BAF186B91F7F4BB29C8435 |
| prism/model_library/project_storage.py | 6DBECD0CFA2ECF74DA76B8634E87B56B089F1AFA1D303ACEC08D0ECD12A9D470 |
| prism/model_library/repository.py | A90BE86116E2567E015EC6E171EA638DC61C4AE3C2D7CA0CC63CA2F398F92DB0 |
| prism/model_library/bundles.py | A7A885E8E28C5DD5D47CF744EB730897D8E132398B3AB4DDB1537E86A7894780 |
| prism/model_library/exporter.py | 12593FE9301EFAD147FFC3DACBC3A7CB81573C09308081F47FB4BF7885B8DA17 |
| prism/model_library/multimaterial/contracts.py | 22FDEF947B1521777213804320BEECC763041182F6D6A99A9B97CB217107BDF3 |

共享 `contracts.py`/`ports.py`/`multimaterial/contracts.py` 未修改。`docs/MODEL_MULTI_MATERIAL_FRAMEWORK_20261027.md` 为共同契约，已通读。

## 2. 回归基线（原始输出在 artifacts/）

接手时（未做任何修改）：

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.venv/Scripts/python.exe -m pytest -q tests/model_library test_model_library_scaffold.py ...
```

- 框架任务书点名的四条：`52 passed in 8.95s`。
- 全 `tests/model_library` + `tests/test_model_library_scaffold.py`：`277 passed, 1 skipped in 115.03s`。

本人改动后（同一命令，见 `artifacts/model-multimaterial-ai1-20261027/model-library-regression.txt`）：
`475 passed, 2 skipped, 1 failed in 138.07s`。数量增长来自并行角色新增的用例（AI 3 UI、AI 4 验收），唯一失败是 AI 4 的
`acceptance/test_d6_logging.py::test_material_identity_fields_are_declared`，原因见 §5 的归还项，不属于解析层。

A1 解析层专项（`artifacts/model-multimaterial-ai1-20261027/a1-catalog-tests.txt`）与 AI 4 的独立期望对照
（`d1-acceptance-against-product.txt`）：`38 passed`。

## 3. 现有链路的真实归属（谁在做、在哪一行）

不是类图，是当前代码里逐条核实的调用链。

### 3.1 附件真实提交（原件/贴图字节）

- 写字节：`prism/model_library/project_storage.py:584 ModelResourceStore`（`put_file`/`put_batch`）：单文件工程进 `attachments` 表（incremental blob），目录工程进内容寻址对象库，经 `prism.storage.managed.open_original` 读。**已有**。
- 模型记录与字节同事务：`prism/model_library/importer.py:1025 _commit` → `repository.py:245 insert_in(connection, payload)`，由调用方连接提交。**已有**。
- 贴图（旧单份外观）提交：`service.py:238 attach_texture` → `resources.put_file` + `update_appearance`。**已有**，且只服务旧 `ModelAppearance`。
- 多材质的“暂存 → 校验 → 原子提交”**不存在**（A3 未做）。

### 3.2 附件索引注册（保存时哪些行会被保留）

- `project_storage.py:512 register_model_attachments(scene, payload)` 把模型原件登记进 `attachments._meta`，只遍历 `payload['files']` + `payload['appearance']['bindings']`（**顶层**）。
- 调用点：`prism/model_library/composition.py:230-231`（导入完成回调）。**只有这一处**。
- 结论：**新增的嵌套材质贴图不会被登记**，下次保存的清理会把它当孤儿删掉。这是我必须补的第一处（A3）。

### 3.3 清理（保存时删除孤儿）

- 全量保存：`prism/fileio/sql.py:1773` `keep |= _model_attachment_ids(scene.model_records)`（`sql.py:1934` 转调 `project_storage.py:553 model_attachment_ids`）。
- `model_attachment_ids` 同样只遍历 `files` + `appearance.bindings`，**不看嵌套材质**。
- 结论：修好 `model_attachment_ids` 的遍历即可让全量清理正确；局部保存另外需要 §3.4 的 `required`。

### 3.4 局部保存（页级保存）

- 入口：`prism/partial_project.py:748-784`。它只对 `material_records` 收集 `references`/`required`
  （`partial_project.py:756-761`），对 `model_records` 只调 `self._domains.capture(...)`。
- `partial_project.py:807-808`：提交后 `self._valid_domain_attachments.update(model_attachment_ids(after))` —— 只把模型附件标为“已知有效”，**没有**把新附件加进 `required`。
- `partial_save.py:422 needed = tuple(sorted(set(required_attachments)))` 才决定“写入前必须存在”的字节。
- 结论：局部保存下新增材质贴图仍然可能丢，必须补 `required`（A3 的补丁交主对话）。

### 3.5 另存为

- 无新代码：另存为走全量 `prism/fileio/sql.py` 写路径（§3.3），只要 `model_attachment_ids` 覆盖嵌套引用即可自动成立。
- 未实测：本轮**没有**跑另存为迁移，见 §5。

### 3.6 目录工程转换

- `prism/storage/migration.py:181 convert_legacy(...)`，`migration.py:224` 对 GENERATED/hidden 列直接拒绝。
- 所以新表/新列不能使用生成列；模型域的 `model_records(id, payload)` + 表达式索引（`project_storage.py:94-126`）已满足。
- 结论：A2/A3 的磁盘 schema 必须以 payload 内 JSON 或普通列实现，禁止 GENERATED。RFC 已写死这条。

### 3.7 历史保留（撤销/重做）

- 现状：模型域**没有**命令栈。`prism/model_library/service.py` 通过 `on_commit(before, after)` 交给上层（`composition.py:213 _commit`），撤销由工程历史负责。
- 多材质命令（替换/独立/恢复）尚不存在，A3 未做。

## 4. A1 已交付（解析层）

新增（全部 Qt-free、F3D-free、不写工程）：

- `prism/model_library/multimaterial/catalog.py`：目录构建器、资源身份（`sha256:`）、槽位词表（9 个可编辑 + 7 个源角色）、`unresolved/absolute/remote/embedded_resources`、payload 编解码（未知字段保留）。
- `prism/model_library/multimaterial/inspectors/`：`gltf.py`（含 GLB）、`obj.py`（含 MTL）、`fbx.py`（自带二进制/ASCII 读取器，读整型数组）、`locator.py`、`__init__.py`（派发 + 每种格式的已知限制）。
- 测试：`tests/model_library/multimaterial/test_data_catalog_{gltf,obj,fbx,codec}.py`，共 41 条，期望全部手写自 fixture 原文。

已证明（有日志）：四格式定义/使用点/贴图/数值可追溯；四腿共享一份材质算 4 个使用点；mesh 多实例是多个使用点；bump 不冒充 normal、map_Ks 不冒充 roughness；缺图与“作者绝对路径”分开报；损坏容器报错而不是“无材质”。

## 5. 未做 / 受阻 / 未测（不得算通过）

- A2 工作材质与磁盘 schema 实验：**未做**（RFC 已交，等主对话冻结）。
- A3 原子提交、暂存、历史、资源回收：**未做**。
- A4 异步门面与日志：**未做**。`MultiMaterialService` 仍是默认不可用（框架安全门测试仍通过）。
- `model_library/events.py` 的 `IDENTITY_FIELDS` 缺 `material_id/binding_id/catalog_revision/state_revision/draft_generation`（AI 4 的 D6 用例因此失败）。`events.py` 不在我的文件所有权内 → 以最小补丁形式交主对话（见 RFC §7），不擅自改。
- 另存为、目录工程转换、清缓存后重开：**未实测**（属于 A2/A3 的证明项）。
- 真实 GBK 编码的 OBJ/MTL 名字：为保证与独立参考逐字一致，采用 UTF-16 BOM → 否则 UTF-8(replace)。旧代码会先试 GBK，现在不会；非 UTF-8 的材质名会显示 U+FFFD。这是**已披露的取舍**，若主对话要求兼容 GBK，需要 AI 4 的参考读取器同步改，否则两边永远不一致。
