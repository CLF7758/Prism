# Prism 当前遗留问题与后续开发交接书

日期：2026年10月22日。工作目录：`C:\Users\admin\Desktop\Prism-main\Prism-main`。

**最高优先级：继续修改项目性能代码，先解决打开、切换、布局、清晰度恢复和资源占用。不要先重新全量诊断、重写计划、扩展保存框架或做无关新功能。**

本文是当前接续清单，不是完成报告。原桌面《Prism完整优化开发交接书-20261021.md》继续作为架构和详细契约参考；其附录F的完成表述需按本文纠正。本文优先顺序和最新代码状态优先于旧状态表。

---

## 〇、2026-10-22 11:30 核对结论与优化方向（本轮源码级复核）

> **本节有两层**：0.1–0.7 是 **11:30 的核对快照**；**0.8 是 12:52 的实施结果与失败台账**，以它为准。0.4 的 15 项状态表是 11:30 的判断，实施后多数已变。

> 本节是**本轮对本文全部 15 项任务 + 保存线 6 项做源码级复核**后的结论，优先级高于下面 §一至§十的旧状态表。
> 下面各节保留原文作为架构与契约参考，但其中"R 表/A 表"的状态列凡与本节冲突，以本节为准。

### 0.1 本轮怎么核的

- 4 路并行只读复核（打开链与全库驻留 / 保存登记与回执 / 跨页移动与导出另存 / LOD、预算与规模验收），全部以 `文件:行号` 为准，不采信任何文档措辞。另启动 1 路对抗性复核（verifier），因长时间无输出而停止、未产出结论；其职责范围内的六条论断（§〇 0.2 与 0.4 中的 6 项）**已由主线程逐条独立取证确认**，结论不依赖该路。
- 只复跑了 **1 个**已知失败用例：`tests/test_view.py::test_on_action_open` → **仍然失败**（`assert 0 == 1`，`tests/test_view.py:288`）。打开 `tests/assets/test1item.bee` 后场景 0 项。§二.3 的"当前失败，原因及引入时间待查"依然成立，本轮**仍未定位引入时间**，不得写成"既有回归"。
- 本轮曾出现并已修复的失败：`catalog-scalar-prefilter-20261022.txt` 中 `test_catalog_browser_partial.py::test_unchanged_scope_skips_filter_and_install_but_metadata_invalidates` 失败，33 秒后由 `catalog-scalar-prefilter-final-20261022.txt` 修复（10 过 / 0 败）。当前该组为绿。
- **未跑全量测试，未跑真实规模 GUI。** 下面所有"已实现/未实现"均为静态源码结论。

### 0.2 必须先纠正的五处旧表述

| 原表述（位置） | 源码事实 | 证据 |
| --- | --- | --- |
| R03「view 中尚未出现 `request_canvas_assets` 正式接线，继续核对」 | **已接线**。续页、每批 32 条 GUI 安装、取消与旧回包防护都完整 | `prism/view.py:896,986,988-998`；`prism/window_workspace.py:750-752`；`prism/paged_canvas_loader.py` 全文；专项用例 `tests/test_paged_canvas_loader.py` 7 项全过 |
| R04「`open_workspace_page` 只换 canvas ID，不读目标记录」 | **已修**。切页会真实读目标页记录 | `prism/window_workspace.py:750-752` → `prism/paged_canvas_loader.py:46-50`（`scope.page` + `read_baseline`）；`:165-169` 三重身份校验（project_id / session 对象 / scope 对象）+ epoch 防旧回包覆盖 |
| R09「`read_board_extras` 仍全量 SELECT data 并 json.loads 每页」 | **基本解决**。改用 SQLite JSON 投影剔除 `$.content`；但源码 docstring 自陈 SQLite 侧仍需扫旧 JSON envelope，**首次建目录成本没有被消灭** | `prism/fileio/sql.py:943-963` `_workspace_page_headers` |
| R01 / R02「glb 未限类型」「空 scope 被 `or None` 变全量」 | **已修** | `prism/fileio/sql.py:377-381`（glb 类型限制）；`:310-312`（空 scope 保留空 tuple，按 `is not None` 区分） |
| R10「单页同类型超 1024 抛异常被吞成空候选」 | **已修**。改为 keyset 迭代 | `prism/project_open_scope.py:162-174` `iter_query`；调用点 `prism/project_search.py:260`、`prism/catalog_browser.py:66` |

### 0.3 反过来：旧文档低估或没写的三件事（这三件是真缺口）

1. **正式按需打开在生产环境 100% 不生效。**
   `prism/view.py:1258` 的 `PRISM_PAGED_OPEN` 环境变量闸门，全仓库**只有测试设置**（`tests/_verify_paged_open.py:137,158`、`tests/test_paged_open_preparation.py:22`），配置项与启动脚本均无此项。正式入口恒走 `prism/view.py:1272` → `_start_project_reader(filename, None)` → **全量读取**。
   另：`prism/view.py:1192-1246` 的 `_paged_open_scope` 是**死代码**（无任何调用点）；`prism/view.py:1103-1144` `_maybe_start_catalog_build` 在正式路径**不可达**（`resolve_scope` 的两个返回都写死 `pending=None`，见 `prism/paged_open_preparation.py:41,63`）。

2. **按需准备失败会留下"已打开的空工程"。**
   `prism/view.py:1255` 先 `clear_scene()`，`prism/view.py:1271` 返回 `True`，`prism/view.py:1304-1305` 因此把 `self.filename` 设成该工程；而失败分支 `prism/view.py:949-955` 只有一条 8 秒状态栏文字、**没有对话框**。触发条件：目录缺失/过期（`prism/paged_open_preparation.py:48-50`）、建库后仍 not ready（`:52-53`）、准备期间源被改写（`:51,62`）。用户看到的是"打开成功但空无一物"。

3. **部分工程里三条"永久删除"路径会让工程从此再也存不了。**
   - 资源树永久删除（`prism/widgets/trash_panel.py:324` → `prism/workspace_service.py:444`）：`self._domain_deletions`（`prism/partial_project.py:243`）在生产代码中**从未被写入**（只有初始化 `:243`、读取 `:524`、`discard` `:714`），于是 `prism/partial_domains.py:130-132` 抛 `PartialSaveConflict` → 之后每次保存都被拒绝。
   - 素材永久删除（`prism/widgets/trash_panel.py:236-261` → `prism/window_workspace.py:485-500`）：partial 模式设计不支持（`prism/fileio/partial_save.py:274-275` 明确抛错），但 GUI 按钮**未按 partial 状态禁用**（`trash_panel.py:122-123`）。
   - 脑图页永久删除（`prism/window_workspace.py:519-521`）：需要 `authorize_mindmap_clear()`（`prism/partial_project.py:385-390`），而该方法**只有测试调用**（`tests/test_partial_residency.py:278`），生产 0 调用点。
   共同特征：不是静默丢数据（都是**显式拒绝保存**），但 `session.invalid` 不置位，用户看不出原因，工程进入"再也存不了"状态。

### 0.4 十五项任务的当前状态

| # | 任务 | 状态 | 结论摘要 |
| --- | --- | --- | --- |
| 1 | 正式按需打开、续页、跨页加载 | **代码已接未验收，且默认关闭** | paged loader 完整实现并有 7 项专项用例；但正式入口被环境变量闸门挡住（0.3.1）；准备失败留空工程（0.3.2） |
| 2 | 查询、计数、刷新与聚合布局 | **部分** | 侧栏 4 个计数已走全库目录（`prism/widgets/media_library.py:405-411` → `prism/project_catalog.py:272-282`）；但 `prism/project_catalog.py:420-504` `query` 仍遍历 `self.asset_items()`（scene 活对象）→ **侧栏十万、列表一百** |
| 3 | 模块切换与编辑器初始化 | **未验收** | 本轮未做专项核；旧判定保留。已知 `prism/view.py:1315-1321` 的项目变更守卫可作后续切入点 |
| 4 | 远中近 LOD、清晰恢复、合成尖峰 | **部分** | 合成侧**已内层切片**（`prism/canvas_composites.py:301-302`）；**tile 侧 4ms 只在块之间检查**（`prism/canvas_tiles.py:291`），`_build()` 原子无内部预算（`:183-196`），且 `prism/canvas_tiles.py:386-389` 在 paint 里同步构建全部粗覆盖；主画布路径无切片能力（`prism/canvas_overview.py:18,126-140`） |
| 5 | 统一预算、有界驻留 | **部分** | 共享字节预算**真实可用**（默认 256MB、`PRISM_CACHE_BUDGET_MB` 可配、6 缓存注册、按优先级淘汰）；**页面/画布对象、目录代理、GPU、磁盘、dirty、临时文件完全无界**；`prism/resource_budget.py:120-127` `snapshot()` **生产无调用点**（不可观测）；**代码库中不存在租约机制**；`prism/image_decode.py:131` 的 shutdown 只接 `aboutToQuit`，`clear_scene`（`prism/view.py:420-469`）不取消待解码任务 |
| 6 | 媒体源延迟准备 | **部分** | `prism/source_store.py:62-76` `retain_archive_video` 流式化 + 指纹键 + 同源合并已接；**保存/导出仍同步物化**（`prism/items.py:1896-1933` 契约未变） |
| 7 | 全库搜索与修改覆盖 | **部分** | keyset 分页已接（见 0.2 R10）；但聚合列表、智能文件夹、以图搜图仍只搜已加载（见 0.4 第 2 项的 4 个调用点） |
| 8 | 部分加载保存保护 | **主体完成，有 3 处红** | 全仓库**不存在** `register_modified`；机制是"基线登记 + 保存时差分"（`prism/partial_project.py:326` `register_loaded` + `:469` `capture`），因此普通编辑**不需要逐入口登记**。真缺口是 0.3.3 的三条永久删除路径。**writer 永不退回全量**（`prism/view.py:1426-1431` 前置闸门 + `prism/fileio/sql.py:596-597` 兜底） |
| 9 | 跨页移动 | **主体完成** | 拖拽入口 + `authorize_canvas_move`（`prism/partial_project.py:392-411`）+ 目标存在/类型/回收站校验（`:413-442`）+ 未加载页（`:276-284`）+ 默认页（`:439-440`）+ 保存期间再移动（`:508`、`:444-465`）+ 撤销重做归属（`:409-411`）均已实现。缺：**无菜单/命令入口**（`prism/actions/actions.py` 无对应 action）；`pages_complete=True` 时"存在于 DB 但不在 `scene.workspace_pages`"的画布会被误拒（`:441-442`） |
| 10 | 原件、附件、删除与导出 | **部分** | 原件字节优先（`prism/fileio/export.py:330-337`）、可取消（`:321-326`）、文件名带 save_id 前缀避免覆盖（`prism/image_source.py:8-16`）。缺：**全选导出在部分加载下三处明确拒绝**（`prism/actions/export_workflow.py:183-188`、`:189-200`、`:298-302`）；无自动避让（逐文件询问，`:368-386`；对照 `prism/fileio/resource_export.py:414-429`）；存在"预览代替原件"降级路径（`prism/fileio/export.py:338-350`，已带 lossless 告警） |
| 11 | 保存期间新编辑与回执失败 | **主体完成，有 1 处红** | dirty 保持与 ack 只推进快照语义**已完成并接线**（`prism/view.py:1466-1468`、`:1346-1354`；`prism/partial_project.py:703-710`）。红：**自动保存（silent）下所有失败路径零界面提示**（`prism/view.py:1389`、`:1323-1327`），而 `prism/__main__.py:527-533` 每 tick 重试 → **静默无限失败循环**；ack 失败只有状态栏 12 秒提示，无模态 |
| 12 | 另存后源引用与目录重绑 | **部分** | `_prism_source`、附件、停放记录、会话基线、临时文件**已重绑**（`prism/partial_project.py:736-773`；`prism/partial_residency.py:128-141`；`prism/fileio/partial_save.py:495-509`）。红：**目录索引未重绑**——`prism/partial_project.py:747-773` `_rebind` 完全不碰 `view._open_scope`（其全部赋值点为 `prism/view.py:456,957,1207`）；旧文件未改动 → 目录仍 ready（`prism/project_catalog_index.py:141-146`）→ **另存后计数与搜索仍读旧工程**（`prism/project_catalog.py:177-197`） |
| 13 | 延迟正文与全局元数据 | **大部分完成** | 目标补载 + 128 批次 + 保留显式清除已做（`prism/fileio/sql.py:1179-1236`）；页面正文用 JSON 投影（`:943-963`）。仍需：正文异步化、保存前不全量补载、全局元数据（标签体系/查询配置/board 状态）差异覆盖 |
| 14 | 图片后端与兼容 | **未完成** | 遗留 `.bee` 打开失败仍在：`test_view.py::test_on_action_open` 本轮复现失败 |
| 15 | 正式入口验收 | **未完成** | 真实规模数据在 **`artifacts/scale-audit/`（2026-10-21）**，不在 `full-audit`；`full-audit` 的 2026-10-22 批次明确声明"未跑真实十万图 GUI"（`artifacts/full-audit/CURRENT_STATUS.md:11,15,87`） |

### 0.5 保存线六项的真实状态（对用户清单的校正）

| 用户清单项 | 核对结论 |
| --- | --- |
| 部分加载下所有编辑入口的保存范围登记 | **普通编辑已覆盖**（基线登记 + 保存时差分，见 0.4 第 8 项）。真缺口是**永久删除三条路径**（0.3.3），不是登记不全 |
| 跨画布移动、撤销重做、保存期间再次移动 | **主体已实现**（0.4 第 9 项）。剩：无菜单入口；`pages_complete=True` 时未在 `workspace_pages` 里的画布会被误拒 |
| 未加载资产的多选／全选原件导出 | **未实现**，三处明确拒绝（0.4 第 10 项）。这是保存线里最大的一块 |
| 保存期间新编辑、回执失败的正式界面闭环 | **dirty/ack 语义已完成**；**自动保存失败路径无提示**（0.4 第 11 项） |
| 另存后全部源引用、目录和临时资源重新绑定 | **源引用/附件/停放/临时资源已重绑**；**目录索引未重绑**（0.4 第 12 项）——这正是"另存后仍读旧工程"的残留路径 |
| 延迟正文异步读取、避免保存前全量补载、全局元数据完整覆盖 | **大部分完成**，剩三项（0.4 第 13 项） |

### 0.6 确定的优化方向

**原则不变**：性能优先，不扩保存框架，不做无关新功能。但**"继续只修零碎预览问题"这条路已经走到头**——2026-10-22 全天批次（`artifacts/full-audit/CURRENT_STATUS.md` 07:12—09:14）全部是刷新/取消/预览修复，`目录驻留下一切点-20261022.md` 也已判定"多遍 O(N) + 强引用全量驻留 + 末端未分片的 GUI 原子安装"是当前主矛盾。下面四组按此重排。

#### 第一组：解锁正式按需打开（不做这组，下面全部白做）

| ID | 改动 | 位置 |
| --- | --- | --- |
| D1 | 环境变量闸门改成 QSettings 开关（默认开），env 保留为强制覆盖/回退；同时删除死代码 `_paged_open_scope` | `prism/view.py:1258`、`:1192-1246` |
| D2 | 准备失败必须显式报错并撤销"已打开"：失败时不设 `self.filename`、不留下空场景；"取消"与"失败"分文案 | `prism/view.py:944-955`、`:1255`、`:1271`、`:1304-1305`；`prism/paged_open_preparation.py:44-62` |
| D3 | 聚合口径统一到全库目录 + dirty overlay：`query` 增目录分支；改 4 个只搜已加载的调用点 | `prism/project_catalog.py:420`；`prism/widgets/media_library.py:820`；`prism/widgets/asset_browser.py:302`；`prism/visual_search_controller.py:113`；`prism/__main__.py:987` |
| D4 | 首次建目录给真实进度 + 用户可取消（`build()` 已支持 `on_batch`，只是没传） | `prism/paged_open_preparation.py:44-53`；`prism/project_catalog_index.py:248-249`；`prism/view.py:1268` |
| D5 | 统一"目录不可用"的 UI 契约：素材库结果列表不再静默返回空 | `prism/widgets/media_library.py:820`（对照 `prism/widgets/asset_browser.py:363`） |

#### 第二组：消除界面线程长阻塞

| ID | 改动 | 位置 |
| --- | --- | --- |
| D6 | 末批同步安装分片：`prepare_layout / stage_canvas / publish_canvas`，保留 `AssetModel.replace` 的一次发布契约。**注意 `compact_layout` 模块级 LRU 无锁**（`prism/compact_layout.py:48-86`），需专用串行布局队列或显式互斥 | `prism/widgets/asset_browser.py:34-41`、`:482-512`；`prism/widgets/asset_canvas.py:639-712` |
| D7 | 拆单 tile 长任务：块间 4ms 不等于单 tile 有界；paint 里的同步粗覆盖构建要改；主画布需具备切片能力或明确降级 | `prism/canvas_tiles.py:183-196`、`:291`、`:386-389`；`prism/canvas_overview.py:18`、`:126-140` |
| D8 | 目录驻留真正有界：可见窗代理 + 稳定 ID 索引，替代全量强引用 proxy；附件 blob 缓存加上限；画布对象明确记账 | `prism/widgets/asset_browser.py:502-506`；`prism/fileio/attachments_store.py:65`；`prism/partial_residency.py:1-6`、`:29` |
| D9 | 关闭工程释放资源：解码队列随 `clear_scene` 取消，预算加 per-project 作用域 | `prism/image_decode.py:131`、`:149-162`；`prism/view.py:420-469`；`prism/resource_budget.py:34-60` |

#### 第三组：安全门（发布部分加载前必须完成，不得被"性能优先"跳过）

| ID | 改动 | 位置 |
| --- | --- | --- |
| D10 | 三条永久删除路径：接上生产授权，**或在 partial 模式下直接禁用按钮**。当前"点了永久删除 → 之后每次保存都被拒绝" | `prism/partial_project.py:243`、`:385-390`、`:524`；`prism/widgets/trash_panel.py:122-123`、`:236-261`、`:324`；`prism/window_workspace.py:485-500`、`:519-521` |
| D11 | 另存后目录重绑：`_rebind` 补 `view._open_scope` / `ProjectOpenScope` / 目录索引，并为新工程触发后台建库 | `prism/partial_project.py:747-773`；`prism/view.py:456,957,1207`；`prism/project_open_scope.py:59` |
| D12 | 自动保存失败必须可见：silent 路径补提示并停止无限重试；ack 失败改模态 | `prism/view.py:1389`、`:1323-1327`、`:1339-1342`；`prism/__main__.py:527-533` |
| D13 | 未加载资产的多选/全选原件导出：接 `scope.iter_query()` + `details()` 流式取源；统一浏览器选中与 scene 选中；复用 `_unique_name` 做自动避让 | `prism/actions/export_workflow.py:183-188`、`:189-200`、`:298-302`；`prism/export_service.py:191-217`；`prism/fileio/export.py:362-386`；`prism/fileio/resource_export.py:414-429` |

#### 第四组：验收（当前几乎没有真实规模证据）

| ID | 改动 | 位置 |
| --- | --- | --- |
| D14 | 补真实规模矩阵：100/1000/1万/10万 × **真实素材**（现只有 100/1000/2200/10000 的合成色块图）；5000 材质 GPU；10万工程保存重开与菜单/右键/多选导出；不同 DPI/冷缓存；长会话内存 | `artifacts/scale-audit/`；`artifacts/full-audit/十万图性能定位报告-20261021.md`；交接书 §六 第 15 项 |
| D15 | 预算可观测：`snapshot()` 接到生产遥测 | `prism/resource_budget.py:120-127` |

### 0.7 验收现状（真实规模数据在 `artifacts/scale-audit/`，2026-10-21）

| 指标 | 是否有实测 | 数据 | 达标 |
| --- | --- | --- | --- |
| 重复进入未变范围 → 全量重建/排列 = 0 | **无实测** | 仅代码层声明 | 无法确认 |
| GUI 同步工作 ≤16ms | 有实测，**未达标** | pan median 22.04 / p95 66.64ms；zoom median 18.83 / p95 38.78ms；滚轮 max 1060ms；heartbeat **max 7687ms** | 否 |
| 焦点清晰 ≤0.5s / 2s | **无独立实测** | 仅有近景 11–13 张全视口恢复 0.08–0.15s，口径不符 | 无法确认 |
| 全视口清晰 ≤2s / 5s | 有实测，**中景未达标** | 0.15 倍/1964 张 = **9.881s**；0.35 倍/331 张 = 0.461s；3 倍/12 张 = 0.103s | 中景否 |
| 每类字节预算 + 固定工作集稳定 | **无实测** | 机制存在但 `snapshot()` 生产无调用点，无长会话多轮数据 | 否 / 无法确认 |
| 打开直到就绪 | 有实测，**未达标** | 32.8–33.0 秒（100k 图） | 否 |

**压力副本口径提醒**：`artifacts/scale-audit/fixture.json:4` 为 `"mode": "duplicates-of-existing-real-assets"`，原图仅 2744 张（`:9`）。**记录规模 ≠ 解码负载**，两者必须分开报告，不得用十万条记录冒充十万张独立图片。

### 0.8 第二波实施结果与失败台账（2026-10-22 12:52，**以本节为准**）

#### 0.8.1 D1–D15 实施状态

| ID | 状态 | 落地位置与证据 |
| --- | --- | --- |
| D1 | **完成（默认仍 off）** | `prism/view.py` 新增 `_paged_open_enabled()`：`PRISM_PAGED_OPEN` 保留为硬覆盖，否则读 QSettings `Project/paged_open`，**默认 `'off'`**。死代码 `_paged_open_scope` 未清（见 0.8.4） |
| D2 | **完成** | `open_from_file` 按需路径失败时返回 `False`；`self.filename` 只由成功后的 `_finalize_loaded_project` 设置；失败走 `QMessageBox.warning` 并撤销"已打开"。**取消**另用 `_paged_open_cancelled` 守卫（只状态栏，不弹框），两者都禁止保存 |
| D3 | **完成** | `ProjectAssetCatalog.query` 增目录分支并与 live/parked overlay 合并；4 个只搜已加载的调用点已切（`media_library` / `asset_browser` / `visual_search_controller` / `__main__`） |
| D4 | **完成** | 建目录进度经 `scope.build(on_batch=)` 接状态栏；取消与失败分文案 |
| D5 | **完成** | 目录不可用不再静默返回空（4 个调用点统一提示） |
| D6 | **开发中（主体未做）** | `prism/compact_layout.py`、`prism/fileio/attachments_store.py` 有改动，但**分片安装主体所在的 `prism/widgets/asset_canvas.py` 一行未改** |
| D7 | **开发中（引入回归）** | `prism/canvas_tiles.py`、`prism/canvas_overview.py` 有改动；`_work()` 把 `break` 改成 `continue`，**违反两条既有契约**（见 0.8.3 A 类） |
| D8 | **开发中（主体未做）** | 同 D6；有界缓存 + LRU 驱逐未见落地 |
| D9 | **开发中（主体未做）** | `prism/image_decode.py`、`prism/resource_budget.py`、`prism/video_thumbnails.py` 有改动，但**未引入新失败**；`clear_scene` 取消解码队列、per-project 预算作用域**未做** |
| D10 | **完成** | 新增 `prism/partial_project.py::authorize_resource_purge` + `_refuse_unwritable_purge` + `mindmap_clear_needed`；资源树永久删除受支持且可保存；素材永久删除在 partial 下**显式禁用按钮并说明原因**；脑图清空经授权后受支持 |
| D11 | **未开始** | `prism/partial_project.py` 的 `_rebind` **仍不碰 `view._open_scope`**；另存后仍读旧工程。**第二波 N3 未落盘任何相关代码**，原定钩子接口**不存在** |
| D12 | **完成** | `_capture_partial_save` 在 silent 时也 `logger.warning` 并记 `view._paged_save_refused = (trace_time.time(), reason)`；回执失败改模态警告；自动保存加**连续失败退避与计数** |
| D13 | **完成** | 解除 `prism/actions/export_workflow.py` 三处明确拒绝（`:183-188` / `:189-200` / `:298-302`）；来源改为目录 + overlay 合并（dirty 优先）；分页取源、保留原件字节、自动同名避让（复用 `_unique_name`）、可取消 |
| D14 | **未开始（但条件已具备）** | **真实工程 `E:\Prism-stress-100k\Prism-100k-v1.prism` 确实存在：55,633,186,816 字节（≈51.8 GiB），2026-10-21；E 盘剩余 1.08 TB** → 现在可以真跑。注意该工程仍是 2744 张原图复制出的十万条记录，**记录规模 ≠ 解码负载** |
| D15 | **未开始** | `prism/resource_budget.py::snapshot()` 生产仍无调用点 |

#### 0.8.2 磁盘实况（逐文件 MD5 对比 `artifacts/rollback-20261022-112713/`）

`prism/` **20 个文件**与第二波前不同（新增 0 / 删除 0）：第一波 13 个 + 第二波 7 个（`canvas_tiles.py`、`canvas_overview.py`、`compact_layout.py`、`fileio/attachments_store.py`、`image_decode.py`、`resource_budget.py`、`video_thumbnails.py`）。

`tests/` **改 2 新增 6**：改 `test_paged_open_preparation.py`、`test_view.py`；新增第一波 4 个 + 第二波 `test_canvas_tile_budget.py`（**只有 1 个用例且是带 `print` 的调试草稿**）、`test_canvas_move_entry.py`（总监树，20 用例）。

#### 0.8.3 失败台账（21 条，三类，证据均为 junitxml）

证据：`artifacts/full-audit/wave2-baseline-20261022.xml`（当前）、`wave2-prebaseline-20261022.xml`（第二波前对照）、`orphan-tests-20261022.xml`、`wave1-recheck-20261022.xml`。

**A 类 6 条 —— 第二波引入，全部来自 `prism/canvas_tiles.py`**：
`test_canvas_tiles.py` 4 条（`test_zoom_uses_parent_cover` / `test_existing_cover_remains_while_group_previews_are_preparing` / `test_existing_remote_cache_cannot_leave_new_viewport_black` / `test_prefetch_does_not_evict_the_visible_cover`）+ `test_asset_tile_slices.py::test_cold_asset_paint_never_calls_atomic_scene_renderer` + `test_asset_browser.py::test_dense_material_canvas_keeps_frame_until_visible_previews_are_ready`。
**根因（已定位到行）**：`prism/canvas_tiles.py::_work()`（约 361-362 行）把 `break` 改成 `continue`，让一个 GUI 回合在 4 ms 预算内**无限推进切片**，与两条既有契约冲突（"一个回合最多画 128 张卡片"、"未就绪时不得把 tile 标记为 stale"）。
**同时它修好了一条**：`test_canvas_lod.py::test_3000_record_overview_and_zoom_out_use_thumbnails_without_original_decodes`（基线失败 → 现通过）。
→ **净账 -5 条失败**。回滚 `canvas_tiles.py` 可让 A 类归零但那条修好的会退回；**推荐保留并补完**（补一个每回合 ≤128 张卡片的**计数上限** + stale 语义修正），这本来就是 D7 该做的事。

**B 类 5 条 —— 第二波前就存在，不是本轮引入**：
`test_canvas_composites.py` 2 条、`test_canvas_lod.py::test_material_display_only_builds_one_qt_layer`、`test_canvas_scalability.py::test_overloaded_decoder_retries_all_visible_items`、`test_asset_layout_contract.py::test_crop_data_change_updates_hit_geometry_and_spatial_index`。

**C 类 10 条 —— 总监树新增 `tests/test_canvas_move_entry.py`**：
- **1 条是测试文件自身写法错**：`test_the_command_is_registered_on_the_window_with_a_shortcut` 用了 `QtGui.Qt.ShortcutContext`——PyQt6 里 `Qt` 在 `QtCore` 不在 `QtGui`。改为 `QtCore.Qt.ShortcutContext`。
- **9 条是移动命令统一提前返回 False**（`move_selected_to_canvas('B')` / `('default-canvas')`）。已排除：`_setup_move_to_canvas()` 确实在 `prism/widgets/media_library.py:250` 被调用；`save_id` 是普通实例属性，`hasattr` 过滤不会误杀。**早退分支尚未逐条定位**，候选：`items` 为空 / `movable` 为空 / `_canvas_destination_allowed` / `authorize_canvas_move` 抛 `PartialSaveConflict`。

**第一波集成回归复跑（17 文件 / 351 用例）= 350 passed / 1 failed** —— 那 1 条即 A 类第 6 条。

#### 0.8.4 本轮确认的既有缺陷（必修）

- **`prism/partial_project.py:526`**：`if row is None or self.pages_complete:` —— 只要 `pages_complete=True`，**真实存在于 DB 的画布也被判为"不存在"**。应为 `if row is None and self.pages_complete:`。这直接卡住"按页打开 + 移动到未加载画布"。（原交接书 0.4 第 9 项记为 `:441-442`，行号已漂移。）
- **`prism/view.py:1233` `_paged_open_scope` 死代码** 与 `_maybe_start_catalog_build` 在正式路径不可达（`resolve_scope` 两处都写死 `pending=None`）。
- **`prism/widgets/media_library.py:57` `MOVE_TO_CANVAS_SHORTCUT = 'Ctrl+Shift+C'`** 是第二波新增（基线里没有），快捷键无冲突已由用例验证。

#### 0.8.5 运行时约束（**不记住就会再踩**）

1. **后台子智能体在父会话回合结束时会被自动 cancel**。已连续三轮验证。唯一可靠解法：用 `task` 的 `run_in_background=false` **阻塞派工**，或在同一回合内持续 `task_output` 轮询到全部交付。
2. **被派生的子会话拿不到 `task` 工具**。已实测：`perf-acceptance-director`（自定义角色）**与 `mavis` 角色的派生子会话**都**没有** `task`。→ **组长必须是用户手动新开的根会话**，子智能体当不了组长。
3. 每个父会话最多 4 个子智能体；组长根会话各带 4 人 → 总并发 = 4 × 组长数。机器 32 逻辑核。
4. **pytest 进程可能在退出阶段段错误崩溃**（0xC0000005），吞掉汇总行。加 `--junitxml="<绝对路径>"` 可保住结果，再用 `python -c` 解析 XML 取失败清单。

#### 0.8.6 下一顺序（本轮定）

1. **先修 `prism/canvas_tiles.py`**（A 类 6 条的唯一来源）：补每回合计数上限与 stale 语义；同时清掉 `tests/test_canvas_tile_budget.py` 那个调试草稿，换成真用例。
2. **再修 `prism/partial_project.py:526`**（一行），并定位 C 类 9 条移动用例的早退分支 + 改掉测试文件的 `QtGui.Qt` 写法错。
3. **然后做 D11 另存重绑**（`partial_project._rebind` 补 `view._open_scope` / `ProjectOpenScope` / 目录索引 + 为新工程触发后台建库），并在 `prism/view.py` 侧接线。
4. **再做 D6/D8/D9 主体**（分片安装、有界驻留、关闭释放资源）。
5. **然后开真实规模验收 D14**：`E:\Prism-stress-100k\Prism-100k-v1.prism`（51.8 GiB）只读跑分，**记录规模与解码负载分开报**。
6. 按需打开的正式放量**最后做**，且默认仍 off，直到 D10–D13 全部验收。

> **协调区**：`coordination/BOARD.md` 是唯一权威现状；`coordination/leaders/` 下有组长章程与 A/B/C 三份派工单。

---

## 一、接手者先看这一页

### 1. 当前到底做到哪里

有不少基础代码和局部优化可以复用，但**没有证明十万图正式界面已经能顺畅打开、跨页、切换、放大恢复清晰和安全保存**。

已有：目录索引、按ID读取、部分保存、异步导航、逻辑图片记录、自绘与LOD、局部缓存、材质摘要、后台视频解码等。未闭环：正式按需打开和续页、全部资产的聚合呈现、完整dirty覆盖、统一内存预算，以及真实界面的规模性能。

最新修代码这一轮被用户消息多次中断，存在工作中的修改。**文件存在不等于接口已接；代码已写不等于回归已过；旧测试全过不覆盖后来的修改。**

### 2. 先做什么、后做什么

共 **15个执行任务**，顺序如下。它们对应旧编号0—8的九个领域，不是要求再新造15套系统。

| 优先顺序 | 任务 | 要解决的主要问题 |
| --- | --- | --- |
| 1 | 正式按需打开、画布续页与跨页加载 | 十万图打开慢，只加载首批、换页空白 |
| 2 | 查询、计数、刷新与聚合布局去重复 | 全部图片/未标签/未分类/默认画布卡顿、数量不一致 |
| 3 | 模块切换与编辑器按需初始化 | 文档/脑图/材质/画布切换阻塞、闪烁、旧回包覆盖 |
| 4 | 远中近LOD与焦点清晰恢复 | 缩小再放大长期模糊、合成绘制尖峰 |
| 5 | 统一内存预算与有界驻留 | 越用越大、后台队列/纹理/页面对象无限积累 |
| 6 | 媒体源延迟准备及预览复用 | 视频打开/首次播放大I/O、GUI同步读取大文件 |
| 7 | 全库搜索、dirty覆盖与未加载跳转 | 搜不到未加载数据、超过页上限漏结果 |
| 8 | 部分加载保存的完整保护 | 未加载行/未知字段被误清或误删 |
| 9 | 跨页移动、默认画布、撤销重做 | 归属错误、消失、重复、无法保存 |
| 10 | 原件和附件生命周期及导出 | 转码、丢失、误删、同名覆盖、只导出可见项 |
| 11 | 保存期间编辑与回执失败处理 | 新编辑被标成已保存、界面假成功 |
| 12 | 另存后源引用与目录/缓存重绑 | 继续读旧工程、后续保存错文件 |
| 13 | 延迟正文与全局元数据的完整收口 | 空文档、保存前旧正文覆盖新编辑、隐藏全量读取 |
| 14 | 可选图片后端与兼容缺陷 | pyvips是否值得采用、旧格式失败归因 |
| 15 | 正式入口验收与架构规范收尾 | 证明实际效果，列明尚未解决项 |

**安全门例外：**任务8—13中，凡会导致当前按需加载丢数据、空正文或编辑被覆盖的问题，必须在任务1—3启用相应功能前做最小必要修复。其余保存功能放后面。不能拿“性能优先”当理由发布不安全的部分加载，也不能借安全门继续扩展无关保存功能。

### 3. 当前代码需要保留的关键判断

- `PRISM_PAGED_OPEN=1`仍是实验开关，默认关闭。不要未经闭环就默认启用。**（2026-10-22 11:30 核实：该变量全仓库只有测试设置，正式入口恒走全量；见 §〇 0.3.1、0.6 D1）**
- 目录缓存缺失/失效时旧逻辑仍可能回退全量打开，不能算四态性能已解决。**（核实：正式路径已改为"后台建目录→再打开"，见 §〇 0.6 D2/D4）**
- 一次按100个ID读取成功，不等于整个十万图工程可用。
- 一份新建的后台加载模块如果没有被view和工作区调用，仍属于未接线。**（核实：`paged_canvas_loader` 已接线，见 §〇 0.2）**
- 正文补载已开始修"只读目标页、不覆盖已存在content"，尚需针对性检查。**（核实：已实现，见 §〇 0.4 第 13 项）**
- 布局键、重复空间索引、分层pack的修改正在落地，不能冒充性能验收结果。**（核实：已实现，但无规模实测，见 §〇 0.4 第 4 项、0.7）**

## 二、保护范围、文件和真实证据

### 1. 原工程与压力副本

| 对象 | 位置/约束 |
| --- | --- |
| 用户原工程 | `C:\Users\admin\Desktop\测试.prism`，只读 |
| 原工程预期SHA256 | `e58a9acf676a4875a6e3e358b4234f42a10458a2ca95238c2403a47aad861b74` |
| 真实审计副本 | `artifacts\full-audit\Test-project-audit.prism`，2766图元、2753原件、1221附件、67页、332材质、9视频 |
| 十万图压力副本 | `E:\Prism-stress-100k\Prism-100k-v1.prism`，100000图片、1000画布、5000材质、500文档、500脑图 |
| 压力数据限制 | 来源是真实资产复制，不等于十万张各不相同的图片；记录规模和解码压力分开评价 |
| 所有写入验证 | 新的专用副本，禁止覆盖原工程、原贴图或旧失败证据 |

源文件、贴图、附件保存和导出必须保持原始字节。派生预览可重建，不能用预览代替原件。原工程已有个别空/损坏附件，保留事实，不得改原工程伪造修复。

### 2. 需要阅读的资料

- `docs/PRISM_HANDOFF_OPTIMIZATION_20261021.md`：原完整交接书正文。
- 桌面同名中文交接书的附录F：上个AI自述，需要结合代码核验，不能直接照抄完成状态。
- `docs/PRISM_PERFORMANCE_PLAN_20261021.md`、`docs/PRISM_PERFORMANCE_EVIDENCE_20261021.md`。
- `artifacts/full-audit/CONTINUATION.md`、`CURRENT_STATUS.md`：部分内容可能滞后，当前源码和本文最新状态优先。
- `artifacts/full-audit/验收报告-20261021-验证轮.md`。
- 八份报告：任务5-8阶段报告、GUI打开分解与视频取证、任务8-资源与复用、任务8-30轮资源曲线、四项因果与规模取证、U1U2U3取证、A1A2A3取证、A1A2A3重测，均为20261021日期、位于full-audit目录。

### 3. 测试记录的准确口径

现有 `artifacts/full-audit/test_reports/*/summary.json` 按每个目标最后一轮结果去重后，核对到46个不同测试文件、889通过、1失败、7跳过。这是已有记录汇总，**不是本次又运行的测试，也不是当前所有工作中修改的测试成绩**。

剩余 `test_view.py::test_on_action_open` 的失败，不能直接写“既有回归、非本轮引入”：更早 `partial-multipage-receipt-20261021.txt` 有 `test_view.py 136 passed`。现在只能标“当前失败，原因及引入时间待查”。环境/时序也可能影响，不能仅凭前后结果直接断定哪一行引入。

## 三、最新发现：不仅是没验收，还有实际缺口

> **本节状态列已被 §〇 修正**：R01、R02、R03、R04、R09、R10 已完成或已接线；R05、R12、R13、R14、R15 部分完成；R16 仍成立且比原文更严重（tile 侧无内层预算，且 paint 内同步构建全部粗覆盖）。下表保留原始问题描述，状态以 §〇 为准。

| 编号 | 核实的问题 | 当前进度 | 接下来怎么修 |
| --- | --- | --- | --- |
| R01 | scoped读取的embedded_glb查询曾读取scope内全部图片BLOB | **已修**（§〇 0.2） | 确认普通图片不读取原件BLOB；混合类型不重复加载 |
| R02 | 空scope曾被`or None`变成全量打开 | **已修**（§〇 0.2） | 空集合只能加载零项，仍标识部分范围，不能全量回退 |
| R03 | `_paged_open_scope`只取pixmap首256项，无后续页闭环 | **已接线**：续页/取消/旧回包防护完整，7 项专项用例全过；但该函数本身是死代码，正式入口另走 `paged_open_preparation`（§〇 0.2、0.3.1） | 有界异步续页，跨页按需加载，取消旧任务，登记基线 |
| R04 | `open_workspace_page`原来只换canvas ID，不读目标记录 | **已修**：切页会真实读目标页记录，三重身份校验防旧回包（§〇 0.2） | 进入目标前/期间请求其数据，旧回包不覆盖当前页 |
| R05 | 聚合列表仍通过`ProjectAssetCatalog.query`遍历已加载资产 | 未闭环 | 接全库目录摘要和dirty覆盖；不能侧栏十万、列表只有100 |
| R06 | 默认关闭实验开关；目录失效仍回退全量 | 未解决 | 先保持安全，随后把无目录/中断/源变化变成可取消渐进流程 |
| R07 | 正文只在保存前补载，打开文档/脑图未补载 | panel getter已加目标补载，未回归 | 核对默认页/普通页/导入/同页替换；正文只取目标 |
| R08 | `_materialize_workspace_pages`曾全量读并覆盖所有页content | 主线已改按ID批次，仅补缺失content；未回归 | 明确空串/None是用户清除，保留编辑和未知字段 |
| R09 | 分页read_board_extras虽然不安装正文，但仍全量SELECT data并json.loads每页 | **基本解决**：改用 SQLite JSON 投影剔除 `$.content`；但首次建目录成本仍在（§〇 0.2） | 目录存小元数据，正文查询按页；不能"丢弃正文"冒充"不读取正文" |
| R10 | 单页某类型搜索超过1024时抛异常，被调用方吞成空候选 | **已修**：改为 keyset 迭代，搜索与目录浏览器均已接入（§〇 0.2） | 所有匹配分页读完或真实分页展示，不漏结果、不假空 |
| R11 | 目录统计没有完整dirty覆盖、已删除页/孤立归属口径不齐 | 开发中 | 同一目录查询+overlay，计数、搜索、列表、导出一致 |
| R12 | 视频`_ensure_materialized`在GUI读整个BLOB、落盘并计算指纹 | 未解决 | 后台流式提取、单源并发合并、可取消、租约管理 |
| R13 | 布局键取错project来源、仅采样64项、漏真实排序/分组变化 | 子智能体已开始修可靠键，未验收 | 使用真实project/epoch和完整有序结果/几何/分组版本 |
| R14 | 布局缓存命中仍重建空间索引、合成并重置相机 | 已开始增加已应用key快速返回，未验收 | 未变布局直接复用；几何/内容变化准确失效 |
| R15 | pack父层可能把大量分组矩形一次送入rpack | 已开始改递归分层，未验收 | 每次实际pack输入有界，结果完整无重叠 |
| R16 | 4ms/每帧4块的预算仅在tile之间检查，单tile仍可能卡很久 | 未解决 | 拆单项工作或专用渐进合成；不能只减每帧块数宣称尖峰解决 |

以上是源码审阅发现和工作中状态，不是新运行整个工程后的缺陷统计。完成后逐项更新；不要永久保留“正在修”却不落地。

## 四、执行任务1—7：性能代码第一优先级

### 第1项：正式按需打开、续页、跨画布加载

**问题：**全量准备让当前页等待；试验路径只读首批，其他页仍可能空；空scope/无目录回退会把有界读取变成全量。

**复用：**ProjectCatalogIndex、ProjectOpenScope、ProjectLoadService、ImageRecord、SceneLoadScheduler、PartialProjectSession、正在编写的paged_canvas_loader。先检查现有接口，不重复新造数据仓库。

**实现顺序：**

1. 修R01/R02并核对scope内图片/文本/视频/GLB语义，不重复native记录，不把缺源记录悄悄换成正常项。
2. 接正式打开链：结构摘要→有效目标页→目录取真实ID→有界详情→GUI分批安装→会话登记。
3. 同一页超过首批要续页，cursor必须推进；没有更多数据才complete，不能靠“取了256张”就算整页完整。
4. 点击其他画布走同一加载服务；不能通过重新load_prism初始化整个scene来增量加载。
5. 工程epoch、页面generation、取消检查贯穿后台读取与GUI安装；快速A→B→C不应用B的晚回包。
6. 不覆盖dirty canonical；加载前核对来源与基线，已安装ID去重。
7. 单页超大按视口渐进处理；当前页可操作、首帧、全视口清晰、后台目录完成分开记录。
8. 无目录/目录中断/源变更：可取消、有进度、可恢复；首次旧格式建目录成本如实报告，不伪空页、不假首屏完成。
9. 记住上次合法页面；不能加载了preferred页，随后_reload_workspace_pages又强制第一画布和全部视图。

**完成标准：**正式入口真正调用新路径；有索引当前100图页不先加载其他999页详情；超过256项可继续显示；换页有内容；取消/旧结果不混数据；普通编辑可保存重开。孤立scope接口快不算完成。

### 第2项：查询、计数、刷新与聚合布局

**问题：**同一动作多次扫描/计数/刷新；不变范围反复创建卡片、布局、空间索引；列表只见已加载资产。

**复用：**目录统计、资源树ID/后代索引、材质摘要revision、asset_browser定时器去重、AssetCanvas自绘、compact_layout和空间索引。

**改法：**

- 同project/epoch/revision和相同条件，复用查询结果；标签/评分/删除/范围/排序等所有条件必须进入键。
- 摘要、详情、原件分离；列表用摘要，不为显示标题取原件。
- 全部/未标签/未分类/默认画布统一目录+dirty覆盖，未知不能显示可信0。
- 有界卡片记录窗口或空间分组，不为十万项创建十万重型图元。
- 几何改变才重排/更新空间索引；标题备注更新文本，像素完成只使相关tile失效。
- 修R13—R15；同已应用布局直接返回，保留相机和手工位置。
- 自动布局按稳定分组局部更新，不能每加载下一批全库跳动。
- 紧贴现有筛选/排序语义，不用抽样身份hash冒充可靠版本。

**完成标准：**重复进入不变范围全量rebuild/arrange为0；正确失效后标题/标签/crop/排序/归属仍正确；侧栏数量、逻辑结果、全选导出一致；未加载不代表不存在。

### 第3项：模块切换与编辑器初始化

**复用：**现有request_navigation、提交屏障、latest-intent、编辑器snapshot epoch、按首次显示启动WebEngine的逻辑。

**改法：**编辑提交异步完成后再进目标；导航最新优先，保存请求不能被导航覆盖；目标加载保留合法旧画面/局部进度，不重建整窗；只创建当前需要的文档脑图编辑器；晚回包检查内容版本。

**注意：**NavigationSequence、navigation_ready等新类/函数目前有定义，不代表所有真实调用点都已使用。逐个核对打开、搜索跳转、导入、双击、关闭。

**完成标准：**画布↔文档↔脑图↔材质快速切换不长时间锁UI，最终目标正确；离开前修改不丢；空内容和同页导入不会被旧快照恢复。编辑器数量有界，而非访问多少页就永久保留多少引擎。

### 第4项：远中近LOD、清晰恢复、合成尖峰

**复用：**lod.py、viewport_items、canvas_tiles/composites/overview、自绘卡片，不重写Qt渲染。

| 规模 | 数据策略 | 远景/中景/近景要求 |
| --- | --- | --- |
| 100 | 小摘要可整体准备，避免额外复杂性 | 远可真实合成，中景清晰小预览，近景按像素恢复 |
| 1000 | 稳定分组+可见范围准备 | 分组合成、局部细化、焦点优先 |
| 1万 | 目录分页/空间查询 | 层级合成，视口相关细化，不预解码全库 |
| 10万 | 有界摘要窗口、任务、结果和驻留 | 远中近同一架构，近处只见两张也必须清晰 |

**改法：**投影像素×DPI决定LOD，考虑crop/旋转；优先鼠标焦点，然后全视口，最后有限预取；过期任务让位；相同源版本与规格请求合并；局部失效；拆单tile长任务，不只给外层循环加4ms判断。

**完成标准：**缩小再快速放大，焦点和全视口自动清晰；期间有真实图像覆盖，无白块/长期马赛克；框选、多选、裁剪、层级、撤销等正确。测试只能观察，不替UI请求高清。

### 第5项：统一预算、有界驻留与资源释放

**问题：**多个缓存各管各的，页面/逻辑对象/纹理/队列可能无限累积。

**复用：**现有缓存及部分资源释放、PartialAssetResidency安全过渡。它目前保留canonical，不是最终内存有界。上个AI提到的resource_budget代码若未落盘或未接线，不算实现。

**改法：**按字节预算管理图片、tile、composite、材质纹理、编辑器、解码中间结果、临时源；可见工作优先，预取先退；限制队列和安装结果；干净详情可卸载，dirty保存成可恢复轻量修改，不能直接丢；关闭工程释放租约并取消任务。

**完成标准：**固定工作集进入稳定区间，递增页面仍受预算约束；内存/GPU/临时文件归属明确；不靠每帧淘汰可见内容造成重解码抖动。短30轮不能证明没有泄漏。

### 第6项：视频及其他媒体延迟准备

**当前：**打开时登记内嵌视频源的改造有用；但首次物化仍在GUI整块读BLOB、写盘和计算指纹，可能把卡顿推迟到首次播放。

**复用：**PyAV、已有有界QThreadPool、source_store、流式retain_stream和源标识。不再做“把首帧解码搬后台”，因为它本来就在后台。

**改法：**实际需要路径时后台流式提取；同源请求合并；可取消并清理半成品；预览按源ID/版本/规格缓存；租约到期释放临时文件；采非黑帧策略先核对实际帧数再改。

**完成标准：**打开不提取无关视频；首次准备不在GUI同步读大文件；预览可复用；播放、保存、导出仍用内嵌原始字节，不能改为依赖外部旧路径。

### 第7项：全库搜索与修改覆盖

**当前：**keyset查询/Scope接口已开始写；catalog计数overlay、聚合查询和UI端到端尚未完整接入。

**复用：**search_candidates/trigram、ProjectOpenScope、search_controller、统一导航。不要再建第二套权威资产集合。

**改法：**稳定cursor分页；单页匹配超1024不能抛错后假装无结果；中文/Unicode大小写/字面%和_语义正确；索引候选后精确过滤；磁盘摘要+新增/修改/移动/删恢复overlay；查询、计数、导出统一；点击未加载目标按ID加载并定位。

**完成标准：**未加载图片和已有支持范围的文档/脑图/材质内容可搜；保存前后结果一致；大量同页匹配不漏；取消不会回写旧结果；索引不可用时明确准备状态，而不是悄悄只搜100张却声称全库无结果。

## 五、任务8—13：记住的全部保存问题与处理标准

这些问题中有不少底座已实现，接手者要核对正式路径，不要全部从零重写。尤其不要因为用户再问“保存问题”就停止性能主线数轮。

### 第8项：部分加载保存、未加载数据与未知字段

**风险：**只加载一页却按scene现有项重写整库，把其他页当删除；未加载正文被空值覆盖；未知JSON字段丢失。

**已有：**PartialProjectSession、PartialDomains、partial_save不可变快照、显式JSON清除/替换、部分加载标记与错误拒绝、附件和材质引用保护。

**还缺：**新正式loader与所有编辑入口范围登记闭环；部分metadata/board状态与删除生命周期需核对；不能一遇非空材质/资源树就拒绝正常保存。

**执行：**加载登记真实基线；只写差异；省略表示未加载，空串/None/[]表示显式清除；未知字段合并保留；部分场景绝不回退全量writer猜删；capture失败在worker启动前提示。

**完成标准：**只改一页能正常保存重开，其他记录/表/未知字段/原件附件不变；新增、清除、删恢复正确；拒绝所有保存不算功能完成。

### 第9项：跨页移动（不是网页跳转）

**风险：**源页消失被当删除；目标未加载时重复插入；default-canvas与虚拟全部/未分类混用；撤销后归属和索引不一致。

**已有：**显式authorize_canvas_move、canonical对象、跨页基线、prepare/commit/cancel卸载、保存期间dirty保留等基础。

**执行：**移动命令验证真实目标存在/类型/回收状态；只改指定源记录归属，不要求加载目标所有图片；同步源/目标目录、计数、搜索；撤销重做走同规则。

**必须覆盖：**移到已加载页；移到未加载页；非法目标；普通页↔默认页；保存期间再次移动；移动后撤销重做再保存重开。

**完成标准：**操作正确完成或执行前明确拒绝，图片不消失不重复，位置与归属重开正确；未改原件和附件不变，失败不显示成功。

### 第10项：附件、原件、删除与导出

**风险：**原件被预览替代；纹理绑定指向空附件；硬删除误删共享源；导出漏未加载选择；同名文件互相覆盖。

**已有：**源字节读取、StoredAttachments、附件写/显式删、绑定校验、原件流式读写部分能力。旧原工程个别损坏项不得洗成正常。

**执行：**原件与可重建预览分离；新绑定附件非空校验；附件记录和材质变更事务一致；永久删除先明确引用/lifecycle，未支持前禁止静默猜删；导出冻结逻辑选择ID/查询快照、分批取源、稳定避同名冲突、可取消。

**完成标准：**图片/视频/材质原件与导出逐项字节一致；只改标签不重写全部原件；多选/全选覆盖未加载结果；共享源不误删，失败清单可定位。

### 第11项：保存期间的新编辑与回执确认

**风险：**启动保存后又编辑，旧快照落盘后把新修改也标成已保存；回执异常却清dirty；双击保存启动两个worker。

**已有：**快照revision、ack只确认快照、失败保持dirty、防重入等保护已有专门实现和历史测试。

**执行：**capture冻结本次版本；后台只写快照；ack核对项目/快照/receipt，只推进实际写入基线；保存期间变更继续dirty；ack缺失/异常/会话失效禁止显示成功；自动保存和手动保存一致。

**完成标准：**保存A期间改成B，第一次落盘A且界面仍dirty，第二次保存落盘B；异常不假清洁，重试可恢复。不能只验证磁盘正确而忽略UI提示。

### 第12项：另存后原件引用与后续保存

**风险：**新文件已生成，但图片 `_prism_source`、停放记录、目录、附件或视频仍指旧工程；旧源移动后新工程不能用。

**已有：**SQLite backup另存、发布前源/目标校验、部分会话source重绑、停放记录重绑已有基础。

**执行：**原地保存与另存分开；另存保留所有未加载数据和表BLOB；安全完成后统一重绑resident/parked/pending记录、附件、目录和临时租约；失败不提前切换；同路径另存等同正确原地语义。

**完成标准：**另存→继续编辑→再保存→重开正确；专用副本条件下旧源不可用仍能从新文件显示/导出；取消/源变更/发布失败不破坏旧源，不留下假成功。55GB整库复制不能承诺秒完成。

### 第13项：延迟正文与工程元数据

**风险：**打开空文档/脑图；用户编辑后保存前物化从磁盘拿旧content覆盖新编辑；页面列表名义延迟，实际仍读取所有正文；默认正文/普通页/导入存在不同路径。

**最新修改：**

- `load_deferred_board_extras(scene,names=None,page_ids=...)`已加入目标ID过滤。
- `_materialize_workspace_pages`按128个ID批次读取，已有content不覆盖，包括None/空串的显式清除。
- document `_scene_html` / mindmap `_scene_tree`已接按需补载。
- 默认页setter清相应deferred标记；`_deferred_page_ids`跟踪尚未补载页。

**状态：代码工作中，尚未完成本轮针对性回归，不能标修复完成。**

**还要做：**目标正文补载最终并入异步导航，避免巨型正文GUI读；read_board_extras页面列表不再全量读取/解析全部data；保存不为未改字段强制读取所有正文；源版本校验、读取失败与旧格式缺表区分；修改全局标签/查询配置/board状态也应明确差异覆盖。

**完成标准：**目标页打开显示原内容；改/清/导入后不被旧正文覆盖；未打开页保存不变；旧格式无envelope兼容；读取异常保持未加载/错误，不能当空正文保存。

## 六、任务14—15：可选项与最终交付

### 第14项：图片后端、兼容与需要更多证据的选择

- pyvips不是必须采用。核对原生运行库、打包、色彩/ICC/EXIF/Alpha/16位/HDR、法线ORM数据图与Qt对照；实际稳定收益才接。
- 不整体换C++。先去全量与重复工作，只有有界流程仍有稳定CPU热点才评估小范围原生实现。
- 旧 `.bee` 打开失败保留现场，核查当前引入点/时序；不删除断言，不扩大超时掩盖。
- 保留Qt/Babylon/PyAV/SQLite，不能自己写替代引擎。

**完成标准：**选型有结果和理由，采用者兼容且可打包，不采用者不阻挡性能主线；兼容问题有明确修复或限制，不伪归因。

### 第15项：正式入口验收与规范收口

已有报告足够开始开发。每包只做相关必要回归；不要重复跑数百项来替代写代码。正式路径接通后再覆盖最终矩阵。

**验收目标（尚非已达到）：**

| 指标 | 目标和限制 |
| --- | --- |
| 重复进入未变范围 | 全量重建/排列为0，布局缓存命中不再重建同一空间索引 |
| GUI同步工作 | 常规尽量≤16ms；切换启动≤100ms；反复>100ms需要拆分 |
| 可信目录+当前100图打开 | 目标1—3秒可操作；首帧、清晰、后台完成分开报 |
| 已准备模块切换 | 目标0.3—0.5秒可操作，不扣掉真实编辑提交等待伪造速度 |
| 交互帧耗时 | 建议p95≤33ms，长尖峰单独报告，不用平均值掩盖 |
| 焦点清晰 | 热预览目标≤0.5秒，需源解码目标≤2秒；大图异常说明 |
| 全视口清晰 | 热态目标≤2秒，需要源读取目标≤5秒，无长期马赛克 |
| 无目录首次建库 | 不承诺秒数，要求可响应、取消、恢复、进度真实 |
| 资源 | 每类有声明字节预算、队列上限，固定工作集稳定，增加页面仍有界 |
| 搜索 | 就绪索引常规首批目标≤300ms；不漏未加载或超页结果 |

数值是开发目标，必须标注硬件/磁盘/GPU/远程/DPI/目录与缓存状态。不达标写原因和下一热点，不能悄悄降低画质或放宽口径。

**最终矩阵：**100/1000/1万/10万；全部/未标签/未分类/默认/自定义；远中近/快速缩放/平移/停止恢复；目录四态；四模块切换；5000材质浏览与实际代表纹理；500文档/500脑图按需打开；保存重开；单选/多选/全选原件导出；长会话资源；真实BAT/GPU环境。offscreen仅作为回归条件，不能覆盖全部产品体验。

**材质功能不得退化：**不透明、透明裁切、半透明、金属、粗糙、法线、AO、自发光、SSS、自定义通道；用户离开材质丢弃临时滑块调整，但绑定/通道/标签/备注/原图保留。预览不要求与用户UE母材质逐像素一致，不借优化重写UE母材质。

**交付：**逐项代码位置、正式接线、工作量/耗时、相关检查、未完成项；更新CONTINUATION和CURRENT_STATUS；不把重叠测试计数累加，不把脚本失败自动算产品错误。

## 七、全部30项问题/待确认项的追踪映射

### A01—A18：已确定的工作

> **本表状态列已被 §〇 0.4 逐项修正**，下表仅保留原始问题描述。关键修正：A01 正式入口仍是全量（A01 阻塞项＝D1/D2）；A02 已修；A05 布局可靠键已实现但无规模实测；A14 共享预算真实存在但覆盖面不足、不可观测；A15 页面对象仍无界；A18 保存底座已成型，真缺口是永久删除三条路径与未加载资产导出。

| ID | 问题 | 下一任务/当前状态 |
| --- | --- | --- |
| A01 | 正式打开全量详情 | 任务1，实验有界路径存在，正式闭环未完成 |
| A02 | 查询形状扫描无关记录 | 任务1/7，ID读取已有，keyset工作中 |
| A03 | 当前页等全库准备 | 任务1/3，未完成 |
| A04 | 切换重复扫描/计数 | 任务2，局部改造，目录overlay未闭环 |
| A05 | 不变范围重复布局 | 任务2，可靠键和快速返回工作中 |
| A06 | 聚合全量准备卡片 | 任务2，摘要/空间窗口未完整接 |
| A07 | 切换误清图片缓存 | 任务2/4，已有局部修改，需相关检查 |
| A08 | 连续尺寸滑块重布局 | 任务2，已有80ms合并，规模未验收 |
| A09 | 中近景恢复慢 | 任务4，未解决 |
| A10 | tile/合成尖峰 | 任务4，单项长任务仍未解决 |
| A11 | 打开提取内嵌视频 | 任务6，延迟登记已有，但物化方式仍需修 |
| A12 | 视频源版本预览复用 | 任务6，部分代码，命中/租约未闭环 |
| A13 | 编辑提交与导航 | 任务3，框架已有，真实调用闭环未完成 |
| A14 | 统一字节预算 | 任务5，未完成 |
| A15 | 多页真正有界驻留 | 任务5，安全停放已有，不等于内存有界 |
| A16 | 搜索未加载数据 | 任务7，部分接入，分页和正文未闭环 |
| A17 | 重复查询与失效架构 | 任务2/7，dirty统一工作中 |
| A18 | 保存不全量重写 | 任务8—13，底座较多，正式UI数据闭环未完成 |

### B01—B12：未知不能猜成已知，但不阻止直接改确定问题

| ID | 仍需确认 | 如何处理 |
| --- | --- | --- |
| B01 | 打开SQL/I/O/解析/构造/安装准确占比 | 新流程少量必要分段，不把剩余全归Python |
| B02 | 历史偶发半秒以上停顿准确来源 | 只对仍复现热点定位，不凭栈所在位置断言耗时发生处 |
| B03 | 清晰恢复主导阶段 | 记录请求/解码/安装/真实绘制时刻 |
| B04 | 过期任务抢占程度 | 统计取消后实际继续工作量，全链路generation检查 |
| B05 | 首次布局分组/后台收益 | 保留有界分层，先消除重复，再测是否需后台纯数据计算 |
| B06 | 非黑帧搜索是否视频主因 | 核对帧数与内容，不盲取黑帧换速度 |
| B07 | 是否真实内存泄漏 | 分离正常缓存、未释放引用、GPU与持续增长 |
| B08 | 本项目WebEngine真实成本 | 只统计自己进程树，不含Codex/用户其他窗口 |
| B09 | pyvips净收益/打包兼容 | 任务14独立试验，可不采用 |
| B10 | 是否需要C++热点实现 | 不整体换语言，先解决架构工作量 |
| B11 | 独立十万图解码负载 | 复制记录与真实独立资产分开报告 |
| B12 | 冷热/磁盘和目录四态表现 | 分开记录；69.6秒不能无证据归因为55GB系统缓存预热 |

“按100条读取395ms”只能作为报告中的数据层观察，不能代表正式打开/绘制/切换；代码曾误读scope内图片原件，首次慢的归因必须重新核对真实读取字节。不能沿用“读了整个55GB所以纯属硬盘正常”这个未经证明的解释。

## 八、哪些可以复用，必须注意什么

| 模块 | 直接复用的能力 | 接手注意 |
| --- | --- | --- |
| ProjectCatalogIndex/OpenScope | 可重建旁路目录、counts/page/details/query | 目录是派生索引，源工程+会话修改才是数据真相；加cursor不等于UI已分页 |
| ProjectLoadService/SceneLoadScheduler | 阶段/取消、GUI批量安装 | 不先排十万项再说有分片；单项也要受控 |
| ImageRecord/viewport_items | 轻量记录、空间查询、呈现分离 | 不是每条一个QWidget；canonical保留不是最终驻留上限 |
| AssetCanvas/compact_layout | 自绘、布局、分组 | 可靠版本、源/crop变化、相机和手工布局保护 |
| canvas_tiles/composites/overview/lod | 真实合成、远中近、缓存 | 像素与几何失效分开，单tile仍可能超预算 |
| navigation/编辑器snapshot | latest-intent、提交屏障、epoch | 复用协调器，不再造第二套；核对真实调用而非仅新函数存在 |
| partial_project/partial_domains/residency | 基线、dirty、跨页、清除、卸载过渡 | 未加载不等于删除；不得丢dirty；不要重复扩展保存底座 |
| partial_save/StoredAttachments | 快照、事务、冲突、回执、附件/原件 | 不转码；失败不清dirty；另存后引用全重绑 |
| source_store/image_source | 稳定源、流读取、元数据 | 禁止在GUI整块读取大视频；临时资源需要租约 |
| material_service/library/render/task | 摘要、revision、当前预览、Babylon | 静态卡片不各起引擎；隐藏停渲染，按用户规则丢临时调整 |
| search_candidates/trigram/search_controller | 候选、精确过滤、跳转 | 超过1024不漏；短词中文fallback；dirty与未加载一致 |
| Qt/PyAV/Babylon/SQLite | 现成显示、媒体、材质/GLB、存储引擎 | 禁止自写替代引擎，不能以性能为名移除功能 |

### 必须统一的协议

1. 异步请求有project ID、open epoch、operation、scope、generation和源revision；安装前再校验。
2. SQLite连接线程内创建/关闭，后台不直接操作GUI图元。
3. 可见焦点优先，结果队列和后台预取有上限，过期可取消。
4. query、count、search、export看统一目录+dirty覆盖，不能各维护一套不一致资产集合。
5. 原件、详情、摘要、预览分离；原件不转码，预览可重建。
6. 缓存有完整key、字节预算、拥有者和失效规则，不用对象地址作为持久资源ID。
7. 空/未知/未加载/错误/取消分开；异常不吞成可信0或空结果。
8. 不用processEvents忙等、任意sleep、全局热键/剪贴板抢占解决同步。

### 必须纠正的九种误读

- 不能说“改msleep一行省47秒”，成功图片分支提前continue。
- 43次pack是分组，不是43次全量十万图排列。
- 卡片数量不是QWidget数量，已有自绘。
- 视频首帧早已后台解码，别重复搬线程。
- 一次request快不能排除整个视频路径。
- 30轮短数据不能冒充30分钟，也不能证明无泄漏。
- prefetched_details为0不能称两页预取成功。
- 小切片JSON快不能排除全量累计；总耗时残差不全是Python。
- 重复预览字符串/重复源去重不能代表真实用户独立十万图效果。

## 九、接手时检查尚未集成的工作，别丢掉也别假完成

本轮并行分工：

- 主线：fileio/sql.py与文档/脑图getter，已落R01/R02/R07/R08相关修改，未完成本轮回归。
- paged_navigation_fix：负责view/window_workspace与新paged_canvas_loader；新模块存在，但本次查到view中尚未出现request_canvas_assets正式接线，继续核对，不写完成。
- catalog_search_fix：负责目录keyset、搜索分页、统计overlay；query/Scope已有after_id/迭代接口修改，dirty覆盖与UI接线仍需完成。
- render_layout_fix：负责可靠布局键、已应用布局快速返回、pack父层分层，已出现修改，针对性回归未完成。

消息打断后智能体可能处于interrupted。接手先检查当前进程/任务和磁盘文件，别重复启动、别覆盖其他线修改。若继续并行：独立文件所有权，view由一个集成人负责；没有完整输出的代码不得标“可直接落地完成”。

上个AI说“P1—P6功能代码全部写出”与其承认的A06/A14/A15/未加载多选导出未完成相矛盾。应改成“若干子功能已写、部分接线、关键闭环未完成”。其有效代码保留，结论按实际核对。

## 十、怎么推进，才不会又停在报告和测试

1. 从任务1开始落正式代码，同时可在不同文件推进2、4、6。任务3/7依赖接口稳定后接。
2. 只补阻挡该功能的数据安全问题，不先扩展永久删除、分布式存储、模型库、手机端或整体C++重写。
3. 每个小包改完跑相关必要回归，不重复全项目诊断、不重复旧十万图超时入口；需要最终界面验收时才扩大范围。
4. 保存测试通过数不是性能成绩；只过语法也不是功能完成。
5. 原始失败报告不覆盖；缺证据标未验收，源码明确缺口标未完成，不混成“都只是没测”。
6. 每包交付写清文件/函数、调用入口、少读/少做了什么、相关结果、剩余问题和下一具体入口。

### 状态词与交付模板

只使用：未开始 / 开发中 / 代码已接未验收 / 部分验收 / 完成 / 阻塞。

```text
当前任务与问题ID：
修改文件/函数：
正式调用入口：
复用哪些已有组件：
原来多做什么，现在减少什么：
数据/线程/取消/缓存失效规则：
实际读取/安装/布局次数、字节、耗时与环境：
相关检查与退出结果；未做的检查：
保存重开/原件保护结果：
未完成项和下一函数：
后台任务状态：
状态：
```

**总完成门槛：正式产品入口可用、性能达标或有明确剩余热点、画质和功能不退化、未加载数据和原件安全、资源有界。不是提交接口、单次100条查询快或累计几百项测试通过。**
