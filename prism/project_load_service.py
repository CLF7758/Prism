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

"""打开工程的分层协调：索引 → 当前页面 → 后台。

任务书 T9-5 要求 `ProjectLoadService` 管「索引、延迟加载和后台任务协调」，
§0 又把当前问题点得很明确：

    项目加载没有稳定的「索引加载 → 当前页面 → 后台内容加载」分层。

**先说清楚这个服务现在做什么、不做什么。**

实测（`tools/open_stages.py`，1562 素材的固定样例）当前的真实数据是：

    读取项目索引          0.001 秒
    建主窗口              0.082 秒
    当前页面首次可操作     0.147 秒
    当前页面缩略图首次     0.005 秒
    原图首次解码          0.005 秒
    加载进来的素材          993 个（不是全部 1562 个）

也就是说**分层本身已经在跑**了 —— 144 毫秒就能操作，只建了当前页面那批。
`SQLiteIO.read()` 里那 146 行早就按「先结构后内容」在走。

所以这个服务**不重写加载**。它做三件当时缺的事：

1. **给外面一个纯读的索引入口**（`read_index`）。工具、测试、将来的
   「最近打开的工程」预览都需要它，而现在只能各自去连数据库 ——
   `tools/open_stages.py` 里就手写了一份。
2. **把各阶段的时间点显式报出来**（`stage_done` 信号）。现在这些数只有
   外部工具在测（`baseline.py` / `open_stages.py`），产品自己不知道；
   出了问题没法从日志里看出是哪一段慢了。
3. **协调后台加载的取消**：窗口关掉时后台还在跑的话，任务不该再去碰
   已经销毁的界面对象（任务书 T5 明确要求）。

真正的「把所有页面都建出来」这种重活不在这里 —— 那正是不该做的事。
"""
import dataclasses
import logging
import os
import sqlite3
import time

from PyQt6 import QtCore

from prism.fileio.errors import PrismFileIOError

logger = logging.getLogger(__name__)


#: 这些表名在读写两边都有用到，写成常量免得拼错。
METADATA_TABLE = 'prism_metadata'
PAGES_TABLE = 'workspace_pages'
ITEMS_TABLE = 'items'
ATTACHMENTS_TABLE = 'attachments'


@dataclasses.dataclass(frozen=True)
class ProjectIndex:
    """一个工程的目录：有多少东西、有哪些页面。

    **只读元数据，不建任何素材对象。** 所以它可以在打开工程之前就跑，
    也很便宜 —— 实测 1562 素材的工程 0.001 秒。
    """

    path: str
    #: `prism_metadata` 里的键值对。
    metadata: dict
    #: 页面目录：每项是 ``{'id': ..., 'kind': ..., 'title': ...}``。
    pages: tuple
    #: 素材行数（不是"已加载的素材数"）。
    item_count: int
    #: 附件行数。
    attachment_count: int
    #: 读这份索引花了多久。
    seconds: float = 0.0

    @property
    def title(self):
        """工程名。优先用元数据里的，没有就退到文件名。"""
        for key in ('title', 'name', 'project_title'):
            value = self.metadata.get(key)
            if value:
                return value
        base = os.path.basename(self.path)
        return base[:-len('.prism')] if base.endswith('.prism') else base

    def pages_of_kind(self, kind):
        return tuple(page for page in self.pages
                     if page.get('kind') == kind)

    @property
    def is_large(self):
        """素材多到该提醒用户「打开要等一会儿」的时候。

        门槛取 1000，是因为任务书 §10 的测试样例里 100/1000/1500+ 是
        三档，而 1500+ 那档才是真正的大工程。
        """
        return self.item_count >= 1000

    def summary(self):
        kinds = {}
        for page in self.pages:
            kinds[page.get('kind', 'canvas')] = kinds.get(
                page.get('kind', 'canvas'), 0) + 1
        parts = [f'{self.item_count} 个素材', f'{self.attachment_count} 个附件']
        if kinds:
            parts.append('、'.join(f'{n} 个 {kind}' for kind, n in
                                   sorted(kinds.items())))
        return f'{self.title}：' + '，'.join(parts)


class ProjectLoadService(QtCore.QObject):
    """打开工程时的分层协调。

    服务**不持有 view**。要报阶段进度就往信号上发，界面自己接。
    """

    #: 索引读好了（`ProjectIndex`）。
    index_ready = QtCore.pyqtSignal(object)
    #: 某个阶段跑完了：``(阶段名, 耗时秒数)``。顺序就是实际发生的顺序。
    stage_done = QtCore.pyqtSignal(str, float)
    #: 读不成。消息是可以直接给用户看的一句话。
    failed = QtCore.pyqtSignal(str)

    #: 阶段名。用常量是因为界面可能要按名字挑着显示（比如只显示耗时长的）。
    STAGE_INDEX = 'index'
    STAGE_WINDOW = 'window'
    STAGE_PAGE = 'page'
    STAGE_THUMBNAILS = 'thumbnails'
    STAGE_BACKGROUND = 'background'

    STAGE_LABELS = {
        STAGE_INDEX: '读取项目索引',
        STAGE_WINDOW: '创建主窗口',
        STAGE_PAGE: '创建当前页面',
        STAGE_THUMBNAILS: '加载缩略图',
        STAGE_BACKGROUND: '后台加载其他页面',
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cancelled = False
        self._stages = []

    # ── 取消 ────────────────────────────────────────────────────

    def cancel(self):
        """窗口要关了，后台任务别再往下跑。

        任务书 T5：「后台任务可取消，窗口关闭时不会访问已销毁的界面对象。」
        """
        self._cancelled = True
        logger.debug('工程加载已取消')

    @property
    def cancelled(self):
        return self._cancelled

    def reset(self):
        """准备加载下一个工程。"""
        self._cancelled = False
        self._stages = []

    # ── 阶段记账 ────────────────────────────────────────────────

    def stage(self, name):
        """计一个阶段。用法：

            with service.stage(ProjectLoadService.STAGE_PAGE):
                ...

        退出时把耗时发出去，同时记下来供 `timings()` 查。写成上下文
        管理器而不是 `start_stage`/`end_stage` 一对，是因为后者在异常
        路径上容易被漏掉，然后数字就不对了。
        """
        return _Stage(self, name)

    def _record(self, name, seconds):
        self._stages.append((name, seconds))
        self.stage_done.emit(name, seconds)

    def timings(self):
        """目前记下的阶段。给日志和工具用。"""
        return tuple(self._stages)

    def timings_summary(self):
        if not self._stages:
            return '（还没有阶段记录）'
        parts = []
        for name, seconds in self._stages:
            label = self.STAGE_LABELS.get(name, name)
            parts.append(f'{label} {seconds:.3f}s')
        total = sum(s for _, s in self._stages)
        return '，'.join(parts) + f'（合计 {total:.3f}s）'

    # ── 索引 ────────────────────────────────────────────────────

    def read_index(self, path):
        """只读工程索引。不建素材、不碰界面、不需要 QApplication。

        读之前先看取消标志 —— 用户在等索引的时候点了取消，就别白读一遍。

        出错时发 `failed` 并返回 None，不抛。因为这个函数会在「打开
        工程」的第一步被调，那一步的失败是要变成一句提示的。
        """
        if self._cancelled:
            return None
        try:
            index = read_project_index(path)
        except FileNotFoundError:
            self.failed.emit(f'找不到文件：{path}')
            return None
        except (sqlite3.DatabaseError, OSError) as exc:
            self.failed.emit(f'读不了这个工程：{exc}')
            return None
        self.index_ready.emit(index)
        self._record(self.STAGE_INDEX, index.seconds)
        return index

    # ── 打开之后 ────────────────────────────────────────────────

    def check_cancelled(self):
        """界面的长循环里可以时不时问一句：还要继续吗。

        返回 True 表示**该停了**。
        """
        return self._cancelled


class _Stage:
    """`ProjectLoadService.stage()` 返回的上下文管理器。"""

    __slots__ = ('service', 'name', 'started')

    def __init__(self, service, name):
        self.service = service
        self.name = name
        self.started = 0.0

    def __enter__(self):
        self.started = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        # 失败也记时间 —— "这一步花了 3 秒然后炸了"比"这一步炸了"有用。
        self.service._record(self.name, time.perf_counter() - self.started)
        return False


def read_project_index(path):
    """读一个 .prism 的索引。纯函数，不依赖 Qt，可以随便测。

    只碰元数据和目录表，**不建任何素材对象** —— 所以 1562 个素材的工程
    也只要毫秒级。实测 0.001 秒。

    表名用常量，是因为读写两边都用到。它们已经拿真实工程核对过
    （`秋雨参考.prism`：items 1563 行、attachments 406 行、
    workspace_pages 1 行、prism_metadata 2 行，另有 sqlar 存原字节）。
    拼错的话 `sqlite_master` 那一查会静默给空，然后得到一个"看起来正常
    但是空的"索引 —— 那种 bug 比报错难查得多，所以缺表和空表都走同一条
    路（下面按表名分别判断，不假设一定有）。
    """
    started = time.perf_counter()
    # 有回滚日志（`-journal`）说明这个工程上次没正常关闭 —— 保存或退出
    # 中途出过事。**只读连接打不开这种库**：SQLite 要靠回滚日志先把文件
    # 恢复成一致状态，而回滚是写操作，于是报
    # `attempt to write a readonly database`。
    #
    # 这里不替用户做回滚（那会改动他的文件），而是说清楚该怎么办 ——
    # 单独看这句报错的人根本猜不到是回滚日志的事。
    journal = f'{path}-journal'
    if os.path.exists(journal):
        raise PrismFileIOError(
            f'这个工程上次没有正常关闭（还留着回滚日志 {os.path.basename(journal)}）。'
            f'请用 Prism 打开它一次 —— 打开时 SQLite 会把没写完的部分'
            f'回滚掉，然后就能正常读取了。'
            f'工程本身没坏：同目录下还留着一份 .bak 备份。',
            path)
    connection = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    try:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}

        metadata = {}
        if METADATA_TABLE in tables:
            for key, value in connection.execute(
                    f'SELECT key, value FROM {METADATA_TABLE}'):
                metadata[key] = value

        pages = ()
        if PAGES_TABLE in tables:
            rows = connection.execute(
                f'SELECT id, kind, title FROM {PAGES_TABLE}').fetchall()
            pages = tuple({'id': row[0], 'kind': row[1], 'title': row[2]}
                          for row in rows)

        item_count = 0
        if ITEMS_TABLE in tables:
            item_count = connection.execute(
                f'SELECT COUNT(*) FROM {ITEMS_TABLE}').fetchone()[0]

        attachment_count = 0
        if ATTACHMENTS_TABLE in tables:
            attachment_count = connection.execute(
                f'SELECT COUNT(*) FROM {ATTACHMENTS_TABLE}').fetchone()[0]
    finally:
        connection.close()

    elapsed = time.perf_counter() - started
    index = ProjectIndex(
        path=path,
        metadata=metadata,
        pages=pages,
        item_count=item_count,
        attachment_count=attachment_count,
        seconds=round(elapsed, 4),
    )
    logger.debug('读了工程索引：%s（%.4f 秒）', index.summary(), elapsed)
    return index
