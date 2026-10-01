"""素材导入：分类、附件存储、结果汇总。

任务书 T9 要求「服务不得持有整个 `PrismGraphicsView`，依赖必须显式传入」，
并点名 ``AssetImportService：导入素材和附件``。

**为什么值得单独一层**：底层的 `load_images` / `load_media` 已经是干净的
模块级函数（只吃 filenames/pos/scene/worker），不用重写。真正散着的是：

  - 扩展名规则分三处 —— 视频在 `items.VIDEO_EXTENSIONS`，3D 在
    `glb_preview.GLB_EXTENSIONS`，而图片那份是 `main_controls._IMAGE_EXTS`
    这个**私有**表，还把 `.glb/.gltf` 混在里面（它们是 3D 模型，走
    `PrismGlbItem`，和图片完全不是一回事）。
  - 附件 id 的分配埋在 `PrismGraphicsView._store_imported_attachment` 里，
    没法单独测。

这一层把这两件事收到一起，而且不碰任何控件。
"""
import dataclasses
import logging
import os

from prism.glb_preview import GLB_EXTENSIONS
from prism.items import VIDEO_EXTENSIONS

logger = logging.getLogger(__name__)

#: 图片扩展名。
#:
#: `main_controls._IMAGE_EXTS` 里有同样一份，但不能直接引用它：那个表是
#: 私有的，而且把 `.glb/.gltf` 也算进去了 —— 在拖放那条路上那样没问题
#: （都是"能丢进来的文件"），但拿它做分类会把 3D 模型当图片。
IMAGE_EXTENSIONS = frozenset({
    '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.tiff', '.tif',
    '.svg', '.ico', '.jfif', '.jpe', '.ppm', '.pgm', '.pbm', '.xbm',
    '.xpm', '.psd', '.exr', '.hdr',
})

#: 分类名。界面按这个决定交给哪个加载函数。
IMAGE = 'image'
VIDEO = 'video'
MODEL = 'model'
OTHER = 'other'

KINDS = (IMAGE, VIDEO, MODEL, OTHER)


def classify(path):
    """这个文件属于哪一类。

    3D 先判：`.glb` 在 `_IMAGE_EXTS` 里也有，先判它才不会被图片那条路
    截走。
    """
    # QUrl is deliberately accepted too: browser capture hands the legacy
    # loader a URL object, and classifying its Python repr would turn every
    # remote image into an "unknown" file.
    candidate = path.path() if hasattr(path, 'path') else str(path)
    extension = os.path.splitext(candidate)[1].lower()
    if extension in GLB_EXTENSIONS:
        return MODEL
    if extension in VIDEO_EXTENSIONS:
        return VIDEO
    if extension in IMAGE_EXTENSIONS:
        return IMAGE
    return OTHER


def classify_all(paths):
    """分成四组，组内保持原顺序。

    保序是有意的：用户拖进来的顺序常常就是他想看到的摆放顺序，
    按类型打乱再摆会让人以为点错了。
    """
    groups = {kind: [] for kind in KINDS}
    for path in paths:
        groups[classify(path)].append(path)
    return groups


@dataclasses.dataclass
class ImportReport:
    """一次导入的结果，给界面显示和调用方决策用。"""

    images: list = dataclasses.field(default_factory=list)
    videos: list = dataclasses.field(default_factory=list)
    models: list = dataclasses.field(default_factory=list)
    skipped: list = dataclasses.field(default_factory=list)
    errors: list = dataclasses.field(default_factory=list)

    @property
    def accepted(self):
        return len(self.images) + len(self.videos) + len(self.models)

    @property
    def total(self):
        return self.accepted + len(self.skipped) + len(self.errors)

    def summary(self):
        """一行结果，中文，直接能显示。"""
        parts = []
        if self.images:
            parts.append(f'{len(self.images)} 张图片')
        if self.videos:
            parts.append(f'{len(self.videos)} 个视频')
        if self.models:
            parts.append(f'{len(self.models)} 个 3D 模型')
        if not parts:
            parts.append('没有可导入的文件')
        text = '、'.join(parts)
        if self.skipped:
            text += f'；跳过 {len(self.skipped)} 个不认识的文件'
        if self.errors:
            text += f'；{len(self.errors)} 个失败'
        return text

    def failure_detail(self, limit=5):
        """失败原因的简要列表，最多 ``limit`` 行。"""
        lines = []
        for path, reason in self.errors[:limit]:
            lines.append(f'{os.path.basename(str(path))}：{reason}')
        if len(self.errors) > limit:
            lines.append(f'……还有 {len(self.errors) - limit} 个')
        return lines


def plan(paths):
    """把一批路径整理成一份导入计划，不碰任何文件内容。

    这一层是纯的：给同样的路径必然得到同样的分组和报告骨架，所以界面
    可以在真正开始读文件之前就先告诉用户"这里面有几个不认识的"。
    """
    groups = classify_all(paths)
    report = ImportReport(
        images=list(groups[IMAGE]),
        videos=list(groups[VIDEO]),
        models=list(groups[MODEL]),
        skipped=[(p, '不认识的格式') for p in groups[OTHER]],
    )
    return report


# ── 附件 ─────────────────────────────────────────────────────────

def next_attachment_id(attachments):
    """下一个可用的附件 id。

    取最大值加一，不是用长度：中间删掉一个之后，拿长度当 id 会撞上
    已有的。空表从 1 开始，和现有工程里存的 id 对得上。
    """
    return max(attachments.keys(), default=0) + 1


def mime_for_suffix(suffix):
    """扩展名 → (MIME, 干净的扩展名)。"""
    clean = (suffix or 'png').lower().lstrip('.') or 'png'
    if clean in ('jpg', 'jpeg'):
        return 'image/jpeg', clean
    if clean == 'svg':
        return 'image/svg+xml', clean
    return f'image/{clean}', clean


def store_attachment(attachments, data, extension=None, name=None):
    """把一个附件存进附件表，返回它的 id。

    只写数据，不生成 URL —— URL 的拼法是界面那边的事
    （`widgets.document_panel.attachment_url`），服务不该依赖它。

    ``attachments`` 可以是普通 dict，也可以是 `StoredAttachments` —— 后者
    只在真正取的时候才把 blob 读出来，而这里写进去的本来就在内存里。
    """
    if attachments is None:
        raise ValueError('附件表不存在')
    if not data:
        raise ValueError('空数据不能当附件存')
    mime, suffix = mime_for_suffix(extension)
    attachment_id = next_attachment_id(attachments)
    attachments[attachment_id] = (
        name or f'imported.{suffix}', mime, bytes(data))
    return attachment_id


def drop_attachments(attachments, attachment_ids):
    """删掉一批附件，返回真正删掉的个数。

    找不到的忽略掉，不报错 —— 调用方通常在清理一段可能已经不全的引用，
    为"这个本来就不在"而失败没有意义。
    """
    removed = 0
    for attachment_id in list(attachment_ids or ()):
        if attachment_id in attachments:
            del attachments[attachment_id]
            removed += 1
    return removed


def referenced_attachments(html_or_tree):
    """从文档正文或脑图树里挑出被引用到的附件 id。

    文档正文存的是 HTML，脑图存的是嵌套 dict，两者都可能出现
    ``prism://attach/<id>`` 形式的引用。用正则扫一遍比分别解析两种结构
    简单得多，而且这里只关心 id。

    前缀从附件模块拿，不在本地写死：那个串目前在两处各定义了一份
    （`fileio/attachments.py` 和 `widgets/document_panel.py`，值相同），
    将来改一处，这里的正则跟着变。
    """
    import re

    from prism.fileio.attachments import ATTACHMENT_PREFIX

    pattern = re.compile(re.escape(ATTACHMENT_PREFIX) + r'(\d+)')
    text = html_or_tree if isinstance(html_or_tree, str) else repr(html_or_tree)
    return {int(match) for match in pattern.findall(text)}
