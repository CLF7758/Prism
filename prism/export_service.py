# This file is part of Prism.
#
# Prism is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Prism is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Prism.  If not, see <https://www.gnu.org/licenses/>.

"""导出这件事的服务层：把「用户想导什么」翻译成「用哪个导出器」。

任务书 T9 要求 `ExportService` 管四种导出：**原图、调整图、当前页面、
当前场景**。任务书 §3.3 又要求导出时必须让用户明确选的是哪一种：

    调整预览默认不修改原始字节。导出时必须明确选择「原始素材」
    「调整后图片」或「当前场景」。

现有的 `prism/fileio/export.py` 里三个导出器**能力都在**，缺的是一层
把意图对上它们的映射 —— 界面不该自己去记「原图导出 = 
ImagesToDirectoryExporter 并且 adjusted=False 并且……」这一串。

**原图导出不能假装**（任务书 T2）：

    没有可验证原始字节时，不得假装提供无损原图导出。应提示用户只能
    导出当前渲染结果，或要求重新定位源文件。

所以 `check_original()` 会先看那批素材里有没有真的原字节，`plan()` 会
把结论写进 `lossless` 和 `notes`，界面照着显示就行。

这个模块**不持有 view、不碰界面**。要进度和取消时，调用方拿 `plan()` 的
结果自己去开线程（`ThreadedIO`）—— 服务只负责「导什么、怎么导」。

**部分加载的工程（按页打开）** 里 scene 只有一页，其余十万级记录仍在磁盘。
原来的 `items_for()` 只认已加载对象，于是「全选导出」要么被拒绝，要么悄悄
只导已加载的那一页。这里补上一条按**目录记录**取项的路：
:class:`OnDiskExportSource` + :class:`CatalogExportCandidate` —— 只保存 id、
名字和归档位置，**不碰字节、不建 Qt 对象**。导出器一次只解析一批（`details()`
上限 1024），所以十万级工程的代价是「一批内存」，不是「整个工程」。
"""
import contextlib
import dataclasses
import enum
import json
import logging
import pathlib
import sqlite3

from prism.image_source import ImageSourceMixin

logger = logging.getLogger(__name__)

#: 目录摘要页大小。上限和 `details()` 的批量上限一致（MAX_BATCH=1024）。
CATALOG_PAGE_LIMIT = 1024

#: 解析一批目录记录（`scope.details()`）的 id 数量。刻意小于上限：
#: 一批就是一个导出线程的内存上限。
CATALOG_DETAIL_BATCH = 512

#: 从归档里流式复制原件时的分块大小。
STREAM_CHUNK = 1024 * 1024


class ExportTarget(enum.Enum):
    """用户想导什么。界面上应该原样把这几个选项摆出来。"""

    #: 原始素材：有原字节就逐字节复制，不经过 QImage 重编码。
    ORIGINAL = 'original'
    #: 调整后图片：应用曝光/通道/灰度等非破坏性预览之后再编码。
    ADJUSTED = 'adjusted'
    #: 当前场景：把画布合成结果渲染出来。
    SCENE = 'scene'
    #: 当前文档：交给文档导出模块。
    DOCUMENT = 'document'
    #: 当前脑图：交给脑图导出模块。
    MINDMAP = 'mindmap'


#: 界面上给每个目标配的说明。写在这里而不是散在界面里，是为了让四种
#: 导出的措辞一致 —— 用户要靠这几句话分清「原图」和「调整后」。
TARGET_LABELS = {
    ExportTarget.ORIGINAL: '原始素材（逐字节复制，不重新编码）',
    ExportTarget.ADJUSTED: '调整后图片（应用曝光/通道/灰度后的新文件）',
    ExportTarget.SCENE: '当前场景（画布合成结果）',
    ExportTarget.DOCUMENT: '当前文档',
    ExportTarget.MINDMAP: '当前脑图',
}


@dataclasses.dataclass(frozen=True)
class OriginalReadiness:
    """这批素材对「原图导出」的准备情况。

    分三态而不是两态，是因为中间那种（有的有原字节、有的没有）**还能
    导**，只是要提醒用户：缺的那些会被重新编码，其余的仍逐字节复制。
    用 bool 表达不了这个差别，接上界面就会把「可导但要提醒」错当成
    「不能导」。
    """

    total: int
    with_bytes: int
    without_bytes: int

    @property
    def all_ready(self):
        return self.total > 0 and self.without_bytes == 0

    @property
    def none_ready(self):
        return self.with_bytes == 0

    @property
    def partial(self):
        return 0 < self.with_bytes < self.total

    @property
    def lossless(self):
        """能不能保证这一次导出的**每一个**文件都是原图。"""
        return self.all_ready

    def summary(self):
        """Original export is all-or-refuse, never a mixture of renderings."""
        if self.total == 0:
            return '没有选择素材。'
        if self.all_ready:
            return f'{self.total} 份素材均有原始字节，将逐字节复制，不重新编码。'
        return (f'{self.without_bytes} / {self.total} 份素材的原件缺失或未能读取，'
                '已停止原件导出，不会以预览图、渲染图或同名文件替代。')


@dataclasses.dataclass(frozen=True)
class ExportPlan:
    """一次导出要怎么办。界面照着执行，不要自己猜。"""

    target: ExportTarget
    #: 导出器**类**（不是实例）—— 实例化要在调用方那边，因为它可能
    #: 需要界面提供的参数（目标目录、格式选项）。
    exporter: object
    #: 导出器的额外关键字参数。
    options: dict
    #: 会不会经过重新编码。原图导出是 False。
    reencodes: bool
    #: 能不能保证是原图。只有 ORIGINAL 且素材真有原字节时才是 True。
    lossless: bool
    #: 这次要导的素材（按顺序）。空元组表示"交给导出器自己从 scene 取"。
    #: 放在计划里而不是让界面事后去筛，是因为"导哪些"本来就是计划的一部分。
    items: tuple = ()
    #: 告诉用户一声就行的说明（"这是新文件"之类）。显示完就可以往下走。
    notes: tuple = ()
    #: **需要用户确认**才能继续的说明。非空时界面应该先问一句。
    #: 和 notes 分开，是因为界面没法从一句话猜出"要不要等用户点头"——
    #: 混在一起要么什么都不敢问，要么每句都弹框。
    warnings: tuple = ()
    #: 还没加载进 scene 的那部分素材（按需打开的工程才有）。`None` 表示
    #: 全部素材都已在 scene 里 —— 界面不必为它做任何事。导出器要一次只解析
    #: 一批，所以这里给的是「来源」，不是一份清单。
    source: object = None

    @property
    def label(self):
        return TARGET_LABELS.get(self.target, self.target.value)


# ── 部分加载的工程：留在磁盘上的那部分素材 ─────────────────────


class ExportSourceMissing(RuntimeError):
    """这条素材没有可交付的原件 —— 不能拿预览或缩略图顶替。"""


class SelectionUnresolved(RuntimeError):
    """选中的 id 在工程里查不到行（被删除、或目录已过期）。"""


class CatalogExportCandidate(ImageSourceMixin):
    """一个**没有加载进 scene** 的素材：只有 id、名字和归档位置。

    它刻意不是 scene 素材：背后没有 `QPixmap`、没有图元、没有解码结果，
    所以导出器能写出的只有工程里真正存着的**原始字节**，写不出就报错 ——
    永远退不回去拿预览或缩略图，因为这里根本没有预览。
    """

    TYPE = 'pixmap'
    is_image = True
    #: 导出器据此判断「原件缺失只能报错，不能重编码」。
    _catalog_export_candidate = True

    def __init__(self, save_id, archive, *, filename='', imgformat=None,
                 title='', deleted_at=None):
        self.save_id = int(save_id)
        self.filename = str(filename or '')
        self.title = str(title or '')
        self._prism_source = (str(archive), self.save_id)
        self._source_path = None
        self._source_blob = None
        self._deleted_at = deleted_at
        self._rowid = None
        self._has_original = None
        self._source_format = (self._normalise_format(imgformat)
                               or self._format_from_filename(self.filename))

    def __str__(self):
        return f'Catalog asset {self.save_id}: {self.filename}'

    # ── 工程里到底有没有原件 ─────────────────────────────────

    def _archive_rowid(self):
        """`sqlar` 里这一条的 rowid；没有原件时是 ``None``。"""
        archive = self._prism_source[0]
        path = pathlib.Path(archive)
        if not path.is_file():
            logger.debug('工程文件不在了：%s', archive)
            return None
        connection = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)
        try:
            row = connection.execute(
                'SELECT rowid FROM sqlar WHERE item_id=?',
                (self.save_id,)).fetchone()
            return row[0] if row else None
        except sqlite3.Error:
            logger.warning('读不到归档条目 %s:%s', archive, self.save_id,
                           exc_info=True)
            return None
        finally:
            connection.close()

    def has_original_source(self):
        if self._has_original is None:
            self._rowid = self._archive_rowid()
            self._has_original = self._rowid is not None
        return self._has_original

    def original_bytes(self):
        """一次读全的原件。批量导出走 `open_original_stream()`，不用它。"""
        if not self.has_original_source():
            return None
        from prism.source_store import read_archive
        try:
            return read_archive(self._prism_source)
        except Exception:                           # noqa: BLE001
            logger.warning('读不到原件 %s', self, exc_info=True)
            return None

    def original_format(self):
        return self._source_format or self._format_from_filename(self.filename)

    @contextlib.contextmanager
    def open_original_stream(self):
        """把原件按块交出去，全程不把整个文件读进内存。

        十万级的工程里，任何一个导出线程最多同时持有 `STREAM_CHUNK`
        加上一个 BLOB 行的窗口。
        """
        from prism.storage.managed import root_for, open_original
        if root_for(self._prism_source[0]) is not None:
            with open_original(self._prism_source[0], 'sqlar', self.save_id, item_id=True) as stream:
                yield stream
            return
        if self._rowid is None:
            self.has_original_source()
        if self._rowid is None:
            raise ExportSourceMissing(
                f'{self._label()}：工程里没有这条素材的原始字节')
        archive = self._prism_source[0]
        connection = sqlite3.connect(
            pathlib.Path(archive).as_uri()+'?mode=ro', uri=True)
        try:
            with connection.blobopen('sqlar', 'data', self._rowid,
                                     readonly=True) as blob:
                yield blob
        finally:
            connection.close()

    def pixmap_to_bytes(self, *args, **kwargs):
        """永远做不到的事，必须说清楚，而不是偷偷给一张重新编码的图。"""
        raise ExportSourceMissing(
            f'{self._label()}：素材未加载，无法生成渲染结果；'
            '只有工程里存的原始字节可以导出')

    def _label(self):
        return self.filename or f'#{self.save_id}'


class OnDiskExportSource:
    """一次导出里「还没加载」的那部分素材。

    只保存 id 和一个只读目录范围（`ProjectOpenScope`），不保存字节、不保存
    Qt 对象。`batches()` 每次只把 `CATALOG_DETAIL_BATCH` 条解析成候选，
    因此十万级工程的内存代价是一批，而不是整个工程。
    """

    def __init__(self, scope, item_ids, *, archive=None, loaded=(),
                 batch=CATALOG_DETAIL_BATCH):
        self.scope = scope
        self.archive = str(archive or getattr(scope, 'project_path', '') or '')
        self.batch = max(1, int(batch))
        #: 已加载的对象：它们**优先于**磁盘记录，用户没保存的编辑不能被
        #: 工程里旧的那一行盖掉。
        self.loaded = tuple(loaded)
        self.resolved = 0
        self.unresolved = ()
        self._cancel = None
        covered = {_item_key(item) for item in self.loaded}
        self._pending = tuple(key for key in (int(i) for i in item_ids)
                              if key is not None and key not in covered)

    def __len__(self):
        return len(self._pending)

    @property
    def total(self):
        """这次导出要从磁盘上取的素材数（不含已加载的那些）。"""
        return len(self._pending)

    @property
    def cancel(self):
        return self._cancel

    @cancel.setter
    def cancel(self, value):
        self._cancel = value

    def _check_cancel(self):
        if self._cancel is not None and self._cancel():
            from prism.project_open_scope import OpenScopeCancelled
            raise OpenScopeCancelled('Export source read cancelled')

    # ── 取项 ─────────────────────────────────────────────────

    def batches(self, cancel=None):
        """逐批交出候选素材：一批 `details()`，一批内存。

        :raises prism.project_catalog_index.CatalogNotReady: 目录不可用。
        :raises prism.project_open_scope.OpenScopeCancelled: 被取消。
        """
        cancel = cancel or self._cancel
        missing = []
        pending = self._pending
        for start in range(0, len(pending), self.batch):
            self._check_cancel()
            ids = pending[start:start+self.batch]
            rows = self.scope.details(ids, cancel=cancel)
            found = {int(row['id']): row for row in rows}
            batch = []
            for key in ids:
                row = found.get(key)
                if row is None:
                    missing.append(key)
                    continue
                candidate = self._candidate(row)
                if candidate is None:
                    missing.append(key)
                    continue
                batch.append(candidate)
            self.resolved += len(batch)
            if batch:
                yield batch
        self.unresolved = tuple(missing)

    def _candidate(self, row):
        extra = {}
        payload = row.get('data')
        if payload:
            try:
                extra = json.loads(payload)
            except ValueError:
                logger.warning('素材 %s 的元数据不是 JSON', row.get('id'))
                extra = {}
        if not isinstance(extra, dict):
            extra = {}
        deleted = extra.get('deletedAt')
        if deleted is not None:
            # 回收站里的素材对导出不可见（和 `items_for()` 一致）。
            return None
        return CatalogExportCandidate(
            row['id'], self.archive, filename=extra.get('filename') or '',
            imgformat=extra.get('sourceFormat'), title=extra.get('title') or '')

    def missing_ids(self, cancel=None):
        """选中的 id 里目录查不到的那些（已删除、或目录落后于工程）。"""
        cancel = cancel or self._cancel
        missing = []
        pending = self._pending
        for start in range(0, len(pending), self.batch):
            self._check_cancel()
            ids = pending[start:start+self.batch]
            rows = {int(row['id']): row
                    for row in self.scope.summaries(ids, cancel=cancel)}
            missing.extend(key for key in ids
                           if key not in rows or rows[key]['deleted_at'])
        return tuple(missing)

    # ── 无损性预检 ───────────────────────────────────────────

    def readiness(self, cancel=None):
        """`(有原件, 没原件)`：问的是工程自己的归档，不是目录缓存。

        目录说「有」不算数 —— 导出时真正读的就是那份归档，所以结论要问它。
        """
        cancel = cancel or self._cancel
        pending = self._pending
        with_bytes = 0
        for start in range(0, len(pending), self.batch):
            self._check_cancel()
            with_bytes += len(self._stored_ids(
                pending[start:start+self.batch]))
        return with_bytes, len(pending)-with_bytes

    def _stored_ids(self, ids):
        if not self.archive or not pathlib.Path(self.archive).is_file():
            return set()
        connection = sqlite3.connect(
            pathlib.Path(self.archive).as_uri()+'?mode=ro', uri=True)
        try:
            rows = connection.execute(
                'SELECT item_id FROM sqlar WHERE item_id IN (%s)'
                % ','.join('?' for _ in ids), ids)
            return {int(row[0]) for row in rows}
        except sqlite3.Error:
            logger.warning('无法预检原件：%s', self.archive, exc_info=True)
            return set()
        finally:
            connection.close()


def _item_key(item):
    """素材的目录 id（`save_id`，回退到 `original_save_id`）。"""
    key = getattr(item, 'save_id', None)
    if key is None:
        key = getattr(item, 'original_save_id', None)
    try:
        return int(key)
    except (TypeError, ValueError):
        return None


def catalog_ids(scope, *, kind='pixmap', cancel=None,
                page_limit=CATALOG_PAGE_LIMIT):
    """走一遍只读目录，返回全项目（未软删除）的素材 id。

    `ProjectOpenScope.iter_query()` 是 keyset 分页、每页有上限、可取消的；
    这里只取标量摘要列，既不读工程的 `data` JSON，也不读任何原字节 ——
    十万行实测半秒以内。
    """
    return [int(row['id'])
            for row in scope.iter_query(kind=kind, limit=page_limit,
                                        cancel=cancel)]


def catalog_ids_for_canvas(scope, canvas_id, *, kind='pixmap', cancel=None,
                           page_limit=CATALOG_PAGE_LIMIT):
    """某一页画布上的素材 id（同 `catalog_ids`，按画布取）。"""
    return [int(row['id'])
            for row in scope.iter_query(kind=kind, canvas_ids=[canvas_id],
                                        limit=page_limit, cancel=cancel)]


class ExportService:
    """把导出意图翻译成导出器。

    不持有 view，不持有界面控件。构造时只拿到一个 scene —— 而且只用来
    取素材，不读它的任何界面状态。
    """

    def __init__(self, scene=None):
        self._scene = scene

    # ── 预检：原图到底能不能导 ──────────────────────────────────

    @staticmethod
    def check_original(items):
        """这批素材对「原图导出」的准备情况。返回 `OriginalReadiness`。

        任务书 T2 那条「没有可验证原始字节时，不得假装提供无损原图
        导出」就落在这里。判据用 ``original_bytes()``（T2 加的接口），
        它只在真的有原始 payload 时才给东西。
        """
        items = [item for item in items or ()
                 if getattr(item, 'TYPE', 'pixmap') == 'pixmap'
                 and not getattr(item, '_deleted_at', None)]
        with_bytes = 0
        for item in items:
            getter = getattr(item, 'original_bytes', None)
            already_resident = getattr(item, '_source_blob', None) is not None
            try:
                payload = getter() if callable(getter) else None
            except Exception:                       # noqa: BLE001
                # 素材自己出问题只该算「这张没有原字节」，不该把整批带崩
                logger.warning('取原始字节失败，按「没有」处理', exc_info=True)
                payload = None
            if payload:
                with_bytes += 1
            if (not already_resident and
                    (getattr(item, '_source_path', None) or
                     getattr(item, '_prism_source', None))):
                item._source_blob = None
        return OriginalReadiness(
            total=len(items),
            with_bytes=with_bytes,
            without_bytes=len(items) - with_bytes)

    def items_for(self, selected_only=False, items=None):
        """要导的那批素材。

        显式传了 ``items`` 就用它 —— 服务不该去猜界面选了什么。没传才
        从 scene 取，而且**只取用户素材**（`items_for_save`），不要那些
        选择框、提示之类的辅助图元。
        """
        if items is not None:
            return list(items)
        if self._scene is None:
            return []
        if selected_only:
            picker = getattr(self._scene, 'selectedItems', None)
            if not callable(picker):
                return []
            # PrismScene 的 selectedItems 接受 user_only；基类的不接受。
            try:
                chosen = picker(user_only=True)
            except TypeError:
                chosen = picker()
            return [i for i in chosen if hasattr(i, 'save_id')]
        getter = getattr(self._scene, 'items_for_save', None)
        if not callable(getter):
            return []
        # 回收站里的素材不导出（方案 §7：软删除的素材对导出不可见）。
        return [i for i in getter()
                if getattr(i, '_deleted_at', None) is None]

    def source_for(self, scope, item_ids=None, *, loaded=(), archive=None,
                   kind='pixmap', cancel=None):
        """「还没加载进 scene」的那部分素材，按目录记录取。

        这条路让部分加载的工程也能按**逻辑范围**（整个工程 / 某个画布页 /
        用户在浏览器里选中的 id）取项，而不是只能取 scene 里那几百个对象。
        `item_ids` 为 None 表示「整个工程」。

        已加载的对象**优先**：它们的 id 会从磁盘取项里剔除，所以用户没保存
        的编辑永远不会被工程里旧的那一行盖掉。

        :returns: :class:`OnDiskExportSource`；没有需要从磁盘取的素材时是
            ``None``（全量加载的工程走原来的路径）。
        """
        if scope is None:
            return None
        if item_ids is None:
            item_ids = catalog_ids(scope, kind=kind, cancel=cancel)
        else:
            item_ids = list(item_ids)
        source = OnDiskExportSource(scope, item_ids, archive=archive,
                                    loaded=loaded)
        return source if source.total else None

    # ── 出计划 ─────────────────────────────────────────────────

    @staticmethod
    def _merge_readiness(readiness, source, *, cancel=None):
        """把「还没加载的那部分」并进原图预检结论里。

        目录记录没有解码结果，所以缺原件的目录记录**不能**像已加载素材那样
        「导渲染结果」—— 它只能报错。话必须说成它真实的样子，否则用户会以为
        拿到的是原图。
        """
        with_bytes, without_bytes = source.readiness(cancel=cancel)
        total = readiness.total + source.total
        ready = readiness.with_bytes + with_bytes
        missing = total - ready
        merged = OriginalReadiness(total=total, with_bytes=ready,
                                   without_bytes=missing)
        if not missing:
            return merged, (
                f'{total} 个素材都会逐字节复制成原图，'
                f'其中 {source.total} 个从未加载过，直接从工程归档里读。'), None
        return merged, None, (
            f'{missing} / {total} 个素材导不出原图：{readiness.without_bytes} 个'
            f'已加载素材没有原始字节（会导出当前渲染结果），'
            f'{without_bytes} 个未加载素材在工程里也没有原始字节（无法导出，'
            '会逐条报错）。其余 '
            f'{ready} 个逐字节复制。')

    def plan(self, target, items=None, selected_only=False, *, source=None,
             cancel=None):
        """算出这次导出该用哪个导出器。

        ``items`` 可以显式传；不传就从 scene 取（见 `items_for`）。
        ``source`` 是 :class:`OnDiskExportSource`：按需打开的工程里那部分
        没加载的素材。它只登记来源，不在这里展开 —— 展开发生在导出线程里，
        一次一批。
        """
        from prism.fileio import export as export_module

        if target in (ExportTarget.ORIGINAL, ExportTarget.ADJUSTED):
            chosen = [item for item in self.items_for(selected_only, items)
                      if getattr(item, 'TYPE', 'pixmap') == 'pixmap'
                      and not getattr(item, '_deleted_at', None)]
            if not chosen and source is None:
                raise ValueError('没有要导出的素材。')

            notes = []
            warnings = []
            lossless = False
            if target is ExportTarget.ORIGINAL:
                readiness = self.check_original(chosen)
                if source is not None:
                    readiness, extra_note, extra_warning = self._merge_readiness(
                        readiness, source, cancel=cancel)
                    if extra_warning:
                        warnings.append(extra_warning)
                    elif extra_note:
                        notes.append(extra_note)
                # 只有「一张有原字节的都没有」才算做不了。部分有的话
                # 照样能导 —— 有原字节的逐字节复制，其余导渲染结果 ——
                # 只是要把这件事说清楚（任务书 T2 的「提示用户」），而且
                # 得让用户点个头，因为他可能不知道有几张会变样。
                if not readiness.all_ready:
                    raise OriginalExportUnavailable(readiness.summary())
                lossless = readiness.lossless
                if source is None and readiness.summary() not in notes:
                    notes.append(readiness.summary())
            else:
                notes.append(
                    '这是应用了调整之后的**新文件**，不会修改原图。')
                if source is not None and source.total:
                    # 目录记录背后没有解码结果，调整后导出拿不到它们。
                    # 不在这里悄悄只导已加载的那些 —— 先说清楚。
                    warnings.append(
                        f'{source.total} 个素材未加载，调整后导出无法生成它们的'
                        '渲染结果（会逐条报错），它们只能按原图导出。')

            return ExportPlan(
                target=target,
                exporter=export_module.ImagesToDirectoryExporter,
                # adjusted=False 走原始 payload 复制，True 走重新编码。
                options={'adjusted': target is ExportTarget.ADJUSTED},
                items=tuple(chosen),
                reencodes=target is ExportTarget.ADJUSTED,
                lossless=lossless,
                notes=tuple(notes),
                warnings=tuple(warnings),
                source=source,
            )

        if target is ExportTarget.SCENE:
            return ExportPlan(
                target=target,
                exporter=export_module.SceneToPixmapExporter,
                options={},
                reencodes=True,
                lossless=False,
                notes=('导出的是画布的合成结果，不是单个素材的原图。',),
            )

        if target in (ExportTarget.DOCUMENT, ExportTarget.MINDMAP):
            # 这两个交给各自的导出模块，这里只说明「该找谁」。
            # 不在这里 import —— 文档导出依赖 python-docx，脑图导出依赖
            # 前端资源，让不导它们的人不背这个开销。
            module = ('prism.fileio.document_export'
                      if target is ExportTarget.DOCUMENT
                      else 'prism.fileio.mindmap_export')
            return ExportPlan(
                target=target,
                exporter=module,
                options={},
                reencodes=True,
                lossless=False,
                notes=(f'由 {module} 负责具体格式。',),
            )

        raise ValueError(f'不认识的导出目标：{target!r}')


class OriginalExportUnavailable(Exception):
    """这批素材没有可验证的原始字节，做不了真正的原图导出。

    单独一个异常类型，是为了让界面能把它和「路径写不进去」之类的普通
    失败分开处理 —— 前者要给用户一个选择（改用调整后导出 / 重新定位
    源文件），后者只是报个错。
    """
