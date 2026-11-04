# 模型目录拖放复用（2026-10-29）

用户操作：在 A 目录选中一个或多个模型，按住左键拖到资源树 B 目录，释放后移动；不是复制或导出原件。

## 现有链路直接复用

MaterialCards/cards.js dragstart → _CardBridge.dragTextures → ModelLibrary.drag_models → library_transfer.start_library_drag → ResourceTreeView._library_drop → ModelLibraryComposition.move_library_assets → ModelLibraryService.patch_metadata_batch → 现有仓库事务/scene 提交 → 模型列表刷新。

模型和材质共用卡片、MIME、目录命中、悬停与落点、创建菜单、移动动作及工程会话校验。模型入口使用 ModelSelection.freeze()，保留跨分页选中语义，不改成仅拖动当前 DOM 可见条目。不新增拖放协议或服务，不改存储/渲染架构。

## 本轮修复

MaterialCards.select_record 原先在本地 toggle 前发 selection_toggled；模型控制器同步 set_selection 后，卡片再次 toggle，再由 selection_changed 回写。这不会稳定丢失最终多选集合（旧逻辑的最初移动测试通过，证据保留），但会让 Ctrl 新增选中第二个模型时焦点错误跳回第一个，已用真实控制器复现。

现在先完成卡片本地选择，再发布原始点击身份，允许控制器回写最终权威选择；后续不再覆盖回写结果。材质库与模型库仍使用同一 MaterialCards。保留旧算法的独立子进程测试在焦点断言失败，修复后通过；没有改写生产源码来运行旧算法。

## 验证

新增 tests/model_library/test_drag_move.py：真实临时工程、真实 OBJ 导入、A/B 目录、WebEngine DOM 点击/Ctrl 点击/dragstart、WebChannel、真实 QDrag MIME、Qt 进入/移动/落下事件、真实组合器/事务、源列表移除、保存后重新打开仓库、文件记录与原件 SHA-256 不变。

OS 原生拖动循环 QDrag.exec 在测试中用 Qt DropEvent 注入替代；这是非桌面交互链路验证，不声称已完成真实 Windows 鼠标拖动验收。未操作用户桌面、未改用户真实工程。

另复用现有边界测试：批量冲突原子回滚、未知字段保留、过期会话/其他工程与错误分区拒绝、整批落到文件夹或模型库根。共享卡片、模型选择/分页/导航、资源树拖放、多选与材质搜索回归合计 104 passed in 37.68s。

原始输出 artifacts/model-library-drag-reuse-20261029.txt；对照 artifacts/model-drag-selection-before-20261029.txt（原算法移动链路可通过）与 model-drag-focus-before-20261029.txt（原算法焦点失败），保留以区分已证问题与最初假设。
