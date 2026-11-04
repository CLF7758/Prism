# RFC-0001 · BindingSite 增加源实例身份与匹配证据

状态：**提案（未实施）**。共享契约由主对话统一修改；AI 2 只提定义、算法、序列化与兼容规则。
影响文件（提案）：`prism/model_library/multimaterial/contracts.py`（`BindingSite`）、
`prism/model_library/multimaterial/catalog.py`（payload 序列化）、各 `inspectors/*.py`（产出方）。

## 1. 动机

B0 用"位置 + 颜色"对账，B0.1 用"结构键 + 变换哈希"，两次都被证明不够：

- 颜色是用户要改的量，位置可以合法重复；
- 变换哈希相同不代表是同一个部位（GPU instancing 的两份实例可以完全一致）；
- 几何指纹只覆盖位置与索引，证明不了 UV/法线/材质分组有没有变。

因此把**身份**与**匹配证据**拆成三类字段，并把规则版本写进数据。

## 2. 新增字段（`BindingSite`）

| 字段 | 类型 | 默认 | 语义 |
| --- | --- | --- | --- |
| `instance_key` | `str` | `''` | **源文件里明确存在的实例身份**。不是变换哈希。取值规则见 §3。`''` 表示"该格式没有实例概念"或"单实例"。 |
| `geometry_fingerprint` | `str` | `''` | 匹配证据：位置 + 索引的内容摘要（§4.1）。空表示解析器未提供。 |
| `content_fingerprint` | `str` | `''` | 匹配证据：UV + 法线 + 材质分组 + 双面 + alphaMode 的摘要（§4.2）。空表示未提供。 |
| `match_rules_version` | `int` | `0` | 产生这些证据时使用的规则版本（§5）。`0` 表示"旧数据，字段可信度未知"。 |

**禁止**：用 `node_path`、材质名、材质值、变换哈希、显示顺序替代上述任一字段作为身份。

## 3. `instance_key` 生成规则（按格式，必须由解析器给出）

| 格式 | 取值 | 说明 |
| --- | --- | --- |
| glTF/GLB，普通节点 | `""` | 一个节点就是一个使用点，节点索引已在 `binding_id` 里 |
| glTF/GLB，`EXT_mesh_gpu_instancing` | `node:<i>/instance:<k>` | `i` 是节点在 `nodes[]` 里的下标，`k` 是该节点 `attributes` 数组（TRANSLATION 等）里的下标 |
| FBX，几何被多个 Model 引用 | `model:<uid>/instance:<k>` | `uid` 取 FBX 对象 UID（AI 1 已有），`k` 是同一 UID 的第 k 次引用（按 Connections 顺序，**只在同一文件内稳定**） |
| OBJ | `""` | 没有实例；面组靠 `primitive_key`（`o`/`g`/`usemtl` 组成）区分 |
| 其他 | `""` + 说明写入 `unsupported` | 不允许自行编造 |

要求：**相同几何、相同变换、但源里有明确实例身份时**，构造式映射必须能区分（映射测试已覆盖）；
只有"对账时源侧拿不到实例身份"或"跨修订内容键不唯一"才允许拒绝。

## 4. 指纹算法（版本 2）

### 4.1 `geometry_fingerprint`

```
sha256( len(positions) || for each position: "%.5f," % round(v,5)
        || len(indices) || for each index: int32_le )[:32]
```

- 只含位置与索引；不含法线/UV/颜色/材质。
- 舍入到 5 位小数：不同加载器的 float/double 宽度差异不会改变结果。
- **只在同一 `parser_id` 内比较**：索引顺序、顶点去重策略不同的解析器会产生不同指纹。

### 4.2 `content_fingerprint`

```
sha256( len(uvs) || uvs(5 位) || len(normals) || normals(5 位)
        || len(groups) || for each group: len || utf8(group)
        || 双面标志(1/2) || alpha_mode 原文 )[:32]
```

- `groups` 是**面组到材质的映射摘要**（按面顺序或排序后的 `primitive_key→material_source_key` 列表），
  这样"贴图没变但某个 primitive 换了材质"能被发现。
- 不含材质数值：数值是用户要改的东西，不能进身份，也不能进"内容是否变"的判据。

### 4.3 变换指纹（仅当需要区分实例时使用，不入库）

```
sha256( "%.5f," 连接 16 个矩阵元素 )[:16]
```

只在 evidence 里使用，**不得**写进 `instance_key`。

## 5. 规则版本与兼容

- `MATCH_RULES_VERSION = 2`（实现：`prism/model_library/multimaterial/native/bindings.py`）。
- 读取旧数据（`match_rules_version == 0` 或缺字段）：
  1. 只按 `binding_id` 精确匹配；
  2. 不做内容承接（因为旧数据没有证据，**不允许猜测**）；
  3. 在 UI/日志里明确"该工程的使用点缺少匹配证据，跨修订无法自动承接"。
- 写入新数据：三个字段一律写出（空串也写），并写 `match_rules_version`。
- 旧程序读新数据：`catalog_from_payload` 已对未知字段宽容；**但不得**让旧程序回写到丢失字段，
  因此 `binding_id` 之外的三个字段必须进入 `unknown_json` 保护范围（或直接拒绝旧版本写入）。
  这一点需要主对话在存储层确认（本轮**未**修改存储）。

## 6. 失败语义（与实现一致）

| 枚举 | 场景 |
| --- | --- |
| `AMBIGUOUS_NATIVE` | 同一结构键对应多个原生对象 |
| `SHARED_NATIVE_OBJECT` | 两个使用点争用同一个原生对象 |
| `DUPLICATE_NATIVE_ID` / `binding_id 重复` | 调用方输入违约（前者报告失败，后者直接抛 `MappingContractError`） |
| `MISSING_NATIVE` / `MISSING_BINDING` | 一侧缺失 |
| `GEOMETRY_MISMATCH` / `CONTENT_MISMATCH` | 证据不一致 |
| `IDENTITY_CHANGED` / `CONTENT_CHANGED` | 跨修订同 ID 但身份或内容变了 |
| `FINGERPRINT_COLLISION` | 内容键命中多个旧使用点 |

## 7. 验收建议（供 AI 4 采纳）

1. 构造"相同位置 + 相同颜色 + 不同节点" → 必须两个 binding。
2. 构造"同名节点 + 同名材质 + 不同几何" → 必须两个 binding。
3. 构造"同名 + 同几何 + 同材质" → 必须拒绝，不得选中任意一个。
4. GPU instancing 两个相同变换的实例 → 有 `instance_key` 时必须区分；缺失时必须拒绝。
5. 源数组重排（nodes/meshes/materials 全部打乱，内容不变）→ 分配必须沿用，且**不得**沿用原生句柄。
6. UV/材质分组单独变化 → `CONTENT_MISMATCH`，不得只看位置就通过。

上述 1—6 已在 `tests/model_library/multimaterial/test_runtime_bindings.py` 里实现（23 个用例全通过），
可作为契约改造后的回归基线。
