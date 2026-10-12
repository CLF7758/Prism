# 阶段 A 基线报告

日期：2026-10-12
执行 AI：QoderWork
方案文档：`C:\Users\admin\Desktop\Prism_侧栏与窗口整改实施方案_给开发AI_2026-10-12.md`

## 1. 工作副本与备份

- 原始项目：`C:\Users\admin\Desktop\Prism-main`（未修改）
- 备份副本：`C:\Users\admin\Desktop\Prism-backup-2026-10-12`（源码 8.8 MB，排除 .venv / __pycache__ / .pytest_cache / output / build / dist / node_modules）
- 工作副本：`C:\Users\admin\Desktop\Prism-work`（同规格源码副本）
- 共享虚拟环境：`C:\Users\admin\Desktop\Prism-main\.venv`（Python 3.12.8，PyQt6 6.7）
- 验证：从 `Prism-work` 目录用 `.venv\Scripts\python.exe` 运行时 `prism.__file__` 指向 `Prism-work\prism\__init__.py`，隔离成功。原项目无 editable install（`site-packages` 里没有 prism*.pth）。

## 2. 桌面 参考.prism 只读快照

只读打开（`mode=ro&immutable=1`），未写回。

| 事实 | 值 | 方案断言 | 一致性 |
| --- | --- | --- | --- |
| 文件大小 | 178.51 MB | — | — |
| `PRAGMA user_version` | 4 | 4 | 一致 |
| 表清单 | attachments, board_mindmap, board_notes, items, prism_metadata, sqlar, sqlite_sequence, workspace_nodes, workspace_pages | — | — |
| `items` 总数 | 1562 | 1562 pixmap | 一致 |
| `items.type` 分布 | pixmap: 1562 | — | 一致 |
| `sqlar` 总数 | 1562 | — | — |
| `workspace_nodes` 总数 | 2 | 2 | 一致 |
| `workspace_nodes` deletedAt != null | 0 | 0 | 一致 |
| `workspace_pages` 总数 | 1 | — | — |
| `board_notes` 总数 | 2 | — | — |
| `attachments` 总数 | 0 | — | — |
| `board_mindmap` 总数 | 0 | — | — |
| `items.data` 里 canvasId 分布 | `default-canvas`: 1561, `<None>`: 1 (id=287) | 1561 + 1 缺失 | 一致 |
| 素材标签状态 | 全部 1562 张 `_tags = []` | 全部未标签 | 一致 |

`items.data` JSON 出现过的键（1562 条并集）：
`canvasId, categories, colorGroup, crop, dominantColor, filename, grayscale, groupId, groupNote, imageHeight, imageWidth, notes, opacity, rating, sourceFormat, tags, title`

**注意：桌面样本的 `data` 里没有 `deletedAt` 键，说明当前 schema 素材级别没有软删除字段。**

## 3. 与截图目录树不一致的关键事实

`workspace_nodes` 只有 2 条，都是 `section='canvas'` 的顶层页面：

| id | title | pageId | parentId | deletedAt |
| --- | --- | --- | --- | --- |
| 790f4904… | 默认画布 | default-canvas | null | null |
| 6b13f4e9… | 未命名画布 | 7558466c… | null | null |

但 `prism_metadata.categories` 存了 31 个分类：`参考图, ARC Raiders, GTA6, Stray, The Finals, 巫师三, 掠影, 摄影棚合集, 摄影导图合集, 心灵杀手, 洛克东, 无尽任务平等, 刺客信条-幻, 刺客信条-英, 死亡搁浅之大, 沙丘, 曼德拉文本, 燕云十, 逆水2, 逆水寒2077, 沙尘, 失落星大荒野, 荒野镖客, 生化杀机2, 星际公民, 极端战, 阿尔山之境, 战地, 战神, 蜘蛛侠, 黑色行动伍`（部分因编码显示乱码，实际内容为 UTF-8 中文）。

**结论**：截图里的"参考 / ARC Raiders / GTA6 …"目录树实际上是**旧 `categories` 系统**（`items._categories` 数组 + `prism_metadata.categories` 名单），跟新的 `workspace_nodes` 资源树并存。

方案 §3.3 说"资源树的父文件夹计数来自下属画布汇总"—— 这个断言只对 `workspace_nodes` 的 canvas 分区成立。旧 `categories` 系统的"父目录 1562"实际是通过 `ResourceTreeModel.set_asset_counts()` 从当前画布归属汇总得来的（见 `_update_navigation` line 299-304）。

`categories` 系统的入口在 `media_library.py` line 555 `cat_names = set(self._load_categories())` 和 `line 767 categories()`。方案没提到这个双系统，实施时要注意不要把它当作 workspace_nodes 处理。

## 4. 源码断言核对

| 方案断言 | 实际 | 一致性 |
| --- | --- | --- |
| `media_library.py._setup_ui()` ~98 行 | 98 行 | 一致 |
| `media_library.py._navigate()` ~276 行 | 276 行 | 一致 |
| `media_library.py._update_navigation()` ~287 行 | 287 行 | 一致 |
| `media_library.py._apply_filter()` ~638 行 | 638 行 | 一致 |
| `media_library.py._show_create_menu()` 存在 | 272 行 | 一致 |
| `libraryHeader` 在 `_setup_ui()` 里创建 | 109-121 行 | 一致 |
| `('tags', '标签管理', 'tag')` 导航项 | 151 行 | 一致 |
| `detail_panel.py` 原色彩分布 ~276 行 | `_cd_container` 在 280 行 | 一致 |
| `detail_panel.py._build_color_distribution()` ~801 行 | 801 行 | 一致 |
| `detail_panel.py._update_palette()` ~884 行 | 884 行 | 一致 |
| `_palette` / `_palette_layout` 存在 | 95-101 行 | 一致 |
| `_setup_ui()` 隐藏 `_cd_container` + `lbl_cd` + `_hist_widget` | 293-296 行 | 一致 |
| `_show_empty()` 里 `_palette.hide()` | 391 行 | 一致 |
| `_refresh_item()` 里 `_palette.hide()` | 782 行 | 一致 |
| `_build_color_distribution()` 里调 `_update_palette(sample)` | 832 行 | 一致 |
| `scene.items_for_save()` 不排除删除状态/注释 | `filter(lambda i: hasattr(i, 'save_id'), ...)` (539 行) | 一致 |
| `commands.DeleteItems` ~55 行 | 55 行 | 一致 |
| `DeleteItems.redo()` 从 scene 移除图片 | 62-63 行 `self.scene.removeItem(item)` | 一致 |
| `sql._write_data_batch()` ~471 行 | 471 行 | 一致 |
| `sql._backup_before_overwrite()` 存在 | 105 行 | 一致 |
| `sql.delete_items()` 清理 sqlar | 550-564 行 | 一致 |
| `schema.USER_VERSION = 4` | schema.py:1 | 一致 |
| `project_search.search_project()` 存在 | project_search.py:34 | 一致 |
| `workspace_service.ResourceTreeService.trash/restore_deleted/purge/trashed_nodes` | 333/355/393/399 行 | 一致 |
| `__main__.py._open_trash_panel()` | 747 行 | 一致 |
| `__main__.py._on_trash_purged()` | 757 行 | 一致 |
| `main_controls.MainControlsMixin` 已有拖动 | 39-197 行 | 一致 |
| `PrismMainWindow(QMainWindow)` 无 FramelessWindowHint | 109 行 | 一致 |
| `menuBar` 有 `_menu_file_title` corner widget | 123-126 行 | 一致 |
| `closeEvent` 已实现 | 1372 行 | 一致 |
| `items.PrismItemMixin.canvas_id` property | 361-371 行 | 一致 |
| `items.get_metadata_save_data()` 写 canvasId | 401 行 | 一致 |
| `items.load_metadata_from_data()` 读 canvasId 默认 'default-canvas' | 413 行 | 一致 |

方案对源码的所有断言 100% 与当前 Prism-main 一致。

## 5. 基线测试结果

命令：
```
cd /c/Users/admin/Desktop/Prism-work
QT_QPA_PLATFORM=offscreen /c/Users/admin/Desktop/Prism-main/.venv/Scripts/python.exe -m pytest \
  tests/test_eagle_sidebars.py tests/test_project_search.py tests/test_search_scope.py \
  tests/test_resource_tree_model.py tests/test_resource_tree_service.py tests/test_resource_tree_trash.py \
  tests/test_tag_inspector.py tests/test_tag_boundaries.py tests/test_tags.py \
  tests/test_detail_panel.py tests/test_material_features.py tests/test_data_safety.py \
  tests/test_source_metadata.py tests/fileio/test_sql.py tests/test_unsaved_triggers.py \
  tests/test_main.py tests/test_ui_scale.py tests/test_interaction_performance.py \
  tests/test_commands.py --tb=line -q
```

结果：**284 passed in 130.61s**，无失败、无跳过。

方案 §13 提到的"上一轮 273 通过 / 19 跳过"是不同测试集，本次基线覆盖方案指定的所有测试文件，全部通过。

## 6. 与方案冲突的既有测试

`tests/test_eagle_sidebars.py` 里两个测试与本次需求直接冲突：

- **`test_untagged_navigation_filters_current_canvas_and_can_clear`**（19-35 行）：
  - 断言 `panel._nav_counts['all'].text() == '2'` —— 3 张图（当前画布无标签 1 + 当前画布有标签 1 + other-canvas 无标签 1）+ 1 个 text annotation。当前逻辑：all=2（只算当前画布的两张图），untagged=1。
  - 断言点击 untagged_button 后 `not elsewhere.isVisible()` —— other-canvas 的图不可见。
  - **方案要求**：all=3（全项目），untagged=2（全项目无标签）。必须重写。

- **`test_palette_copies_sampled_colour_and_clears_on_deselection`**（48-58 行）：
  - 直接测新增 `_palette_layout` 圆点色带的复制行为。
  - **方案要求**：删除 `_palette` 及其相关代码。此测试必须删除。

保留的测试：
- `test_tree_counts_roll_up_nested_canvas_folders`（资源树父目录递归汇总）
- `test_tag_chips_wrap_and_long_names_fit`（FlowLayout 换行）

## 7. 关键架构决策预判

基于源码事实，后续阶段采取的技术路线：

### 7.1 素材软删除（Stage C）

**决策：软删除图片留在 scene 里，只隐藏 + 不可选 + 计数/搜索/导出/查重/画布隔离入口过滤。**

理由：
- `items.data` 是 JSON 列，加 `deletedAt` / `deletedBatchId` 字段**不需要 schema migration**（USER_VERSION 保持 4）。旧文件读取时 `data.get('deletedAt')` 为 None 即视为活跃。
- `_write_data_batch()` 靠 `items_for_save()` 决定清理 —— 只要软删除图片仍在 scene 且有 `save_id`，DB 记录和 `sqlar` 源字节自然保留。
- 不用改 `_backup_before_overwrite()` / `delete_items()` / 另存为链路。
- 撤销/重做直接改 `_deleted_at` 字段 + `setVisible`，比"从 scene 移除/加回"更安全。

代价：需要在所有绘制/选择/过滤/导出/查重/搜索/页归属/计数入口识别删除状态。这些入口在源码里已经用 `scene.items_for_save()` 或 `scene.items()` 枚举，集中改造可行。

### 7.2 全项目统计（Stage B）

**决策：新增 `prism/project_catalog.py`，导出 `ProjectAssetCatalog` 类。**

接口：
- `active_items()` → 全项目活跃素材生成器（跨画布、排除注释、排除软删除）
- `total_count()` → 活跃素材总数
- `untagged_count()` → 无有效标签的活跃素材数
- `canvas_counts()` → `{canvas_id: count}` 字典，无 canvasId 归入 `UNASSIGNED_CANVAS = '__unassigned__'` 常量桶
- `trash_count()` → 顶层删除记录数（图片按批次归并，见 §7.1）
- `search(query, tag_expression, untagged_only, min_rating, canvas_scope)` → 全项目结果列表
- `locate(item)` → 返回 `(page_kind, page_id)` 用于导航

`MediaLibraryPanel._update_navigation()` 改为委托 `ProjectAssetCatalog`。`_navigate('all'/'untagged')` 进入全项目结果模式（复用 `search_results` 树控件）。

### 7.3 无边框窗口（Stage E）

**决策：`FramelessWindowHint` + Windows `nativeEvent` 处理 `WM_NCCALCSIZE` / `WM_NCHITTEST` / `WM_NCLBUTTONDBLCLK`。**

理由：
- 方案 §9.2 明确要求"使用系统窗口移动/调整尺寸能力优先"、"贴靠、任务栏显示、Alt+Tab、快捷键关闭以及最小化后恢复正常"、"最大化时不覆盖任务栏"。
- `QWindow.startSystemMove()` / `startSystemResize()` 在 Qt 6.7 Windows 上可用，但最大化/贴靠/阴影需要 `WM_NCCALCSIZE` 处理才完整。
- 采用 `WM_NCCALCSIZE` 返回 0（保留原生非客户区行为）+ 自绘标题栏叠加，能同时得到阴影/圆角/贴靠/动画。
- `main_controls.MainControlsMixin` 的 Krita 风格 `movewin_mode`（快捷键激活手动拖动）不冲突，可共存。

## 8. 阶段 A 结论

- 备份 + 工作副本已就位，隔离验证通过
- 桌面 `参考.prism` 快照已采集，与方案断言 100% 一致
- 源码所有行号/函数/字段断言 100% 与当前 Prism-main 一致
- 基线 284 项测试全通过
- 与方案冲突的既有测试已识别（`test_eagle_sidebars.py` 两个）
- 关键架构决策已预判，可进入 Stage B
