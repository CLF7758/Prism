# Library interaction changes — 2026-10-29

## Requested behavior and implementation
- Removed model-summary-ready status and clear-selection button. Existing selection count/blank-area clear interaction remains. Model-root tree count now suppressed in delegate by SECTION_ROLE=model + NODE_TYPE_ROLE=section; root NODE_ID_ROLE is None, so initial id-based attempt was ineffective. Internal counts unchanged.
- Web material/model cards: F2 and name double-click create an input inside h3. Enter/blur sends renameInline(id,name,generation); Escape cancels. Renderer uses textContent/input.value, never injects names as HTML. Image/body double-click keeps preview behavior. Existing service/repository validation and write gates remain authoritative; label updates through service refresh, not optimistic persisted-state claims. Material rename accepts supplied name; model rename already supports it. Context menu rename uses same inline entry point.
- Model sidebar material spheres: existing F2/double-click path now opens a native QLineEdit over the name area, never QInputDialog. Enter commits display_name; Escape cancels. Editor callback captures the original material identity; stale destroyed editors cannot clear newer editor ownership.
- Cards Shift selects displayed-order range from anchor; Ctrl+Shift unions range; Ctrl toggles. Existing domain selection signals used. Qt grid/tree retain ExtendedSelection. Resource-tree modified clicks no longer navigate away during organization. AssetCanvas replaces Shift toggling with model-order range selection. Main free-placement canvas Shift-click adds an unselected object without dropping existing selection (free-placement canvas has no universal list order). Existing Ctrl+Shift external drag preserved.
- AssetBrowser existing thumbnail slider has +/- and Ctrl-wheel synchronization in grid scopes (all/unclassified/recent/untagged). CanvasToolbar adds logarithmic camera zoom with +/- for main/asset canvas; hides this duplicate toolbar control for grid scopes, which use their thumbnail-size slider. Main Ctrl-wheel explicitly uses existing camera zoom; other configured wheel gestures retained.
- Trash asset grid gains thumbnail slider, +/- and Ctrl-wheel. AssetGrid now exposes logical thumbnailSize separately from padded gridSize. Ctrl-wheel clamps logical width 96..280 and preserves content aspect, avoiding repeated +12/+48 padding accumulation. Material/model WebEngine Ctrl-wheel uses existing per-library card-size sliders and persistence.

## Evidence
- library-interaction-first-20261029.txt: 65 passed (initial inline/UI changes).
- library-interaction-final-20261029.txt: 44 passed including actual WebChannel name editing, no-dialog assertion and Shift three-card selection.
- library-interaction-complete-20261029.txt: 61 passed including native model-material inline name Enter/Escape, toolbar, grid layout, tree and trash lifecycle.
- library-final-validation-20261029.txt: initial 3 failures / 50 passes. Grid test exposed padded-size drift; fixed logical size API. Canvas wheel fixture had no content (existing camera intentionally ignores empty scene); fixture now has a real pixmap. No original camera guard weakened.
- library-final-validation-retest-20261029.txt: 63 passed (zoom/inline/cards/model-material/trash/resource tree/partial catalogue).
- library-shift-zoom-validation-20261029.txt: 5 passed; Ctrl-wheel clamp and slider sync, real canvas scale, trash scale, actual Shift-click tree range without navigation and main canvas additive Shift selection.
- cards.js node syntax check successful; source hashes in artifacts/library-interaction-source-20261029.json.

## Limits
No desktop control or user project changes. These are Qt offscreen/WebEngine/service tests, not user desktop/DPI/package acceptance. Cross-page range anchors evicted from the bounded model-card window are not independently verified; list range tests cover currently installed displayed order. Main canvas uses additive Shift selection rather than inventing a range over arbitrary spatial placement. No automatic selection of unloaded model records is claimed.

## Actual entry-point footer correction
User reported controls absent in All/Unclassified/Untagged/Recent/Trash. Previous tests verified grid capability, not complete navigation/control visibility. AssetBrowser.toolbar is reparented into the main title/header stack (main.py around 489), so page sizing controls must not depend on that narrow/conditional header.
- AssetBrowser now owns a fixed bottom ThumbnailZoomBar, independent of title-bar reparenting. Existing slider and resize_cards state reused; old header sizing widgets removed, not duplicated.
- Embedded TrashPanel owns the same page-level footer outside its tab content. Explicit +/- and styled slider dimensions remain usable in narrow columns.
- Empty-result label receives Ctrl-wheel and forwards logical size changes; real grid viewport wheel remains on AssetGrid. This covers the empty-library/filtered-result surface that previous direct wheelEvent tests missed.
- tests/test_zoom_page_entrypoints.py navigates through actual browser.open modes all/unclassified/untagged/recent and main_window._open_trash_panel; checks visible page-owned footer, plus/minus, actual QApplication.sendEvent(QWheelEvent) to viewport, empty-result event and slider sync.
- First run: artifacts/zoom-page-entrypoints-20261029.txt 3 passed / 27 errors. New event filter read empty before construction; fixed with guarded getattr. Kept errors, no timeouts/expectations relaxed.
- artifacts/zoom-page-entrypoints-retest-20261029.txt: 30 passed in 19.14s, actual entry tests plus zoom/selection, trash lifecycle and partial catalogue regressions. This is offscreen Qt navigation/event evidence, not observation of the user's desktop.
- Running user process was started before this correction. Restart of source application is needed to load newly constructed controls; no running application was closed or changed by automation.

## Filter-left zoom position and pinned section navigation
User requested sizing directly LEFT of Filter, and persistent access to canvas/mindmap/document/material/model sections during deep resource scrolling.
- Important corrected route: aggregate browsing displays CanvasToolbar in the main header stack. The page-specific AssetBrowser.toolbar stays hidden there. Initial move back into page toolbar failed four isVisibleTo assertions (artifacts/pinned-sections-zoom-position-20261029.txt); retained. Final implementation reparents the existing thumbnail sizing group into the visible CanvasToolbar immediately before filter_button. It is shown for grid scopes; existing camera zoom stays for free-placement canvas. No footer duplicate. Existing wheel/slider state and source sizing callbacks retained.
- ResourceSectionNavigation is a pinned five-button row above the ResourceTreeView, outside its scroll viewport. Existing folder/page tree remains the single owner of selection/expansion/order/menus/storage. Clicking a fixed shortcut expands and scrolls to that section; material/model shortcuts use the existing authoritative open routes. Canvas/mindmap/document locate their category files without inventing/opening arbitrary new pages. Tree folder hierarchy and creation '+' remain intact. It is a permanent shortcut strip, not five cloned trees or another copy of workspace data.
- Buttons resolve the current tree model on every activation so project rebind cannot retain stale indexes.
- artifacts/pinned-sections-zoom-position-final-20261029.txt: 33 passed (header order AND visibility in all four actual scopes, wheel/buttons, pinned scrolling, resource model/view, toolbar and navigation commit).
- artifacts/pinned-sections-real-sidebar-20261029.txt: 7 passed; includes actual main-window sidebar with 60 canvas folders, scrolling to bottom, all five shortcut buttons still visible, jump to canvas category; and actual header entry-point controls. No desktop control used.
- Running source app must reload modified modules on restart. Tests are offscreen navigation/event checks; user desktop and DPI acceptance remain separate.

## Correction: original section rows fixed, NO extra shortcut UI
The user's clarification supersedes the previous horizontal pinned-shortcut design. That strip and resource_section_navigation.py were removed; there is no runtime section_navigation widget.
- FixedResourceSections presents the ORIGINAL vertical root rows using the same ResourceTreeModel indexes and original _BlankAreaDelegate painting (icons, text, model count suppression and '+' menu). Disclosure arrow is painted using the native tree branch style. Existing section menus and model creation capability gate reused.
- One authoritative ResourceTreeView displays only the active root's children; it is placed directly under that root row. Its viewport scrolls files/folders, while ALL root rows retain their positions during that scroll. Clicking a root expands its child area/collapses it; material/model open signals follow original routes. Other root rows remain visible around the expanded area. No second workspace copy or extra category data created.
- External setCurrentIndex/scrollTo (search/navigation) reveals the correct root automatically. Rebinding uses the current project model, disconnects prior model signals and does not duplicate headers. Row context menus and root drop validation use original model/view rules.
- artifacts/fixed-original-sections-first-20261029.txt: 19 passed.
- artifacts/fixed-original-sections-final-20261029.txt: 41 passed, including actual sidebar with 60 folders, unchanged root-row coordinates after scrolling, all original rows visible, '+' menu gate, model activation, external document-node reveal, resource keys/model/view and navigation/zoom entry regression.
- artifacts/fixed-original-sections-rebind-20261029.txt: 21 passed after model-lifetime cleanup; project model replacement test included.
- No desktop control. DPI, native mouse rendering and package acceptance remain user verification; offscreen tests do not claim to have inspected the user's running window.


## 更正：六个同级大类独立折叠（2026-10-29）

此前“单个子树视口随当前分区搬动”的实现已被本节取代；不得再把它作为正确交互。画布、脑图、文档、材质库、模型库、标签始终是同级固定标题。每行保留原有小箭头，空分类也显示；点击箭头只展开/折叠该分类，点击标题导航并展开自己的内容，其他分类的展开状态保留。滚动任一分类的子内容不会滚走六个标题。

FixedResourceSections 复用 ResourceTreeView、同一个 ResourceTreeModel 和同一个 QItemSelectionModel，为每个分类提供子视口；没有复制工程节点、标签数据、菜单或导入导出服务。主树仍是对外入口，选中、定位、内联创建/重命名与节点展开转到对应视口，现有信号只转发一次。原小箭头使用同一套 chevron-down/right 图标，避免正式 QSS 不绘制手工 branch 的问题。各分类展开状态按工程保存在现有设置中。

测试重点为“同时展开材质库、模型库、标签 → 单独折叠材质库 → 模型库保持展开、标签标题和状态保留”，以及共享模型/选择、子内容滚动、空分类箭头像素、其他分类内联创建、工程模型重绑、正式主题 Windows 原生绘制与全局搜索定位。初轮测试原始失败保留：一个新夹具未开启模型目录能力；七个旧辅助函数假定编辑器属于唯一视口。夹具遵守原能力门，辅助函数改为定位当前实际编辑视口，原业务断言未删减。


## 最新裁决：恢复原始紧凑树布局（取代上述固定分区实现）

用户明确拒绝分区撑满高度及由此产生的空白。已删除 fixed_resource_sections.py 和对应多视口/路由代码，MediaLibrary 重新直接使用原 ResourceTreeView。六类是同一棵树的同级根，恢复原生小箭头和紧凑的逐行排列。展开仅按实际子项高度占位，不为某个分类分配剩余高度；不再声称单独视口固定标题是本轮交付。工程数据、搜索与菜单业务保留。

新布局回归 tests/test_resource_tree_layout.py 直接检查折叠六根的连续 visualRect、顶部位置、只有默认画布时下一大类紧跟最后子项；正式主题原生 Windows/offscreen 启动探针已改测原树。旧 fixed-section/independent-section 产物保留为历史，不作为当前实现依据。


## 当前最终交互：原紧凑树 + 当前大类顶部吸附

用户再次明确：原来的紧凑树布局必须保留，但向下滚动某个展开大类的子项时，该大类标题不能消失。本轮保留单个 ResourceTreeView、六个同级根与原生行高；不恢复多分区伸展布局。

当当前展开根滚出视口上缘，视图在原位置复用该根的 delegate/图标/菜单绘制顶部吸附行，子项正常滚动；到下一个大类时更新归属。吸附行不参与布局分配，不制造空白，不改变树模型或节点父子关系。箭头可折叠该类，标题与加号仍走已有选择/导航/创建菜单。六个根（含空类）hasChildren 保留 disclosure affordance；空白创建行仍隐藏，不伪造文件条目。绘制使用 initViewItemOption，避免此前正式主题的原生崩溃。

新增验证逐一遍历画布/脑图/文档/材质库/模型库/标签，每类数十条子项：滚动中标题 y=0、子项实际位置改变、吸附命中原根、折叠该根；折叠全部后六类仍紧挨顶部。正式 Windows 与 offscreen 子进程都覆盖滚动后的吸附绘制。没有桌面控制，用户真实桌面仍待验。


### 实际绘制补正：空类箭头与吸顶残影

Qt 仍会省略没有可见子项的根箭头，hasChildren=True 不足以保证绘制。ResourceTreeView.drawBranches 现在对六个 section 根显式复用 chevron 图标，其他节点继续原生绘制。新增“新建工程六个箭头”测试比较实际渲染图与去掉图标的对照，逐根验证像素差，分别运行默认主题及正式 QSS。

QWidget.scroll 会移动视口子控件并复用旧像素；仅在后续 paint 中恢复吸顶位置会漏掉滚动瞬间。scrollContentsBy 现在先隐藏旧吸顶行，执行原滚动，再恢复 y=0 并请求视口重绘；移除下一类把标题向上推的逻辑。吸顶行使用明确 alpha=255 的底色，另有透明调色板反例验证整个行输出不透明，防止底下文字透出。这里不据该反例声称真实用户 palette 一定透明。

回归 164 passed in 61.97s；原始结果 artifacts/resource-arrows-and-sticky-paint-final-20261029.txt。含六类滚动即时位置、紧凑布局、真实箭头像素、透明调色板反例、原生 Windows/正式主题隐藏子进程、创建/拖放/键盘/搜索回归。用户桌面未控制。
