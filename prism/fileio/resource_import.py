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

"""Import a folder (or a ``.zip``) back into the resource tree.

这是 :mod:`prism.fileio.resource_export` 的逆运算，规则和「拖一个文件夹
进画布」是同一套（用户定的）：

* **一层目录一个容器** —— 层级跟着目录走；
* 中间层只有子目录、自己没有内容的，**不建空容器**；
* 重名由落地那边自动加序号，不覆盖任何东西；
* 不认识的扩展名跳过，不影响其它文件；
* 目录名 / 文件名先清洗成合法标题（和导出侧用同一个 ``safe_name``）。

三类分区的容器不一样，因为内容的单位不一样：

======== ========================================== ==================
分区      一个**目录**变成                             一个**文件**变成
======== ========================================== ==================
画布      画布页（目录里的图片/视频归它）              图片/视频/3D 模型 → 该画布页的素材
文档      文件夹                                       ``.docx`` → 文档页
脑图      文件夹                                       ``.xmind`` → 脑图页
======== ========================================== ==================

**画布为什么要多一个文件夹**：资源树里只有「文件夹」能挂子节点，画布页是
叶子。所以一个「下面还有子目录」的目录，除了自己的画布页之外，还会得到一个
**同名文件夹**来撑住层级 —— 这正是工程自带的老结构（打开旧工程时，带子页面
的页面就是被转成「同名文件夹 + 页面」的）。

这一层只读文件系统：扫出计划、解开压缩包、挡住不安全的路径。它不碰工程，
也不碰界面 —— 落地在 :mod:`prism.actions.import_workflow`。
"""
import dataclasses
import logging
import os
import stat
import zipfile

from prism.asset_import_service import OTHER, classify
from prism.fileio.resource_export import safe_name
from prism.i18n import _

logger = logging.getLogger(__name__)


#: 分区 -> 该分区里「一个文件 = 一个页面」的扩展名。
#: 画布不在这张表里：它的容器是目录（见模块开头的规则）。
PAGE_EXTENSIONS = {'document': ('.docx',), 'mindmap': ('.xmind',)}

SECTION_TITLES = {'canvas': '画布', 'mindmap': '脑图', 'document': '文档'}

#: 节点标题的上限（:`ResourceTreeService._title` 的硬限制）。留出几个
#: 字符给落地时的「 2」后缀。
MAX_TITLE = 76

#: 压缩包的安全上限。导入的 zip 是不可信输入，这两条是底线。
MAX_ARCHIVE_ENTRIES = 20000
MAX_ARCHIVE_BYTES = 2 * 1024 ** 3


class ResourceImportError(Exception):
    """来源用不了：目录不存在、压缩包坏了，或者里面没有能导入的东西。"""


@dataclasses.dataclass(frozen=True)
class ImportNode:
    """计划里的一个节点。

    ``parent_path`` 用 ``relative_path`` 指父节点，``None`` 表示挂在导入
    目标（右键的那个节点）下面。
    """

    relative_path: str
    parent_path: object
    node_type: str                  # 'folder' | 'page'
    title: str
    page_kind: str                  # 'canvas' | 'document' | 'mindmap'
    #: 文档页 / 脑图页的源文件。
    source_file: object = None
    #: 画布页的素材文件。
    assets: tuple = ()


@dataclasses.dataclass(frozen=True)
class ImportPlan:
    """一次导入将要建出来的东西。落地前先给用户看的就是它。"""

    section: str
    root_title: str
    nodes: tuple = ()
    #: ``(相对路径, 原因)``——没导的文件，如实列出来。
    skipped: tuple = ()
    notes: tuple = ()

    @property
    def folders(self):
        return tuple(node for node in self.nodes if node.node_type == 'folder')

    @property
    def pages(self):
        return tuple(node for node in self.nodes if node.node_type == 'page')

    @property
    def asset_files(self):
        return tuple(path for node in self.nodes for path in node.assets)

    def summary(self):
        """给用户看的一句话。"""
        if self.section == 'canvas':
            return _(
                'Will create {canvases} canvas(es) and {folders} folder(s), '
                'with {assets} asset(s).').format(
                    canvases=len(self.pages), folders=len(self.folders),
                    assets=len(self.asset_files))
        return _(
            'Will create {pages} page(s) and {folders} folder(s).').format(
                pages=len(self.pages), folders=len(self.folders))


def _parent_path(relative):
    """``relative`` 的父节点是谁。

    ``''`` 是**根目录自己**（挂在导入目标下，返回 ``None``）；根的直属
    子项（``'Stray/'``、``'a.docx'``）的父是根，返回 ``''``。
    """
    if not relative:
        return None
    trimmed = relative.rstrip('/')
    if '/' not in trimmed:
        return ''
    return trimmed.rsplit('/', 1)[0] + '/'


def node_title(value):
    """文件/目录名 -> 合法节点标题。

    和导出侧共用 ``safe_name``（同一套清洗规则），再按树的长度上限截断。
    """
    return safe_name(value)[:MAX_TITLE]


class ResourceTreeScanner:
    """把一个目录扫成 :class:`ImportPlan`。

    只读文件系统：不建节点、不写工程。``classify_file`` 默认用
    素材导入那一层留下的分类（图片 / 视频 / 3D 模型是同一个判据）。
    """

    def __init__(self, section, classify_file=classify):
        if section not in SECTION_TITLES:
            raise ResourceImportError(
                _('Unknown section: {section}').format(section=section))
        self.section = section
        self._classify = classify_file

    def scan(self, directory, root_title=None):
        """扫 ``directory``；``root_title`` 不给就用目录名。"""
        directory = os.path.abspath(str(directory))
        if not os.path.isdir(directory):
            raise ResourceImportError(
                _('The folder {path} does not exist.').format(path=directory))
        title = node_title(root_title
                           or os.path.basename(directory.rstrip('\\/'))
                           or SECTION_TITLES[self.section])
        skipped = []
        nodes = self._scan_directory(directory, '', title, skipped)
        if not nodes:
            raise ResourceImportError(_(
                'Nothing in {path} can be imported: this is the '
                '{section} section.').format(
                    path=directory, section=SECTION_TITLES[self.section]))
        notes = []
        if skipped:
            notes.append(_('Skipped {count} file(s) that do not fit this '
                           'section.').format(count=len(skipped)))
        return ImportPlan(section=self.section, root_title=title,
                          nodes=tuple(nodes), skipped=tuple(skipped),
                          notes=tuple(notes))

    # ── 扫描 ──────────────────────────────────────────────────

    def _scan_directory(self, directory, relative, title, skipped):
        """返回这个目录子树要建的节点（父在前）。"""
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            logger.warning('Could not read %s: %s', directory, exc)
            skipped.append((relative or directory, str(exc)))
            return []
        files = sorted((entry for entry in entries if entry.is_file()),
                       key=lambda entry: entry.name.casefold())
        folders = sorted((entry for entry in entries if entry.is_dir()),
                         key=lambda entry: entry.name.casefold())

        children = []
        for entry in folders:
            children.extend(self._scan_directory(
                entry.path, f'{relative}{entry.name}/',
                node_title(entry.name), skipped))

        assets, pages = [], []
        for entry in files:
            path = f'{relative}{entry.name}'
            if self.section == 'canvas':
                if self._classify(entry.path) != OTHER:
                    assets.append(entry.path)
                else:
                    skipped.append((path, _('not an image, video or model')))
            elif os.path.splitext(entry.name)[1].lower() in \
                    PAGE_EXTENSIONS[self.section]:
                pages.append(entry.path)
            else:
                skipped.append((path, _('not a {kind} file').format(
                    kind=SECTION_TITLES[self.section])))

        return self._nodes_for(relative, title, assets, pages, children)

    def _nodes_for(self, relative, title, assets, pages, children):
        parent = _parent_path(relative)
        nodes = []
        if self.section == 'canvas':
            # 有子目录：一个同名文件夹撑住层级（画布页是叶子，撑不了）。
            if children:
                nodes.append(ImportNode(relative, parent, 'folder', title,
                                        'canvas'))
            # 有媒体才有画布页 —— 中间层不建空画布。
            if assets:
                nodes.append(ImportNode(relative, parent, 'page', title,
                                        'canvas', assets=tuple(assets)))
        else:
            if pages or children:
                nodes.append(ImportNode(relative, parent, 'folder', title,
                                        self.section))
            for path in pages:
                name = node_title(os.path.splitext(os.path.basename(path))[0])
                nodes.append(ImportNode(
                    f'{relative}{os.path.basename(path)}', relative, 'page',
                    name, self.section, source_file=path))
        nodes.extend(children)
        return nodes


def scan_files(section, paths):
    """一批散装文件 → :class:`ImportPlan`：一个文件一个页面。

    用户直接选了几个 ``.docx`` / ``.xmind`` 时走这条 —— 不像目录那样套
    一层同名容器，每个文件就是一个页面。

    画布不在内：画布分区选散装素材时走的是「归到当前画布」那条老路
    （和拖单个文件进来一样）。
    """
    if section not in SECTION_TITLES:
        raise ResourceImportError(
            _('Unknown section: {section}').format(section=section))
    if section == 'canvas':
        raise ResourceImportError(_(
            'Import single files into a canvas by dropping them on it.'))
    nodes, skipped = [], []
    for path in paths:
        name = os.path.basename(str(path))
        if os.path.splitext(name)[1].lower() not in PAGE_EXTENSIONS[section]:
            skipped.append((name, _('not a {kind} file').format(
                kind=SECTION_TITLES[section])))
            continue
        nodes.append(ImportNode(
            name, None, 'page', node_title(os.path.splitext(name)[0]),
            section, source_file=path))
    if not nodes:
        raise ResourceImportError(_(
            'None of the chosen files belong to the {section} '
            'section.').format(section=SECTION_TITLES[section]))
    return ImportPlan(section=section, root_title=SECTION_TITLES[section],
                      nodes=tuple(nodes), skipped=tuple(skipped))


# ── 压缩包 ────────────────────────────────────────────────────


def _is_symlink(info):
    return stat.S_ISLNK(info.external_attr >> 16)


def extract_archive(archive_path, into):
    """把 zip 解压到 ``into``，返回真正要导入的根目录。

    导出时写了一层「根名/」，所以**只有一个顶层目录时剥掉它** —— 导出再
    导入不会越套越深。

    导入的压缩包是别人给的，这一层必须挡住：绝对路径、``..``、符号链接、
    超大解压量、条目数过多。
    """
    try:
        archive = zipfile.ZipFile(str(archive_path))
    except (OSError, zipfile.BadZipFile) as exc:
        raise ResourceImportError(_('Could not read {path}: {reason}').format(
            path=archive_path, reason=exc)) from exc

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise ResourceImportError(_(
                'The archive holds too many files ({count}).').format(
                    count=len(infos)))
        total = 0
        tops = set()
        for info in infos:
            name = info.filename.replace('\\', '/')
            parts = [part for part in name.split('/') if part not in ('', '.')]
            if not parts or name.startswith('/') or '..' in parts or \
                    (len(name) > 1 and name[1] == ':'):
                raise ResourceImportError(_(
                    'The archive contains an unsafe path: {name}').format(
                        name=info.filename))
            if _is_symlink(info):
                raise ResourceImportError(
                    _('The archive contains a link: {name}').format(
                        name=info.filename))
            total += info.file_size
            if total > MAX_ARCHIVE_BYTES:
                raise ResourceImportError(_(
                    'The archive is too large to unpack.'))
            tops.add(parts[0])
            archive.extract(info, into)

    if len(tops) == 1:
        candidate = os.path.join(into, tops.pop())
        if os.path.isdir(candidate):
            return candidate
    return into
