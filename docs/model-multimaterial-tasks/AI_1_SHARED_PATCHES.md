# AI 1 · 共享文件候选补丁（交主对话统一集成）

原则：`project_storage.py`、`partial_project.py`、`events.py`、共享 `contracts*.py` 由主对话改；这里只给**最小补丁、调用点、依赖与针对性测试**。
每个补丁都对应 `tests/model_library/multimaterial/test_data_shared_patches.py` 里一条现在会 `skip` 的用例——补丁落地后它会自动变成断言。

## P1（RFC 8.4-a）`model_attachment_ids` / `register_model_attachments` 覆盖嵌套资源

现状：`project_storage.py:512/553` 只遍历 `files` + `appearance.bindings`。实测 `model_attachment_ids(payload)` 对带 `multimaterial.resources` 的 payload 只返回 `[9000]`（原件），9001/9002 丢失 → 保存后清理会把新贴图当孤儿删掉。

```python
# prism/model_library/project_storage.py
def _nested_material_attachment_ids(payload: dict) -> tuple:
    """Project attachments owned by the multi-material state (not the originals)."""
    ids = []
    block = payload.get('multimaterial') or {}
    if not isinstance(block, dict):
        return ()
    for entry in (block.get('resources') or {}).values():
        if isinstance(entry, dict) and entry.get('attachment_id') is not None:
            ids.append(int(entry['attachment_id']))
    for material in ((block.get('state') or {}).get('materials') or ()):
        if not isinstance(material, dict):
            continue
        for patch in material.get('textures') or ():
            value = (patch or {}).get('value') or {}
            if isinstance(value, dict) and value.get('attachment_id') is not None:
                ids.append(int(value['attachment_id']))
    return tuple(ids)
```

调用点（三处都要，缺一就会丢数据）：

| 位置 | 改法 |
| --- | --- |
| `project_storage.py:553 model_attachment_ids` | `ids.extend(_nested_material_attachment_ids(payload))` |
| `project_storage.py:512 register_model_attachments` | 在 `entries` 之后 `for attachment_id in _nested_material_attachment_ids(payload): ...`（沿用同一把锁与 `MODEL_MIME`） |
| `prism/fileio/sql.py:1773` | 无需改（走 `model_attachment_ids`） |

依赖：无。风险：误把**未提交**的暂存资源登记为长期引用 → 允许（比丢图安全），但要在 A3 用租约根区分。
测试：`test_pending_nested_material_attachments_are_part_of_the_model`、`test_pending_nested_material_attachments_are_registered`。

## P2（RFC 8.4-b）局部保存把模型域新附件算进 `required`

现状：`prism/partial_project.py:748-766` 只对 `material_records` 收集 `references`/`required`；`model_records` 只在提交后 `_valid_domain_attachments.update(model_attachment_ids(after))`（807 行）。→ 页级保存新增的材质贴图**不在** `required`，`partial_save.py:422` 不会要求它存在。

```python
# prism/partial_project.py, inside `if self._domains is not None:` after the material loop
if self._models_need_attachment_scan():
    from prism.model_library.project_storage import model_attachment_ids
    for record in getattr(scene, 'model_records', ()) or ():
        if not isinstance(record, dict):
            continue
        current = set(model_attachment_ids(record))
        model_id = record.get('model_id') or record.get('id')
        old = self._rows.get(('model_records', model_id))
        old_ids = set(model_attachment_ids(_json_object(old[0]))) if old else set()
        references.update(current)
        required.update(current - old_ids)
```

`self._rows` 的键必须与 `partial_project.py:772-774` 的 `('model_records', key)` 一致（那里已经在用同一形状）。
依赖：P1（`model_attachment_ids` 要先覆盖嵌套资源）。测试：`test_pending_partial_save_counts_model_attachments_as_required`（现在是"出现次数"启发式；落地后请把断言换成对 `capture_partial_save(...).required_attachments` 的直接检查）。

## P3（RFC 8.4-c）`encode_record` 与 `decode_record` 对称

现状（实测）：`decode_record` 把 `multimaterial` 收进 `unknown_fields`，但 `encode_record` 只把它写在 `payload['unknown_fields']` 下，不展开回顶层。保存之所以没丢，是因为 `repository._write_payload` 又调 `merge_unknown_fields` 合并了一次。→ 任何直接用 `encode_record` 的地方（导入构造 payload、未来的命令层）都会丢。

```python
# prism/model_library/project_storage.py, end of encode_record()
    for key, value in (payload.get('unknown_fields') or {}).items():
        if key not in CONTRACT_FIELDS and key not in OWNED_FIELDS:
            payload.setdefault(key, value)
    return payload
```

同时建议 `OWNED_FIELDS` 增加 `'multimaterial'`（显式认领，但仍必须能被回写——只认领不回写就是本 bug）。
测试：`test_encode_record_flattens_the_retained_block`（现在 skip）、`test_owning_the_field_must_not_hide_it_from_the_record`（现在通过，防回退）。

## P4（RFC 8.3）`events.IDENTITY_FIELDS` 增加材质身份

```python
# prism/model_library/events.py
IDENTITY_FIELDS = (..., 'material_id', 'binding_id',
                   'catalog_revision', 'state_revision', 'draft_generation')
```

要求：`model_event_contract()['required_fields']` **按事件**给出这些字段（材质事件必填、旧模型事件可选），不要把它们变成所有旧事件的必填项。
测试：`test_pending_events_declare_material_identity`（skip）+ AI 4 的 `acceptance/test_d6_logging.py`（**保留失败，不放宽**）。

## P5（RFC 8.1）`BindingSite.resolution`

```python
# prism/model_library/multimaterial/contracts.py
@dataclass(frozen=True)
class BindingSite:
    binding_id: str
    source_key: str
    node_path: str
    geometry_key: str
    primitive_key: str
    original_material_id: str | None
    resolution: str = 'assigned'      # assigned | unassigned | dangling
```

落地后我只需在 `catalog.CatalogBuilder.add_binding` 把 `note_binding_resolution()` 收的值写进该字段（其余语义不变），`catalog.binding_resolutions()` 可保留为读取便捷函数。
同时需 AI 4 同步期望：`unassigned_sites` 不含 dangling（见 `AI_1_REFERENCE_CORRECTIONS.md`）。
测试：`test_pending_binding_site_carries_its_resolution_state`。

## 顺序与回退

1. P3 → P1 → P2（数据不丢是前提）→ P4/P5（契约与日志）。
2. 每个补丁都可单独回退：P1/P2 删掉新增遍历即回到"只认 files"的行为；P3 删掉展开循环即回到现状（现状仍靠保存路径兜底）；P4/P5 是纯增字段。
3. 任何一步都不需要迁移用户工程数据：`multimaterial` 段缺失即表示"没有多材质状态"。
