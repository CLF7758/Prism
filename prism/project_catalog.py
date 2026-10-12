"""全项目素材查询服务（方案 §5）。

统一为左栏导航数字、资源树计数、全项目结果列表和状态栏提供同一套谓词，
避免 `media_library` / `project_search` / `resource_tree_model` 各自写一套
过滤条件导致口径漂移（顶部"全部素材 22" vs 父目录"1562"就是这么来的）。

设计要点
--------

* **只读元数据，不解码图像。** 1562 张图的计数不应触发 1562 次原图读取，
  否则平移/缩放/拖动过程中每一次 `scene.changed` 都会卡顿。所有谓词只看
  `_tags` / `_rating` / `_canvas_id` / `_deleted_at` / `_title` / `_notes`
  / `filename` 这些内存里已经有的属性。

* **活跃素材定义**（方案 §5.1）：
  - 有 `save_id` 属性（`scene.items_for_save()` 的口径，见 scene.py:539）
  - `TYPE` 不在 `ANNOTATION_TYPES = ('path', 'text')` 里（画笔线和文字
    注释不是素材）
  - 未软删除（`_deleted_at` 为 None，Stage C 引入的字段；缺失视为活跃）

* **无归属素材**（方案 §5.3）：桌面样本 `参考.prism` 里有 1 张图缺少
  `canvasId`。`items.load_metadata_from_data()` 会把缺失默认为
  `'default-canvas'`（items.py:413），所以运行时 `_canvas_id` 通常不会是
  None。但为了防御 `_canvas_id` 为 None、或指向的画布已不在工程里的情况，
  统一归入 `UNASSIGNED_CANVAS` 桶：
  - 全项目数量、未标签数量各计一次
  - 切换当前画布不漂移
  - 结果列表里路径显示为"未归属"
  - 定位时若原画布已消失，回落到 `default-canvas`（存在）或第一个画布

* **同一对象不重复计数**：用 `id(item)` 作稳定身份（`save_id` 未保存前
  可能是 None）。`scene.items()` 本身不会返回同一对象两次，所以直接枚举
  即可；额外的去重只在合并多个来源（例如归档 + 场景）时才需要。

* **不按文件名/内容去重**（方案 §5.1）：一张图出现在两个画布且项目存了
  两个独立对象，算两份实例。

* **不扫描磁盘参考目录**（方案 §5.1）：项目已导入的素材与文件夹里尚未
  导入的文件是不同概念。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Optional

logger = logging.getLogger(__name__)


#: 画笔线和文字注释的 TYPE。它们不是素材：没有分类/标签/评分/文件名，
#: 按素材规则算永远"不匹配"。与 `media_library.ANNOTATION_TYPES` 保持
#: 一致（那里用字符串元组避免循环 import，这里同理）。
ANNOTATION_TYPES = ('path', 'text')

#: 无归属素材的虚拟画布 ID。不会与任何真实 pageId 冲突（真实 pageId 是
#: uuid4().hex 或 'default-canvas'/'default-mindmap'/'default-document'）。
UNASSIGNED_CANVAS = '__unassigned__'

#: 结果列表里"未归属"的显示文本。
UNASSIGNED_LABEL = '未归属'


@dataclass
class AssetResult:
    """全项目结果列表的一行。

    `item` 是场景里的图元对象（可能已被 sip 删除，消费方要用
    `sip.isdeleted` 守卫）。其余字段是渲染时需要的元数据快照，避免结果
    列表在构建后再去反复访问图元属性。
    """
    item: object
    title: str
    path: str
    canvas_id: str
    page_kind: str = 'canvas'
    tags: list = field(default_factory=list)
    rating: int = 0
    notes: str = ''
    filename: str = ''
    deleted_at: Optional[str] = None

    @property
    def is_unassigned(self) -> bool:
        return self.canvas_id == UNASSIGNED_CANVAS


class ProjectAssetCatalog:
    """全项目素材查询服务。

    参数
    ----
    scene:
        `PrismScene` 实例。计数和查询都从 `scene.items_for_save()` 枚举。
    resource_service:
        可选的 `ResourceTreeService`。用来把 canvas_id 映射到资源树路径
        （"画布 / 参考 / 子文件夹"）。缺失时路径退化为画布标题或"未归属"。
    page_title_resolver:
        可选的可调用对象 `(canvas_id) -> str`，返回画布标题。默认从
        `scene.workspace_pages` 里查。

    线程安全
    --------
    非线程安全。所有方法都应在 GUI 线程调用。计数走 300 ms 防抖
    （`MediaLibraryPanel._recount_timer`），不要在 `scene.changed` 的每
    一次发射里都调。
    """

    def __init__(self, scene, resource_service=None, page_title_resolver=None):
        self.scene = scene
        self.resource_service = resource_service
        self._page_title_resolver = page_title_resolver

    # ── 谓词 ─────────────────────────────────────────────────

    @staticmethod
    def is_annotation(item) -> bool:
        """画笔线 / 文字注释。它们不是素材。"""
        return getattr(item, 'TYPE', '') in ANNOTATION_TYPES

    @staticmethod
    def is_deleted(item) -> bool:
        """软删除状态。Stage C 引入 `_deleted_at` 字段；缺失视为活跃。"""
        return getattr(item, '_deleted_at', None) is not None

    @classmethod
    def is_active(cls, item) -> bool:
        """活跃素材：有 save_id、非注释、未软删除。

        注意：`scene.items_for_save()` 已经过滤了 `hasattr(i, 'save_id')`，
        所以从它枚举出来的 item 不需要再检查 save_id。但 `is_active` 也
        会被外部直接调用（例如传入 `scene.items()` 的元素），所以保留
        完整检查。
        """
        if not (hasattr(item, 'save_id') or getattr(item, '_asset_source', None)):
            return False
        if cls.is_annotation(item):
            return False
        if cls.is_deleted(item):
            return False
        return True

    # ── 枚举 ─────────────────────────────────────────────────

    def asset_items(self):
        for item in self.scene.items():
            if hasattr(item, 'save_id') or getattr(item, '_asset_source', None):
                yield item

    def active_items(self) -> Iterator:
        """全项目活跃素材生成器。

        跨所有画布和嵌套文件夹，不随当前画布切换而变为局部数量。
        """
        for item in self.asset_items():
            if self.is_active(item):
                yield item

    def deleted_items(self) -> Iterator:
        """全项目软删除素材生成器（回收站里的图片）。"""
        for item in self.asset_items():
            if (hasattr(item, 'save_id') or getattr(item, '_asset_source', None)) and not self.is_annotation(item) \
                    and self.is_deleted(item):
                yield item

    # ── 计数 ─────────────────────────────────────────────────

    def total_count(self) -> int:
        """活跃素材总数（方案 §5.1）。"""
        return sum(1 for _ in self.active_items())

    def untagged_count(self) -> int:
        """无有效素材标签的活跃素材数。

        "有效标签"的判定：`_tags` 列表非空，且至少有一个非空白字符串。
        页面标签不能自动代替图片标签（方案 §5.1）。
        """
        count = 0
        for item in self.active_items():
            if not self._has_valid_tags(item):
                count += 1
        return count

    @staticmethod
    def _has_valid_tags(item) -> bool:
        tags = getattr(item, '_tags', None) or []
        return any(str(t).strip() for t in tags)

    def canvas_counts(self) -> dict:
        """`{canvas_id: count}` 字典，含 `UNASSIGNED_CANVAS` 桶。

        资源树父文件夹的递归汇总由 `ResourceTreeModel.set_asset_counts()`
        基于这个字典做（它已经实现了文件夹层级的 roll-up，见
        test_eagle_sidebars.test_tree_counts_roll_up_nested_canvas_folders）。
        """
        counts: dict = {}
        for item in self.active_items():
            cid = self._effective_canvas_id(item)
            counts[cid] = counts.get(cid, 0) + 1
        return counts

    def trash_top_level_count(self) -> int:
        """回收站顶层删除记录数（方案 §7.1 口径）。

        - 独立删除的图片：每张记 1
        - 删除整个页面/文件夹：记 1 条（内部图片数在行内另显示，不累加
          到徽标）

        实现上：
        - 素材级：统计软删除图片中"不属于任何已删除页面/文件夹"的数量。
          属于已删除页面的图片算在页面那条记录里，不重复计。
        - 页面级：`resource_service.trashed_nodes()` 里 `parentId` 为 None
          或父节点未删除的节点数（顶层记录）。
        """
        deleted_page_ids = self._deleted_page_ids()
        asset_count = 0
        for item in self.deleted_items():
            cid = getattr(item, '_canvas_id', None)
            if cid in deleted_page_ids:
                # 这张图属于一个被整体删除的页面/文件夹，算在那条记录里
                continue
            asset_count += 1
        page_count = len(self._top_level_trashed_nodes())
        return asset_count + page_count

    def trash_asset_count(self) -> int:
        """回收站里素材总数（含被整体删除的页面内部的素材）。

        用于回收站面板"素材"页签的条目数显示，与 `trash_top_level_count`
        的徽标口径不同。
        """
        return sum(1 for _ in self.deleted_items())

    def _deleted_page_ids(self) -> set:
        """当前处于软删除状态的页面/文件夹 pageId 集合。"""
        if self.resource_service is None:
            return set()
        ids = set()
        for node in self.resource_service.trashed_nodes():
            page_id = node.get('pageId')
            if page_id:
                ids.add(page_id)
            # 文件夹本身没有 pageId，但它下面的页面会被一起软删除。
            # `trashed_nodes()` 已经返回所有软删除节点（含子节点），所以
            # 这里不需要额外递归。
        return ids

    def _top_level_trashed_nodes(self) -> list:
        """顶层删除记录：parentId 为 None 或父节点未删除的软删除节点。"""
        if self.resource_service is None:
            return []
        trashed = list(self.resource_service.trashed_nodes())
        trashed_ids = {n['id'] for n in trashed}
        top = []
        for node in trashed:
            parent_id = node.get('parentId')
            if not parent_id or parent_id not in trashed_ids:
                top.append(node)
        return top

    # ── 查询 ─────────────────────────────────────────────────

    def query(self, text: str = '', tag_expression: str = '',
              untagged_only: bool = False, min_rating: int = 0,
              canvas_scope: Optional[Iterable] = None,
              include_deleted: bool = False,
              limit: Optional[int] = None) -> list:
        """全项目素材查询，返回 `AssetResult` 列表。

        参数
        ----
        text:
            普通文字搜索。多个词都要命中（空格 = 与）。匹配 `_title` /
            `filename` / `_notes` / `_tags`。大小写不敏感。
        tag_expression:
            标签查询语法（`tag:xxx` / `xxx:*` / `a | b` / `a -b` / `-*`）。
            委托 `prism.tags.parse_query` 解析。
        untagged_only:
            只返回无有效标签的素材。
        min_rating:
            最低评分（0 = 不限）。
        canvas_scope:
            限定画布 ID 集合。None = 全项目。`UNASSIGNED_CANVAS` 可作为
            集合成员，表示包含无归属素材。
        include_deleted:
            是否包含软删除素材。默认 False（活跃素材）。回收站面板用 True。
        limit:
            最多返回多少条。None = 不限。结果列表 UI 通常设 200~500，
            避免一次构建上万行 QTreeWidgetItem。

        返回
        ----
        `list[AssetResult]`，按 (canvas 路径, 标题) 排序，便于分组显示。
        """
        from prism import tags as tags_module

        text_words = [w.casefold() for w in (text or '').split() if w]
        tag_query = tags_module.parse_query(tag_expression) if tag_expression else None
        tag_system = tags_module.tag_system(self.scene)
        scope_set = set(canvas_scope) if canvas_scope is not None else None

        results: list = []
        source = self.asset_items()
        for item in source:
            if not (hasattr(item, 'save_id') or getattr(item, '_asset_source', None)):
                continue
            if self.is_annotation(item):
                continue
            deleted = self.is_deleted(item)
            if deleted and not include_deleted:
                continue
            if not deleted and include_deleted:
                # include_deleted=True 时只返回删除项（回收站视图）
                continue

            cid = self._effective_canvas_id(item)
            if scope_set is not None and cid not in scope_set:
                continue

            if untagged_only and self._has_valid_tags(item):
                continue

            rating = int(getattr(item, '_rating', 0) or 0)
            if min_rating and rating < min_rating:
                continue

            item_tags = list(getattr(item, '_tags', []) or [])
            if tag_query is not None:
                index = tags_module.name_index(item_tags, tag_system)
                if not tag_query.matches(index):
                    continue

            if text_words:
                haystack = self._text_haystack(item, item_tags)
                if not all(word in haystack for word in text_words):
                    continue

            results.append(self._build_result(item, cid, item_tags, rating, deleted))
            if limit is not None and len(results) >= limit:
                break

        results.sort(key=lambda r: (r.path, r.title.casefold()))
        return results

    @staticmethod
    def _text_haystack(item, item_tags) -> str:
        parts = [
            str(getattr(item, '_title', '') or ''),
            str(getattr(item, 'filename', '') or ''),
            str(getattr(item, '_notes', '') or ''),
        ]
        parts.extend(str(t) for t in item_tags)
        return ' '.join(parts).casefold()

    def _build_result(self, item, canvas_id, item_tags, rating, deleted) -> AssetResult:
        title = (getattr(item, '_title', '') or
                 getattr(item, 'filename', '') or
                 '未命名')
        path = self.canvas_path(canvas_id)
        return AssetResult(
            item=item,
            title=str(title),
            path=path,
            canvas_id=canvas_id,
            page_kind='canvas',
            tags=list(item_tags),
            rating=rating,
            notes=str(getattr(item, '_notes', '') or ''),
            filename=str(getattr(item, 'filename', '') or ''),
            deleted_at=getattr(item, '_deleted_at', None) if deleted else None,
        )

    # ── 画布归属与路径 ───────────────────────────────────────

    def _effective_canvas_id(self, item) -> str:
        """素材的有效画布 ID。

        - `_canvas_id` 为 None → UNASSIGNED_CANVAS
        - `_canvas_id` 指向的画布已不在工程里 → UNASSIGNED_CANVAS
        - 否则返回 `_canvas_id`

        "画布已不在工程里"的判定：`scene.workspace_pages` 里没有对应 id
        且不是 'default-canvas'（default-canvas 是隐式默认，即使
        workspace_pages 里没有也可能存在素材，见
        `_pages_with_implicit_defaults`）。
        """
        cid = getattr(item, '_canvas_id', None)
        if cid is None:
            return UNASSIGNED_CANVAS
        known = self._known_canvas_ids()
        if cid in known or cid == 'default-canvas':
            return cid
        return UNASSIGNED_CANVAS

    def _known_canvas_ids(self) -> set:
        ids = set()
        for page in getattr(self.scene, 'workspace_pages', []) or []:
            if page.get('kind') == 'canvas':
                ids.add(page.get('id'))
        # workspace_nodes 里的 canvas 分区页面也算
        if self.resource_service is not None:
            for node in self.resource_service.nodes:
                if node.get('section') == 'canvas' and node.get('pageId'):
                    ids.add(node['pageId'])
        return ids

    def canvas_path(self, canvas_id: str) -> str:
        """画布在资源树里的可读路径，如 "参考 / ARC Raiders"。

        UNASSIGNED_CANVAS 返回 "未归属"。
        """
        if canvas_id == UNASSIGNED_CANVAS:
            return UNASSIGNED_LABEL
        if self.resource_service is None:
            return self._page_title(canvas_id)
        # 从 resource_service 里找 pageId 对应的节点，向上拼路径
        node = None
        for n in self.resource_service.nodes:
            if n.get('pageId') == canvas_id:
                node = n
                break
        if node is None:
            return self._page_title(canvas_id)
        parts = []
        current = node
        seen = set()
        while current is not None and current['id'] not in seen:
            seen.add(current['id'])
            parts.append(current.get('title') or '')
            parent_id = current.get('parentId')
            if not parent_id:
                break
            current = self.resource_service._by_id(parent_id)
        parts.reverse()
        return ' / '.join(p for p in parts if p) or self._page_title(canvas_id)

    def _page_title(self, canvas_id: str) -> str:
        if self._page_title_resolver is not None:
            try:
                title = self._page_title_resolver(canvas_id)
                if title:
                    return str(title)
            except Exception:
                logger.exception('page_title_resolver failed for %s', canvas_id)
        for page in getattr(self.scene, 'workspace_pages', []) or []:
            if page.get('id') == canvas_id:
                return str(page.get('title') or canvas_id)
        if canvas_id == 'default-canvas':
            return '默认画布'
        return canvas_id

    # ── 定位 ─────────────────────────────────────────────────

    def locate(self, item) -> tuple:
        """返回 `(page_kind, page_id)`，用于导航到素材所在画布。

        - 素材有明确画布且画布存在 → ('canvas', canvas_id)
        - 素材无归属或画布已消失 → ('canvas', fallback_canvas_id)
          fallback 优先 'default-canvas'（若工程里有素材属于它），否则
          第一个已知画布，否则 None（调用方应提示用户选择有效画布）。
        """
        cid = getattr(item, '_canvas_id', None)
        known = self._known_canvas_ids()
        if cid and (cid in known or cid == 'default-canvas'):
            return ('canvas', cid)
        fallback = self._fallback_canvas_id()
        return ('canvas', fallback)

    def _fallback_canvas_id(self) -> Optional[str]:
        # default-canvas 是隐式默认页：`_pages_with_implicit_defaults` 总是
        # 支持打开它来承载素材（见 media_library.py），即使 workspace_pages
        # 里没有显式注册。所以它是无归属素材的首选回落，不要求它出现在
        # _known_canvas_ids() 里。
        return 'default-canvas'
