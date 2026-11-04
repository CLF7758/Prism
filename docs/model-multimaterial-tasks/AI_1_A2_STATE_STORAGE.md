# AI 1 · A2：状态模型、校验与存储对照实验

日期：2026-10-27。这一份只讲 A2：状态怎么表达、怎么校验、磁盘布局用哪套。渲染路线不影响这里的结论（按主对话指示，未等渲染）。

## 1. 交付的代码（Qt-free、F3D-free、不写工程）

| 文件 | 职责 |
| --- | --- |
| `prism/model_library/multimaterial/vocabulary.py` | 槽位唯一来源：`STANDARD_SLOTS`(9) / `SOURCE_ROLES`(7) / `editable_slots(verified)` / `slot_policy` |
| `prism/model_library/multimaterial/state_store.py` | 状态编解码（未知字段对称往返）、三态解析 `effective_texture`/`effective_properties`、引用根 `collect_references`、`orphaned_resources`、`state_signature` |
| `prism/model_library/multimaterial/validation.py` | 目录/状态/请求校验 + 错误码、`request_signature` 幂等、`plan_make_unique` 复制规则 |
| `prism/model_library/multimaterial/catalog.py` | 新增 `binding_resolutions`/`bindings_by_resolution`/`dangling_bindings`，悬空使用点保留 |
| `prism/model_library/multimaterial/inspectors/text.py` | 字节级文本解析与编码判定（A1 修正） |

测试：`tests/model_library/multimaterial/test_data_state.py`（26 条）、`test_data_catalog_text.py`（10 条）、`test_data_shared_patches.py`（5 通过 / 4 待补丁 skip）。

## 2. 三态与"来源可解释"

固定顺序（实现即 `state_store.effective_texture`）：

1. 本地 `set` → 用本地值（来源 `working`）；
2. 本地 `clear` → 没有贴图，**不回落到基础**（来源 `cleared`）；
3. `inherit` 或未出现 → 基础：受管库快照（`snapshot`）优先，其次源定义（`source`）；
4. 都没有 → `none`。

数值同理：缺失=继承 / `{"value":0.0}`=真的 0 / `{"disabled":true}`=显式关闭，三者不可互换。
测试 `test_zero_and_disabled_are_different_from_absent`、`test_an_override_wins_and_a_clear_does_not_fall_back` 覆盖。

## 3. 校验（拒绝而不是"保存时修复"）

- 状态：工作材质 id 唯一、源材质必须存在、分配必须指向存在的使用点与材质、`set` 必须带 value 且资源在工程内、源角色与未验证槽位拒绝（`E_SLOT_READ_ONLY`）、悬空使用点不能分配。
- 请求：目标只从 `working_ids`/`binding_ids` 来；未声明参数拒绝；`keep`/`apply_defaults` 之类"语义开关"一律拒绝；幂等按内容哈希校验，同 id 不同内容 → `E_REQUEST_CONFLICT`。
- `make_unique`：同源目标一份副本、异源目标各自一份（复制数量 = 不同源材质数，不是目标数）。

## 4. 存储对照实验（原始数据）

脚本：`artifacts/model-multimaterial-ai1-20261027/a2_storage_experiment.py`
输出：`a2-storage-experiment.json`（全部 16 组原始数字）、`a2-storage-table.txt`（下表）

| layout | materials | 序列化 P50(ms) | 写入 P50 | 写入 P95 | 恢复(ms) | 存储(KB) | 每次修改增长(KB) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A 完整内嵌 | 1000 | 10.85 | 13.05 | 17.23 | 21.12 | 2208 | **2208** |
| **B 受管清单+稀疏覆盖** | 1000 | 0.019 | **0.052** | **0.087** | 0.318 | 1730 | **16** |
| B2 清单分离+全量 state | 1000 | 2.89 | 5.80 | 12.19 | 12.34 | 2208 | 500 |
| C 细粒度表 | 1000 | – | 0.018 | 0.034 | 3.48 | 1952 | 8 |

同表在 1 / 10 / 100 材质下的数字见 JSON；`recovered_materials/assignments` 均已校验读回一致。

**结论（建议，待主对话定案）**

- A 不可取：单槽修改让工程每次增长约 2.2MB（1000 材质）。
- **建议 B**：写入 P95 0.087ms、增长 16KB、恢复 0.32ms；不动表结构。
- C 作为超大规模升级路径保留（写入最省，恢复要读全量 3.5ms@1000，需迁移与保存路径接线）。
- B2 显示"分离清单"是次因，"稀疏覆盖"是主因。

**测量未覆盖（不得当成已测）**：UI 卡顿、原生渲染、多模型并发、磁盘满/断电、真实工程规模下的 WAL 峰值、冷缓存。REPEATS=50 只给到 P95，没有 P99。

## 5. 已知限制

- 恢复测量在 B 只恢复覆盖层，完整视图由 `catalog` 解析（解析成本计入 `effective_texture`，未单独计时）。
- 没有测 1 万材质（实验上限 1000），因为任务书要求的规模是 1/10/100/1000。
- 幂等记录的持久化位置只是建议（`mm_command_log`），未建表——属于 A3。
