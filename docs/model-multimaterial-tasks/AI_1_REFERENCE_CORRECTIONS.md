# AI 1 → AI 4：两处独立期望需要修正（附反证）

`tests/model_library/multimaterial/acceptance/` 是独立期望的持有者，D1 目前 `95 passed / 3 failed / 1 skipped`。
三条失败**不是产品回归**，而是"产品语义按主对话要求修正后，期望需要同步"。这里给出证据与建议改法；我没有改你的文件。

## 1. 悬空材质引用：期望把 dangling 计进 unassigned，产品现在分开计

主对话退回意见（§一）明确：悬空引用**必须留在使用点里**，并带 `assigned/unassigned/dangling` 三态；不能从使用点集合移除后只藏进 `unknown_json`。
产品已按此实现：悬空使用点保留在 `binding_sites`（`original_material_id=None`，`source_key` 保留原始 `usemtl:<名>`），状态由 `catalog.binding_resolutions()` 给出。

失败用例：

| 用例 | 期望 | 产品 |
| --- | --- | --- |
| `fixture:obj_dangling_and_unused` | `unassigned_sites` = 1（悬空不算 unassigned），`dangling_material_references` = 1 | `binding_sites` 里 None 计数 = 2（1 真 unassigned + 1 dangling） |
| `upstream:assimp_multimaterial` | 同上（`unassigned 0 / dangling 1`） | None 计数 = 1（全部来自 dangling） |
| `upstream:assimp_utf16` | `unassigned 0` | None 计数 = 1（dangling） |

**建议的期望改法**（不改产品）：

```python
# test_d1_against_product.py, compare()
from prism.model_library.multimaterial.catalog import binding_resolutions
resolutions = binding_resolutions(catalog)
actual_unassigned = len([b for b in catalog.binding_sites
                         if resolutions.get(b.binding_id) == 'unassigned'])
actual_dangling = len([b for b in catalog.binding_sites
                       if resolutions.get(b.binding_id) == 'dangling'])
if facts['unassigned_sites'] != actual_unassigned:  # 报问题
    ...
if len(facts.get('dangling_sites') or []) != actual_dangling:  # 新增一条对照
    ...
```

改完后三条应一起转绿；若主对话批准 RFC 8.1（`BindingSite.resolution` 字段），这段可以直接读字段而不用 helper。

## 2. 文本编码：参考读取器会丢信息，不宜作为"逐字一致"的标准

主对话退回意见（§二）：不能为匹配参考脚本而统一 UTF-8 `replace`；身份不能建立在有损解码上；无法确定编码要报歧义。

产品现行策略（`inspectors/text.py`，10 条测试）：
1. UTF-8 BOM / UTF-16 BOM / 严格 UTF-8 → **确定**，身份=文本；
2. 其他 → 候选（gb18030 → big5 → shift_jis → cp1252），**标记为不确定**，显示用第一个候选，列出全部候选，`unresolved` 写明"编码不确定"，身份 = `名字~原始字节哈希`；
3. 所有候选都失败 → `damaged`，显示带 U+FFFD，身份仍由原始字节决定；两个不同坏字节序列**不会**合并成一个材质（有夹具）。

反证（可直接复现）：

| 事实 | 参考读取器 | 产品 |
| --- | --- | --- |
| `box_mat_with_spaces.obj` 中 `Terraindök`（0xF6 字节，既非 UTF-8 也非任何候选的合法串） | `utf-8 + replace` → `Terraind\xfffdk`，**原始字节信息丢失且没有任何标记** | 同样显示替换字符，但**报告编码不确定**并保留原始字节作身份 |
| 两个不同坏字节序列（如 `\x81` 与 `\x8d`） | 都解成同一替换字符 → 若按名字建身份就会**合并成同一材质** | 身份含字节哈希 → 两个材质，可分别定位 |
| GB18030 材质库（`木材` = `\xc4\xbe\xb2\xc4`） | 解成 4 个替换字符，名字不可读 | 显示 `木材`，标不确定，身份含字节哈希 |

也就是说：**参考读取器在 `utf-8 + replace` 上是有损的**，把它当作"唯一标准答案"会迫使产品也丢信息。建议：
- D1 的名称对照在 `'\ufffd' in expected_name` 时保持现状（记为 note），但**新增**两条硬对照：①坏字节文件必须报告"编码不确定/损坏"；②两个名字解码相同但字节不同时，产品必须给出两个材质且使用点计数各自正确。
- 若要统一"参考也读 GB18030"，请先在 `material_reference.py` 里加 BOM/严格 UTF-8/候选链，再同步我的实现（我会跟随，因为语义不是向参考妥协，而是两边都变正确）。

## 3. 顺带说明：这两处不是"产品领先，参考落后"

- 悬空三态：产品改动是主对话要求，期望需要更新，**责任在期望侧**。
- 编码：两边都应改成"确定/不确定/损坏 + 原始字节身份"，**责任在双方**；在参考改完前，D1 的逐字名称对照请继续按 §2 的 note 处理，不要为了绿而把产品改回有损。
