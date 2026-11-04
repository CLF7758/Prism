# 主对话集成记录：多材质日志契约

日期：2026-10-27。仅修复共享日志接口，不实现多材质业务、不开启生产能力、不写用户工程。

## 本次修改

- `prism/model_library/events.py`：新增 material_id、binding_id、catalog_revision、state_revision、draft_generation、gesture_id 身份传递。
- 保留旧模型事件的 LEGACY_IDENTITY_FIELDS，不把新增材质字段变成所有旧事件的必填项，也不填虚假的 0 或空材质。
- 新增 material.parse/draft/commit/install 阶段事件的字段表；解析前不强求尚未知的目录/状态修订，全模型恢复不强求单一材质/使用点；草稿和安装要求代次，提交要求手势，失败要求错误码。
- 材质失败/终态使用现有错误/终态优先级；隐私清洗、路径掩码、日志入口保持原路径。
- model_event_contract 暴露全部身份、旧身份、材质身份、阶段事件及 `model_material_event/1` 子契约。旧 `model_event/1` 保留，新增子契约为可辨认的增量。

`material_id` 在编辑事件中使用工作材质身份；源定义身份可另带 source_material_id，不拿 actor 序号作身份。共享或批量操作的单一 binding_id 可不适用，具体范围应由命令/请求关联，不伪造一个使用点。

## 验证范围

1. 旧日志、共享日志新回归、原生作业与协议回归：61 passed，11.90 秒。
2. AI 4 独立 D6 原测试断言不变复跑：6 passed，0.83 秒。仅将生成证据目录重定向到主对话集成目录，原 AI 4 失败报告未覆盖。
3. 新回归确认身份进入 trace 入口、旧代次与期望代次可区分、材质终态优先级、全模型操作不虚构单一使用点、缺代次能诊断。

原始证据：`artifacts/model-multimaterial-integration-20261027/` 下 `logging-regression.txt`、`d6-recheck.txt`、`d6-recheck/logging-contract.json`；重定向运行器为 `recheck_d6.py`。

**证明边界：**DEF-1 的字段声明/传递子项已修复；尚未通过真实解析→草稿→事务→原生安装的生产链验证。新增回归截取 trace 入口，不能证明写盘或用户编辑端到端日志完整。DEF-1 的端到端追踪子项交 AI 4 保留待验，不能仅因 6 项通过关闭整个编辑验收。

## 样本只读核查

`E:/Prism-stress-100k/SHA256SUMS.txt` 474 条记录在其声明路径下均不存在。根目录还有 Game、两个 .prism 文件、清单和总览图。

这一检查只证明声明路径不完整，不证明文件已被删除、不证明 Game 其他位置没有资产、不证明工程内没有嵌入原件。没有打开或改写大工程，也没有从中提取样本。原 20 模型和真实十万混合集不可继续沿用旧可用性结论。

证据：`artifacts/model-multimaterial-integration-20261027/stress-inventory-check.json`。真实十万混合负载、原件迁移后的完整性仍未验。可独立开展合成索引负载，但不能代替真实混合资产测试。

## 给 AI 4 的下一步

保留 D0/D1 解析基线及 DEF-2/3/4 反例。新增编码碰撞、悬空/未指定完整使用点、同目标乱序预览、取消晚到、提交期间新草稿、跨材质拾取、快照预览后源库变化等独立期望，待契约统一后核验实现。

FBX FileName/RelativeFilename 同时保留声明；相对候选在资产包内确实存在时可优先用于解析，但必须记录实际采用路径、冲突与歧义，不能覆盖原始声明。无 usemtl、缺 MTL、未指定、悬空材质分开建期望。

冷暖解析成绩只归解析层；D2/D3/D4 和暖选/安装/首帧继续受阻/未测。F3D 环境采集在干净子进程先 import f3d，不把导入顺序失败写成安装不可用。原失败记录保留，并追加正确条件下的新结果。
