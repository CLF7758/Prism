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
"""
import dataclasses
import enum
import logging

logger = logging.getLogger(__name__)


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
        """给用户看的一句话。界面直接拿去显示，不用自己拼。"""
        if self.total == 0:
            return '没有选中任何素材。'
        if self.all_ready:
            return (f'{self.total} 个素材都有原始字节，'
                    f'可以逐字节复制，不会重新编码。')
        if self.none_ready:
            return (f'这 {self.total} 个素材都没有保留原始字节（可能是画布上'
                    f'生成的、或来自旧版工程），没法做真正的原图导出。')
        return (f'{self.without_bytes} / {self.total} 个素材没有原始字节，'
                f'它们只能导出当前渲染结果（会重新编码）；'
                f'其余 {self.with_bytes} 个仍逐字节复制。')


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

    @property
    def label(self):
        return TARGET_LABELS.get(self.target, self.target.value)


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
        items = list(items or ())
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
        return list(getter()) if callable(getter) else []

    # ── 出计划 ─────────────────────────────────────────────────

    def plan(self, target, items=None, selected_only=False):
        """算出这次导出该用哪个导出器。

        ``items`` 可以显式传；不传就从 scene 取（见 `items_for`）。
        """
        from prism.fileio import export as export_module

        if target in (ExportTarget.ORIGINAL, ExportTarget.ADJUSTED):
            chosen = self.items_for(selected_only, items)
            if not chosen:
                raise ValueError('没有要导出的素材。')

            notes = []
            warnings = []
            lossless = False
            if target is ExportTarget.ORIGINAL:
                readiness = self.check_original(chosen)
                # 只有「一张有原字节的都没有」才算做不了。部分有的话
                # 照样能导 —— 有原字节的逐字节复制，其余导渲染结果 ——
                # 只是要把这件事说清楚（任务书 T2 的「提示用户」），而且
                # 得让用户点个头，因为他可能不知道有几张会变样。
                if readiness.none_ready:
                    raise OriginalExportUnavailable(readiness.summary())
                lossless = readiness.lossless
                if readiness.partial:
                    warnings.append(readiness.summary())
                else:
                    notes.append(readiness.summary())
            else:
                notes.append(
                    '这是应用了调整之后的**新文件**，不会修改原图。')

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
