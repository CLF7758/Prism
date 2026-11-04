# AI 1 · RFC v2：多材质持久化、命令参数与资源闭环

状态：**v2 修订稿，未冻结**（2026-10-27 第二版）。作者：AI 1。共享 `multimaterial/contracts.py` 未修改。
v1 已被主对话退回，本稿按退回意见逐条修订。**布局不冻结**：§3 只冻结逻辑边界，磁盘布局由 §4 的对照实验数据决定。

## 0. v2 相对 v1 的修订差异

| # | v1 | v2（本稿） | 依据 |
| --- | --- | --- | --- |
| 1 | 悬空材质引用不进 `binding_sites`，只藏进 `unknown_json` | **悬空引用保留在使用点里**，另带 `resolution: assigned/unassigned/dangling`；原始引用与诊断都保留 | 退回意见 §一；使用点计数必须完整 |
| 2 | 为对齐参考读取器，OBJ/MTL 统一 UTF-8 `replace` | 按字节解析：UTF-8/BOM/UTF-16 确定，其他为**不确定**并报告；**身份用原始字节**；坏字节不静默 | 退回意见 §二 |
| 3 | `EDITABLE_SLOTS`（9 个）当成"可编辑" | 拆成 `STANDARD_SLOTS`（9 个标准槽位）/ `SOURCE_ROLES`（7 个源角色）/ `editable_slots(verified)`（由后端能力决定）；唯一来源 `multimaterial/vocabulary.py` | 退回意见 §三 |
| 4 | 建议"完整 catalog 内嵌模型行"作为最终方案 | **不批准**。改为三方案对照实验（§4），数据在 `artifacts/.../a2-storage-experiment.json` | 退回意见 §四 |
| 5 | 只有 `model_attachment_ids` 补个小函数 | 完整的**资源引用根**表 + 生命周期 + 目录工程/SQLite 事务边界（§6），并区分"漏引用"与"永久保留" | 退回意见 §五 |
| 6 | `parameters_json` 可带 `working_ids`、`keep`、`apply_defaults` | 目标只从 `working_ids`/`binding_ids` 来；`keep` 由契约固定；`replace_full` 无开关；`restore_material` 不碰分配；`make_unique` 复制规则写死；幂等按内容校验 | 退回意见 §六 |
| 7 | 未区分"认领字段"与"回写路径" | 明确要求 `ModelRecord`/`encode`/`decode`/元数据修改/保存路径都能回写整段；只加 `OWNED_FIELDS` **不够**（已实测 `encode_record` 不对称） | 退回意见 §四/§七 |

已落地的代码：`vocabulary.py`、`validation.py`、`state_store.py`、`catalog.binding_resolutions()`、`inspectors/text.py`；
待裁决的契约与共享文件改动见 §7 与 `AI_1_SHARED_PATCHES.md`。

## 1. 存储位置（逻辑边界，冻结项）

- 数据仍在现有工程 SQLite 的 `model_records`，不新增数据库、不新增字节存储；原件/贴图继续走 `attachments` 与目录工程对象库。
- 不使用 GENERATED/隐藏列（`prism/storage/migration.py:224` 会拒绝目录工程转换）。
- 旧 `appearance` 原样保留，不自动散布到每个材质。
- **未知字段必须能往返**：state、catalog、材料、贴图四层各有 `unknown`，编码时把未知键展开回本层顶层，解码时把本层未知键收进 `unknown['payload']`（对称，已实现并有测试）。

## 2. 载荷 v2

```jsonc
"multimaterial": {
  "schema_version": 2,
  "catalog": {                      // = catalog.catalog_to_payload()
    "schema_version": 1, "revision": 3,
    "source_digest": "sha256:…", "parser_id": "gltf-2.0",
    "definitions": [ … ], "binding_sites": [ … ],
    "unresolved": [ … ],
    "unknown": { "binding_resolutions": {"<binding_id>": "assigned|unassigned|dangling"},
                 "dangling_bindings": { … }, … }
  },
  "state": {
    "state_revision": 7,
    "materials": [ …见 §2.1… ],
    "assignments": [ {"binding_id": "…", "working_id": "w-…"} ],
    "legacy_override": {},
    "unknown": {}
  },
  "resources": { "sha256:…": {"attachment_id": 41, "locator": "wood.png", "size": 1234} },
  "catalog_ref": { "digest": "sha256:…", "revision": 3 },   // §4 方案 B：受管清单引用
  "unknown": {}
}
```

### 2.1 工作材质与三态（与 `state_store` 实现一致）

```jsonc
{
  "working_id": "w-3f2a…", "source_material_id": "gltf:material:0",
  "display_name": "wood", "base_snapshot_ref": "sha256:…",
  "textures": [
    {"slot": "basecolor", "action": "set", "value": { …TextureUse… }},
    {"slot": "normal",    "action": "inherit"},
    {"slot": "emissive",  "action": "clear"}
  ],
  "properties": {"roughness": {"value": 0.0}, "metallic": {"disabled": true}},
  "provenance": { … }, "unknown": {}
}
```

解析顺序（固定，`state_store.effective_texture`，有测试）：

1. `set` → 本地覆盖；
2. `clear` → 没有贴图，**不得回落到基础**；
3. `inherit` 或缺失 → 基础：`base_snapshot_ref`（受管库快照）优先，否则 `source_material_id`（源定义）；
4. 都没有 → 该槽位没有贴图（不是黑图、不是 1×1）。

数值三态：缺失=继承；`{"value": x}`=设为 x（**0 就是 0**）；`{"disabled": true}`=显式关闭（**不等于 0**）。

## 3. 槽位词表（唯一来源 = `multimaterial/vocabulary.py`）

| 集合 | 内容 | 谁能用 |
| --- | --- | --- |
| `STANDARD_SLOTS` | basecolor、normal、roughness、metallic、ao、opacity、emissive、sss、displacement | 目录、UI、存储；**不等于可编辑** |
| `SOURCE_ROLES` | bump、specular、specular_exponent、reflection、ambient、decal、transparency | 只读展示；`set` 一分写就被拒（`E_SLOT_READ_ONLY`） |
| `editable_slots(verified)` | `STANDARD_SLOTS ∩ 后端已验证`，且剔除 `UNVERIFIED_SLOTS`（sss、displacement） | 命令校验；默认空 |

导入方向：`catalog`/`inspectors` → `vocabulary`；UI 与后端**不得**复制列表。`catalog.EDITABLE_SLOTS` 只是历史别名（= `STANDARD_SLOTS`）。
把词表搬进共享 `contracts.py` 与否由主对话裁决（§8.2）。

## 4. 布局：只冻结边界，布局按实验定（本节不冻结）

对照实验（真实 SQLite + WAL，单槽修改 ×50，1/10/100/1000 材质，每材质 4 个使用点）：

| layout | materials | 序列化 P50(ms) | 写入 P50(ms) | 写入 P95(ms) | 恢复(ms) | 存储(KB) | 每次修改增长(KB) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A 完整内嵌 | 1000 | 10.85 | 13.05 | 17.23 | 21.12 | 2208 | **2208** |
| **B 受管清单 + 稀疏覆盖** | 1000 | 0.019 | **0.052** | **0.087** | 0.318 | 1730 | **16** |
| B2 清单分离 + 全量 state | 1000 | 2.89 | 5.80 | 12.19 | 12.34 | 2208 | 500 |
| C 细粒度表 | 1000 | – | 0.018 | 0.034 | 3.48 | 1952 | 8 |

原始数据：`artifacts/model-multimaterial-ai1-20261027/a2-storage-experiment.json`（含 1/10/100 规模与 create/recover），表格 `a2-storage-table.txt`。

结论与建议（**待主对话定案**）：

- A（完整内嵌）**不可取**：每次单槽修改让工程增长一个完整 catalog（1000 材质约 2.2MB），恢复 21ms。
- **建议 B 为主方案**：catalog 受管一行 + 模型行只写稀疏覆盖；写入 P95 0.087ms，增长 16KB，恢复 0.32ms（恢复覆盖层，完整视图由 catalog 解析）。不需要改动表结构，风险最低。
- C 写入最省、但恢复要读全量（3.5ms@1000）且需要迁移与保存路径接线；作为**超大规模升级路径**保留，不作为本轮落地方案。
- B2 证明收益主要来自"稀疏覆盖"（分离清单只是次因）。

## 5. 命令参数与语义（修订）

目标只有一个入口：`MaterialEditRequest.working_ids` / `binding_ids`。`parameters_json` 里出现目标或未声明的键 → `E_PARAM_INVALID`（已实现 `validation.parse_parameters`）。

| operation | 目标 | 允许参数 | 语义（调用者不可改） |
| --- | --- | --- | --- |
| `patch` | `working_ids` | `slot`、`action`、`resource_id`/`value`、`texcoord`、`channels`、`color_space`、`uv_transform`、`normal_convention` | 三态编辑；源角色与未验证槽位拒绝 |
| `replace_surface` | `working_ids` | `library_material_id`、`library_revision`、`source_digest` | 只换 `SURFACE_REPLACEMENT_SLOTS`；**固定保留** `opacity/double_sided/sss/displacement` 与几何分配，**没有 `keep` 参数** |
| `replace_full` | `working_ids` | 同上 | 完整采用新定义 + 显式默认值；**没有 `apply_defaults` 参数**，不存在隐式合并 |
| `make_unique` | `binding_ids` | `name` | 同源目标共享**一份**副本；不同源**各自**一份（`validation.plan_make_unique`，有测试） |
| `restore_material` | `working_ids` | – | 只恢复这些材质的**内容**，不改任何分配 |
| `restore_model` | –（整模型） | – | 恢复原分配与原材质；不动标签/备注/目录 |

- 一个真实手势一个命令；滑条在 UI 聚合；文本按一次字段编辑。
- `request_id` 幂等**按内容**：`validation.request_signature()`（operation + 目标身份 + 解释后参数 + 目标列表，规范化 JSON 的 sha256）。同 id 同内容 → 返回原回执；同 id 不同内容 → `E_REQUEST_CONFLICT`。
- 幂等记录持久化在工程内（建议 `mm_command_log(request_id, signature, command_id, revision)`），因此**跨重启**有效；仅当该记录也被清理（回收站清空）后才可能重复执行，这一点必须在 UI 文案里写明。

## 6. 资源闭环（修订，取代 v1 §7.4 的单个辅助函数）

### 6.1 引用根（`state_store.collect_references`，已实现）

| 根 | 谁提供 | 说明 |
| --- | --- | --- |
| `catalog:{material_id}:{slot}` | 目录 | 源文件声明的贴图 |
| `working:{working_id}:{slot}` | 工作材质 `set` | 用户新图 |
| `snapshot:{working_id}` + `snapshot:{ref}:{slot}` | `base_snapshot_ref` 指向的受管库快照 | 快照本身与其资源都要保 |
| `inherited:{working_id}:{slot}` | 源定义（继承来源） | 与 catalog 根可能重合，去重后仍保留两条理由 |
| `history:{command_id}` | 撤销/重做栈（A3） | 每步命令的快照引用 |
| `lease:{lease_id}` | 预览租约（A4） | 暂存副本 |
| `trash:{entry_id}` | 回收站 | 已删除但可恢复的模型/材质 |
| `extra` | 调用方补充 | 不允许各角色私建第二份清单 |

`orphaned_resources(references, held)` 给出"项目持有但无人引用"的集合——清理**只**能删这个集合，且要走回收站策略。

### 6.2 生命周期（每个阶段谁负责）

1. **暂存**：新图先写 `resources` 暂存区（目录：`.tmp/multimaterial/{job_id}/`；单文件工程：内存 blob + 哈希），只做字节与哈希核对，不登记引用。
2. **提交**：同一 SQLite 事务内写 attachments/对象库索引 + `model_records.payload.multimaterial`；成功后引用才生效。
3. **回执**：只有事务提交成功才发持久化回执；UI/原生失败不改变回执。
4. **历史**：命令栈持有 `history:` 根；撤销/重做沿用同一批资源（不复制字节）。
5. **租约**：预览副本由 `lease:` 根保护；释放租约只清该副本。
6. **回收站**：删除材质/模型时引用转入 `trash:` 根，保留可恢复窗口；清空回收站才允许真正回收独占资源。
7. **回收**：`orphaned_resources` → 删附件/对象；**共享资源（多根引用）永不因单根消失而删**。

### 6.3 目录工程与 SQLite 的边界（不宣称天然原子）

- 单文件工程：attachments 在同一个 SQLite 里，`BEGIN IMMEDIATE … COMMIT` 覆盖"字节行 + payload"，是**单库事务**。
- 目录工程：对象写入文件系统（`object_refs` 索引在 SQLite）。因此提交顺序固定为
  **① 写对象 → ② 同一 SQLite 事务写 `object_refs` + payload → ③ 提交**。
  失败窗口只有"对象已落盘但索引未提交"：这类对象是**孤儿对象**，由启动/保存时的扫描回收（索引无记录 = 可删），不会造成数据丢失。
- 反过来"索引已有、对象缺失"是致命状态：提交前必须校验对象存在与哈希，缺失即拒绝提交。
- **不宣称跨文件系统原子性**：崩溃恢复的判据是"payload 里有引用 ⇒ 对象必须存在"，恢复时扫描并报告，不静默修补。

### 6.4 需要的证据（A3 交付，未做）

提交失败（磁盘满/锁超时）、进程被杀（提交前/提交后）、历史释放后资源才回收、删源材质库后模型快照仍完整、搬迁后不依赖原盘。

## 7. 错误码与终态（与 `validation.py` 一致）

`E_CAPABILITY_MISSING`、`E_PARAM_INVALID`、`E_STALE_SESSION`、`E_STALE_REVISION`、`E_CATALOG_MISMATCH`、`E_UNKNOWN_BINDING`、`E_UNKNOWN_MATERIAL`、`E_SLOT_READ_ONLY`、`E_ACTION_INVALID`、`E_RESOURCE_MISSING`、`E_TARGET_MISSING`、`E_REQUEST_CONFLICT`、`E_STATE_INVALID`、`E_CANCELLED`、`E_COMMIT_FAILED`。
终态：`queued/running/completed/failed/cancelled/stale`；`job_id` 只表示受理。

## 8. 需要主对话裁决 / 别处改一处

### 8.1 契约：`BindingSite.resolution`（阻塞 A1 语义完整性）

建议字段 `resolution: str = 'assigned'`（`assigned|unassigned|dangling`）。
在此之前：悬空使用点**已经保留**在 `binding_sites`（`original_material_id=None`，`source_key` 保留原始 `usemtl:` 名），状态由 `catalog.binding_resolutions(catalog)` 一角读取（有测试）。
需要 AI 4 同步两处期望：`unassigned_sites` 不含 dangling，`dangling_sites` 单列（反证与请求见 `AI_1_REFERENCE_CORRECTIONS.md`）。

### 8.2 词表归属

建议把 `STANDARD_SLOTS`/`SOURCE_ROLES`/`UNVERIFIED_SLOTS` 搬进 `multimaterial/contracts.py`，`vocabulary.py` 保留函数（`editable_slots`/`slot_policy`）。导入方向不变。若主对话决定留在 `vocabulary.py`，AI 2/AI 3 一律从那里导入。

### 8.3 `events.IDENTITY_FIELDS`（AI 4 D6 因此失败，保留失败不掩盖）

增加 `material_id`、`binding_id`、`catalog_revision`、`state_revision`、`draft_generation`；`required_fields` 按事件适用，**不**让所有旧事件必填。

### 8.4 模型域的保存/清理/局部保存（三处补丁）

见 `AI_1_SHARED_PATCHES.md`：`model_attachment_ids`/`register_model_attachments` 覆盖嵌套资源；`partial_project` 把模型域新附件算进 `required`；`encode_record` 与 `decode_record` 对称（已实测不对称，只加 `OWNED_FIELDS` 不够）。

## 9. 落地顺序

1. 主对话裁决 §8.1–8.4 并落补丁；AI 4 同步期望。
2. A2（已做）：`state_store`/`validation`/测量——见 `AI_1_A2_STATE_STORAGE.md`。
3. A3：`commands.py`（六操作 + §4 事务 + §6 生命周期）、`resources.py`（引用根 + 回收）。
4. A4：门面端口注入与调度（生产能力继续关闭）。
