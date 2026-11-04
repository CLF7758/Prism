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

"""Export a branch of the resource tree - a section, a folder or one page.

Whatever the user right-clicks, the export always contains that node **and
everything below it**, and the shape of the tree survives the trip:

    folder         -> a directory
    document page  -> ``<title>.docx``
    mind map page  -> ``<title>.xmind``
    canvas page    -> ``<title>/`` holding that canvas' own assets

A canvas' assets (pictures, video, 3D models) are written **in the format
they came in**, byte for byte, exactly like 「导出图像 → 导出原图」 does.
An image without its original payload is never re-encoded as a substitute.
Rendering is a separate explicit export target, not an original-file fallback.

Both destinations - a directory or a ``.zip`` - are written from the same
:class:`ExportPlan`, so the archive always holds exactly what the folder
would have held; there is no second code path that could drift.

A page's text is read **when the file is written**, not when the plan is
built: a page that was never opened keeps its text in the project file,
and reading that file belongs on the thread that writes.  A page whose
text cannot be read fails - loudly, and without leaving a file behind.
An export that writes nothing but an empty file is worse than one that
says it could not read the text, so that is what this module does.

Nothing here touches widgets: the caller passes the page list, the
attachment table and a way to ask a canvas for its assets.  Writing may
run on a worker thread - the functions take the ``worker=`` keyword the
:class:`prism.fileio.ThreadedIO` convention uses.
"""
import dataclasses
import logging
import os
import re
import shutil
import tempfile
import uuid
import zipfile

from prism.fileio.resource_export_preparation import (
    PageBodyReader, attachment_reason, is_blank, missing_attachments)
from prism.i18n import _

logger = logging.getLogger(__name__)


#: Section -> the name used when the whole section is exported.
SECTION_TITLES = {'canvas': '画布', 'mindmap': '脑图', 'document': '文档'}

#: Characters Windows refuses in a file name.  ``/`` and ``\`` are already
#: rejected by the tree service; the rest arrive through imported titles.
_ILLEGAL_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: Names Windows reserves for devices, which cannot be used as file names.
_RESERVED_NAMES = frozenset(
    ['con', 'prn', 'aux', 'nul']
    + [f'com{index}' for index in range(1, 10)]
    + [f'lpt{index}' for index in range(1, 10)])

#: Fallback when a title has nothing usable left after cleaning.
_EMPTY_NAME = 'Untitled'


class ResourceExportError(Exception):
    """The export cannot start: no such node, or nothing to export."""


def _validate_relative(value, *, single=False):
    parts = str(value).split('/')
    if (not isinstance(value, str) or (single and len(parts) != 1)
            or any(not part or part in ('.', '..') or part.rstrip(' .') != part
                   or _ILLEGAL_NAME_CHARS.search(part)
                   or part.split('.')[0].casefold() in _RESERVED_NAMES for part in parts)):
        raise ResourceExportError('不安全的导出路径，未写入任何文件：' + str(value))


class ExportCancelled(str):
    """The one failure that says the user stopped the export.

    It is a string like the other failures, so it travels through the
    same channel - but the interface can tell it apart from a real
    error: the user pressed cancel, nothing is broken, and no window
    needs to pop up in their face.
    """


class _ExportInterrupted(Exception):
    """Internal control flow; public cancellation receipts remain strings."""


class _ExportFailed(Exception):
    """Stop publication after a recorded entry failure."""


@dataclasses.dataclass(frozen=True)
class ExportEntry:
    """One file of the plan; ``relative_path`` always uses ``/``.

    The payload comes from exactly one of three places, and all three end
    up on disk identically:

    * ``payload`` - bytes already in memory (an asset's original bytes)
    * ``source_path`` - a file outside the project (a referenced video or
      3D model), copied as it is
    * ``writer`` - ``callable(target_path)`` for formats that have to be
      generated while writing (``.docx``, ``.xmind``)

    ``source_resolver`` is a fourth source: the plan is built on the GUI
    thread, and an embedded video is only pulled out of the .prism archive
    while the plan is written, on the thread that writes it.
    """

    relative_path: str
    payload: bytes = None
    source_path: str = None
    writer: object = None
    #: True when the content is the file the user imported, untouched.
    lossless: bool = True
    #: ``callable() -> path``; resolved when the plan is written.
    source_resolver: object = None

    def __post_init__(self):
        _validate_relative(self.relative_path)

    def target_for(self, root):
        """The real path this entry writes to under ``root``."""
        target = os.path.join(root, *self.relative_path.split('/'))
        real_root = os.path.normcase(os.path.realpath(root))
        real_target = os.path.normcase(os.path.realpath(target))
        if os.path.commonpath((real_root, real_target)) != real_root:
            raise ResourceExportError('导出路径越过目标目录，已停止')
        return target

    def write_to(self, target):
        """Return True for a published file, False for a skipped page.

        A skipped page may leave an older export at the same path. Its
        existence does not mean this operation wrote anything.
        """
        from prism.source_store import materialization_hold
        directory = os.path.dirname(target)
        if directory:
            os.makedirs(directory, exist_ok=True)
        if self.writer is not None:
            return self._write_generated(target)
        source = self.source_path
        if self.source_resolver is not None:
            source = self.source_resolver()
        if source is not None:
            staging = _staging_path(target)
            try:
                with materialization_hold(source):
                    shutil.copy2(source, staging)
                os.replace(staging, target)
            finally:
                _remove_quietly(staging)
            return True
        staging = _staging_path(target)
        try:
            with open(staging, 'wb') as handle:
                handle.write(self.payload or b'')
            os.replace(staging, target)
        finally:
            _remove_quietly(staging)
        return True

    def _write_generated(self, target):
        """Write a generated file (``.docx``, ``.xmind``) into place.

        The writer works on a sibling file and the result is moved into
        place only when it is complete.  A writer that fails half way
        leaves the target as it was - never an empty file pretending to
        be the export - and a writer that produces nothing at all (an
        empty mind map, for instance) leaves nothing behind either.
        """
        staging = _staging_path(target)
        try:
            self.writer(staging)
            if not os.path.exists(staging):
                return False
            os.replace(staging, target)
            return True
        finally:
            _remove_quietly(staging)


@dataclasses.dataclass(frozen=True)
class ExportPlan:
    """What one export will write, computed before anything is touched.

    ``notes`` are things the user only has to be told; ``warnings`` are
    things that change what they get and deserve a moment of attention
    (the same split :class:`prism.export_service.ExportPlan` uses).
    """

    root_name: str
    entries: tuple = ()
    #: Directories that must exist even when nothing lands inside them,
    #: so an empty folder still shows up in the export.
    directories: tuple = ()
    notes: tuple = ()
    warnings: tuple = ()
    #: Assets written from their imported bytes.
    lossless_assets: int = 0
    #: Assets that had no original payload and were re-encoded.
    reencoded_assets: int = 0
    #: Asset titles that could not be exported at all.
    skipped_assets: tuple = ()
    #: The reader that knows where a page's text is.  It also counts what
    #: the write phase really produced, so the number the user is told
    #: is the number of files on disk.
    bodies: object = None

    def __post_init__(self):
        _validate_relative(self.root_name, single=True)
        for relative in self.directories:
            _validate_relative(relative)

    @property
    def file_count(self):
        """How many files this export really writes.

        A page whose text turns out to be empty produces no file, so the
        count is read after the write, not before it.
        """
        skipped = getattr(self.bodies, 'skipped_files', 0) if self.bodies \
            else 0
        return len(self.entries) - skipped

    @property
    def all_notes(self):
        """Every note: the ones the plan knew about, plus the ones the
        write phase discovered.

        A page whose text only turns out to be empty when it is read
        cannot be announced while the plan is built - that is the whole
        point of reading it late.
        """
        notes = list(self.notes)
        if self.bodies is not None:
            notes.extend(list(getattr(self.bodies, 'notes', ()) or ()))
        return notes

    def close(self):
        """Let go of whatever the plan holds open."""
        bodies = self.bodies
        closer = getattr(bodies, 'close', None) if bodies is not None else None
        if callable(closer):
            closer()


class _PlanBuilder:
    """Mutable scratch space while walking the tree."""

    def __init__(self):
        self.entries = []
        self.directories = []
        self.notes = []
        self.warnings = []
        self.lossless_assets = 0
        self.reencoded_assets = 0
        self.skipped_assets = []
        #: The reader that knows where a page's text is.
        self.bodies = None

    def add(self, entry):
        self.entries.append(entry)

    def as_plan(self, root_name):
        warnings = list(self.warnings)
        if self.reencoded_assets:
            warnings.append(_(
                '{count} asset(s) had no original bytes and were exported '
                'as the current rendering.').format(count=self.reencoded_assets))
        if self.skipped_assets:
            names = '、'.join(self.skipped_assets[:5])
            if len(self.skipped_assets) > 5:
                names += '…'
            warnings.append(_(
                '{count} asset(s) could not be exported, their original '
                'file is gone: {names}').format(
                    count=len(self.skipped_assets), names=names))
        return ExportPlan(
            root_name=root_name,
            entries=tuple(self.entries),
            directories=tuple(self.directories),
            notes=tuple(self.notes),
            warnings=tuple(warnings),
            lossless_assets=self.lossless_assets,
            reencoded_assets=self.reencoded_assets,
            skipped_assets=tuple(self.skipped_assets),
            bodies=self.bodies)


def _materialization_resolver(item):
    """Resolve an embedded video's path when the plan is written.

    The bytes come from the .prism archive, never from a file outside the
    project: an item whose archive is gone fails loudly instead of
    exporting whatever happens to lie at ``item.filename``.  The resolver
    runs on the thread that writes the plan, so the GUI thread never waits
    for the extraction.
    """
    def resolve():
        path = item._ensure_materialized()
        if not path or not os.path.isfile(path):
            raise ResourceExportError(
                _('The original video is missing; it was not exported.'))
        return path
    return resolve


class ResourceSubtreeExporter:
    """Turn one node of the resource tree into an :class:`ExportPlan`.

    ``service`` is the :class:`prism.workspace_service.ResourceTreeService`
    behind the tree, ``pages`` the project's page list (the one holding the
    document HTML and the mind map tree).  ``canvas_items`` is called with a
    canvas page id and returns that canvas' assets.

    ``bodies`` is the reader that knows where a page's text is; it defaults
    to one that only knows the pages given here.  A caller that can read a
    page's text from the project file (because it was opened page by page)
    hands that reader over instead - the export then reads the text when
    it writes, on the thread that writes, and never asks the scene for
    something the scene does not have.
    """

    def __init__(self, service, pages=None, attachments=None,
                 canvas_items=None, bodies=None, document_format='docx', asset_mode='legacy',
                 mindmap_format='xmind'):
        if document_format not in ('docx', 'pdf'):
            raise ResourceExportError('不支持的文档导出格式')
        if asset_mode not in ('legacy', 'original_images', 'original_all'):
            raise ResourceExportError('不支持的原件导出范围')
        self.document_format = document_format
        if mindmap_format not in ('xmind', 'pdf'):
            raise ResourceExportError('不支持的脑图导出格式')
        self.mindmap_format = mindmap_format
        self.asset_mode = asset_mode
        self.service = service
        self.pages = {page['id']: page for page in (pages or [])
                      if isinstance(page, dict) and page.get('id')}
        self.attachments = attachments or {}
        self.canvas_items = canvas_items or (lambda page_id: ())
        self.bodies = bodies if bodies is not None else PageBodyReader(
            self.pages.values())

    def _page_of(self, node):
        """The page of ``node`` as the reader sees it."""
        pages = getattr(self.bodies, 'pages', None)
        if pages is None:
            pages = self.pages
        return pages.get(node.get('pageId')) or {}

    # ── The plan ──────────────────────────────────────────────

    def build_plan(self, section, node_id=None):
        """Plan the export of ``node_id`` (or the whole ``section``).

        The picked node names the export root: a folder (or a section) has
        its *contents* written into that root, while a page **is** the
        export - its text, its mind map, its assets.

        :raises ResourceExportError: when there is no such node.
        """
        if section not in SECTION_TITLES:
            raise ResourceExportError(
                _('Unknown section: {section}').format(section=section))
        node = None
        if node_id is not None:
            node = self.service._by_id(node_id)
            if node is None and section == 'canvas' and node_id == 'implicit:default-canvas':
                node = dict(id=node_id, section='canvas', nodeType='page',
                            title='默认画布', pageId='default-canvas')
            if node is None or node.get('deletedAt') is not None:
                raise ResourceExportError(_('This item no longer exists.'))
            if node.get('section') != section:
                raise ResourceExportError(
                    _('This item belongs to another section.'))
        title = node['title'] if node is not None else SECTION_TITLES[section]
        builder = _PlanBuilder()
        builder.bodies = self.bodies
        if node is not None and node['nodeType'] == 'page':
            # 右键的就是一个页面：导出它自己（根就是它的名字）。
            self._collect_page(section, node, '', builder)
        else:
            self._collect(section, node_id, '', builder, set())
        return builder.as_plan(safe_name(title))

    def _collect(self, section, parent_id, prefix, builder, taken):
        """Walk one sibling group; ``taken`` keeps names unique per folder."""
        for node in self.service.listChildren(section, parent_id):
            name = _unique_name(taken, safe_name(node['title']))
            path = f'{prefix}{name}'
            if node['nodeType'] == 'folder':
                builder.directories.append(path)
                self._collect(section, node['id'], path + '/', builder, set())
            else:
                self._collect_page(section, node, path, builder)

    def _collect_page(self, section, node, path, builder):
        """``path`` 为空表示这一页就是导出根：名字改用它自己的标题。"""
        page = self._page_of(node)
        name = path or safe_name(node['title'])
        if section == 'document':
            self._add_document(node, page, name + '.' + self.document_format, builder)
        elif section == 'mindmap':
            self._add_mindmap(node, page, name + '.' + self.mindmap_format, builder)
        else:
            self._add_canvas(node, path, builder)

    # ── Pages ─────────────────────────────────────────────────

    def _add_document(self, node, page, relative_path, builder):
        """A document goes out as .docx, pictures inside it included.

        The text is read when the file is written, not when the plan is
        built: a page that was never opened keeps its text in the project
        file, and reading that file belongs on the thread that writes.

        A page whose text cannot be read produces **no file**: an empty
        ``.docx`` that says "exported" is worse than a refusal.
        """
        title = node['title']
        if 'content' in page and is_blank(page.get('content')):
            # 已经知道这一页是空的：不出文件，说明为什么。
            builder.notes.append(_empty_document_note(title))
            return
        page_id = node.get('pageId')
        bodies = self.bodies
        attachments = self.attachments
        document_format = self.document_format

        def write_document(target):
            from prism.fileio.document_export import export_note_to_word, export_note_to_pdf

            body = bodies.read(page_id)
            if not body.resolved:
                raise ResourceExportError(body.reason or _(
                    'Editor content could not be read. '
                    'Please retry exporting.'))
            if body.empty:
                # 写的时候才发现是空的：同样不出文件，同样说明。
                bodies.notes.append(_empty_document_note(title))
                return
            html = body.content or ''
            missing = missing_attachments(html, attachments)
            if missing:
                # 图片拿不到就不写：缺了一张图的 .docx 看着是成功的，
                # 打开才知道少东西 —— 那不算成功。
                raise ResourceExportError(attachment_reason(missing))
            writer = export_note_to_pdf if document_format == 'pdf' else export_note_to_word
            writer(html, attachments, target)

        builder.add(ExportEntry(relative_path, writer=write_document))

    def _add_mindmap(self, node, page, relative_path, builder):
        """A mind map goes out as .xmind, node pictures included.

        The tree is read when the file is written, for the same reason
        the document text is: a page that was never opened still has its
        tree in the project file.
        """
        title = node['title']
        tree = page.get('content')
        if 'content' in page and (not isinstance(tree, dict) or not tree):
            builder.notes.append(
                _('Skipped the empty mind map “{title}”.').format(
                    title=title))
            return
        page_id = node.get('pageId')
        bodies = self.bodies
        attachments = self.attachments

        def write_mindmap(target):
            from prism.fileio.mindmap_export import export_mindmap

            body = bodies.read(page_id)
            if not body.resolved:
                raise ResourceExportError(body.reason or _(
                    'Editor content could not be read. '
                    'Please retry exporting.'))
            tree = body.content
            if not isinstance(tree, dict) or not tree:
                bodies.notes.append(
                    _('Skipped the empty mind map “{title}”.').format(
                        title=title))
                return
            worker = getattr(bodies, '_export_worker', None)
            export_mindmap(tree, target, self.mindmap_format, attachments,
                           cancel=lambda: bool(worker and worker.canceled))

        builder.add(ExportEntry(relative_path, writer=write_mindmap))

    def _add_canvas(self, node, path, builder):
        """A canvas becomes a directory holding its own assets.

        ``path`` is empty when the canvas page *is* the export root; then
        the assets sit directly in the root and there is no extra
        directory to create (the writer makes the root itself).
        """
        if path:
            builder.directories.append(path)
        prefix = f'{path}/' if path else ''
        has_items = False
        taken = set()
        for item in self.canvas_items(node.get('pageId')):
            if item is None:
                continue
            has_items = True
            self._add_asset(item, prefix, builder, taken)
        if not has_items:
            builder.notes.append(
                _('The canvas “{title}” is empty.').format(title=node['title']))

    # ── Canvas assets ─────────────────────────────────────────

    def _add_asset(self, item, prefix, builder, taken):
        kind = getattr(item, 'TYPE', '')
        if self.asset_mode != 'legacy':
            if kind not in ('pixmap', 'video', 'glb'):
                return
            if self.asset_mode == 'original_images' and kind != 'pixmap':
                builder.notes.append('未包含非图片素材：' + _asset_title(item))
                return
            self._add_original_snapshot(item, prefix, builder, taken)
            return
        resolver = None
        try:
            if kind == 'pixmap':
                payload, extension, lossless = self._image_data(item)
                source_path = None
            elif kind in ('video', 'glb'):
                payload, extension, source_path, resolver = self._file_data(
                    item, kind)
                lossless = (payload is not None or source_path is not None
                            or resolver is not None)
            else:
                # Annotations (pen strokes, text) are not assets to take
                # away.
                return
        except Exception:                           # noqa: BLE001
            if kind == 'pixmap':
                raise ResourceExportError('原件无法取得，已停止原件导出：'+_asset_title(item))
            # One unreadable asset must not cost the user the whole export.
            logger.exception('Could not read the asset %s', _asset_title(item))
            builder.skipped_assets.append(_asset_title(item))
            return
        if payload is None and source_path is None and resolver is None:
            builder.skipped_assets.append(_asset_title(item))
            return
        if lossless:
            builder.lossless_assets += 1
        else:
            builder.reencoded_assets += 1
        name = _unique_name(taken, self._asset_name(item, extension))
        builder.add(ExportEntry(prefix + name, payload=payload,
                                source_path=source_path,
                                source_resolver=resolver, lossless=lossless))

    def _add_original_snapshot(self, item, prefix, builder, taken):
        """Freeze original-source descriptors; never read/encode an image in GUI."""
        kind = item.TYPE
        bodies = self.bodies
        blob_attr = {'pixmap': '_source_blob', 'video': '_video_blob', 'glb': '_glb_blob'}[kind]
        blob = getattr(item, blob_attr, None)
        formatter = getattr(item, 'original_format', None)
        extension = (formatter() if callable(formatter) else None) or _extension_of(
            getattr(item, 'filename', '')) or {'pixmap': 'png', 'video': 'mp4', 'glb': 'glb'}[kind]
        name = _unique_name(taken, self._asset_name(item, str(extension).lstrip('.')))
        if blob is not None:
            builder.add(ExportEntry(prefix + name, payload=bytes(blob), lossless=True))
        else:
            archive = getattr(item, '_prism_source', None)
            path = getattr(item, '_source_path', None) if kind == 'pixmap' else None
            if not archive and kind == 'video':
                url = getattr(item, '_video_url', None)
                if url is not None and hasattr(url, 'isLocalFile') and url.isLocalFile():
                    path = url.toLocalFile()
                path = path or getattr(item, '_video_source_path', None)
            if not archive and kind == 'glb':
                path = getattr(item, '_glb_path', None)
            if archive:
                from prism.export_service import CatalogExportCandidate
                candidate = CatalogExportCandidate(archive[1], archive[0], filename=name)
                def write(target):
                    with candidate.open_original_stream() as source, open(target, 'wb') as output:
                        _copy_original_stream(source, output, bodies)
            elif path:
                path = str(path)
                try:
                    stat = os.stat(path)
                except OSError as error:
                    raise ResourceExportError('原件无法读取，未使用缩略图替代：' + name) from error
                expected = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
                def write(target):
                    def check():
                        value = os.stat(path)
                        if (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns) != expected:
                            raise ResourceExportError('外部原件已变化，导出已停止：' + name)
                    check()
                    with open(path, 'rb') as source, open(target, 'wb') as output:
                        _copy_original_stream(source, output, bodies)
                    check()
            else:
                raise ResourceExportError('没有可用原件，未使用预览图或重新编码替代：' + name)
            builder.add(ExportEntry(prefix + name, writer=write, lossless=True))
        builder.lossless_assets += 1

    @staticmethod
    def _image_data(item):
        """(payload, extension, lossless) for a picture.

        The imported bytes win whenever they are still there: re-encoding a
        PNG or an EXR on the way out is exactly what 「无损导出」 rules out.
        """
        payload = None
        getter = getattr(item, 'original_bytes', None)
        if callable(getter):
            try:
                payload = getter()
            except Exception:                       # noqa: BLE001
                logger.warning('Could not read the original bytes of %s',
                               _asset_title(item), exc_info=True)
                payload = None
        if payload:
            formatter = getattr(item, 'original_format', None)
            extension = (formatter() if callable(formatter) else None) or 'png'
            return payload, str(extension).lstrip('.').lower(), True
        raise ResourceExportError('原件未能读取，未使用预览图或重新编码替代：'+_asset_title(item))

    @staticmethod
    def _file_data(item, kind):
        """(payload, extension, source_path, resolver) for a video or a 3D model.

        These are either stored inside the project or referenced on disk;
        both are exported as they are, without going through a decoder.

        An embedded video is **not** extracted while the plan is built:
        the plan is built on the GUI thread, and pulling a large original
        out of the .prism archive there would freeze the window.  The
        extraction is started in the background instead, and the thread
        that writes the plan resolves it.
        """
        blob = getattr(item, '_video_blob', None) if kind == 'video' \
            else getattr(item, '_glb_blob', None)
        extension = _extension_of(getattr(item, 'filename', '')) or (
            'mp4' if kind == 'video' else 'glb')
        if blob:
            return blob, extension, None, None
        if kind == 'video' and not getattr(item, '_is_reference', False):
            if getattr(item, '_prism_source', None):
                # 内嵌在工程里的视频：字节只能来自 .prism 存档。这里不能用
                # _video_url —— 它可能还指着导入时的那个外部文件，而那个
                # 文件早没了；导出要的是工程里的原始字节。
                request = getattr(item, 'request_materialization', None)
                if callable(request):
                    request()
                return None, extension, None, _materialization_resolver(item)
            path = getattr(item, '_video_source_path', None)
            if path and os.path.isfile(path):
                return None, _extension_of(path) or extension, path, None
        url = getattr(item, '_video_url', None) if kind == 'video' \
            else getattr(item, '_glb_path', None)
        path = None
        if kind == 'video' and url is not None and url.isLocalFile():
            path = url.toLocalFile()
        elif isinstance(url, str) and url:
            path = url
        if path and os.path.isfile(path):
            return None, _extension_of(path) or extension, path, None
        return None, extension, None, None

    def _asset_name(self, item, extension):
        stem = os.path.splitext(
            os.path.basename(getattr(item, 'filename', '') or ''))[0]
        stem = safe_name(stem) if stem else ''
        if not stem or stem == _EMPTY_NAME:
            save_id = getattr(item, 'save_id', None) or 0
            stem = f'{save_id:04d}'
        return f'{stem}.{extension}'


def _copy_original_stream(source, output, bodies):
    while chunk := source.read(1024*1024):
        _check_export_cancelled(getattr(bodies, '_export_worker', None))
        output.write(chunk)


def _empty_document_note(title):
    """Why a document page produced no file.

    The page is named so a folder with several pages says *which* one
    was empty - otherwise "there was nothing to export" is a riddle.
    """
    return '{} ({})'.format(
        _('The document note is empty, nothing to export.'), title)


def _asset_title(item):
    return (getattr(item, 'filename', '') or
            str(getattr(item, 'save_id', '') or '?'))


def _extension_of(path):
    return os.path.splitext(str(path or ''))[1].lstrip('.').lower()


def safe_name(title):
    """A title that can be a file name on every platform Prism runs on."""
    name = _ILLEGAL_NAME_CHARS.sub('_', str(title or '').strip())
    name = name.rstrip(' .')
    if not name:
        return _EMPTY_NAME
    if name.split('.')[0].casefold() in _RESERVED_NAMES:
        name = '_' + name
    return name


def _unique_name(taken, name):
    """``name``, numbered if the folder already holds one like it.

    Comparison folds case because the file systems Prism writes to treat
    ``A.docx`` and ``a.docx`` as the same file.
    """
    if name.casefold() not in taken:
        taken.add(name.casefold())
        return name
    stem, extension = os.path.splitext(name)
    index = 2
    while f'{stem} ({index}){extension}'.casefold() in taken:
        index += 1
    candidate = f'{stem} ({index}){extension}'
    taken.add(candidate.casefold())
    return candidate


# ── Writing the plan ──────────────────────────────────────────


def write_directory(plan, directory, worker=None):
    """Write ``plan`` into ``directory/<root_name>/``.

    :returns: the path of the export root.
    """
    root = os.path.join(str(directory), plan.root_name)
    try:
        errors = _write_entries(plan, root, worker)
    finally:
        plan.close()
    _finished(worker, root, errors)
    return root


def write_single_entry(plan, target, worker=None):
    """One typed resource file; cancellation never replaces an older output."""
    errors = []
    staging = _staging_path(str(target))
    _begin(worker, 1)
    try:
        if len(plan.entries) != 1:
            raise ResourceExportError('单文件导出需要一个非空资源')
        if plan.bodies is not None:
            plan.bodies._export_worker = worker
        _check_export_cancelled(worker)
        written = plan.entries[0].write_to(staging)
        _check_export_cancelled(worker)
        if written:
            os.replace(staging, str(target))
        elif plan.bodies is not None:
            plan.bodies.skipped_files += 1
        _progress(worker, 1)
    except _ExportInterrupted:
        _record_export_cancelled(plan, errors)
    except Exception as error:
        errors.append(str(error))
    finally:
        _remove_quietly(staging)
        plan.close()
    _finished(worker, str(target), errors)
    return str(target)


def write_new_directory(plan, directory, worker=None):
    """Publish a new subtree only after every entry succeeds; never merge."""
    root = os.path.join(str(directory), plan.root_name)
    errors = []
    staging = None
    try:
        if os.path.exists(root):
            raise ResourceExportError('导出目标已存在，请选择其它文件夹；不会覆盖已有内容。')
        staging = tempfile.mkdtemp(prefix='.prism-export-', dir=str(directory))
        errors = _write_entries(plan, staging, worker)
        if not errors:
            _check_export_cancelled(worker)
            os.rename(staging, root)
            staging = None
    except _ExportInterrupted:
        _record_export_cancelled(plan, errors)
    except Exception as error:
        errors.append(str(error))
    finally:
        if staging is not None:
            if (os.path.normcase(os.path.dirname(os.path.realpath(staging))) !=
                    os.path.normcase(os.path.realpath(str(directory)))
                    or not os.path.basename(staging).startswith('.prism-export-')):
                raise ResourceExportError('拒绝清理不属于本次导出的临时目录')
            shutil.rmtree(staging)
        plan.close()
    _finished(worker, root, errors)
    return root


def write_zip(plan, zip_path, worker=None):
    """Write ``plan`` as one ``.zip`` archive.

    The files are staged in a temporary directory first, so the archive is
    built from the very same writer the folder export uses - a ``.docx`` in
    the zip is the ``.docx`` the folder would have received.

    The archive itself is written beside the target and moved into place
    when it is complete: a failed export must not destroy the archive the
    user had there before.

    :returns: the path of the archive.
    """
    errors = []
    staging_path = _staging_path(str(zip_path))
    try:
        with tempfile.TemporaryDirectory(prefix='prism-export-') as staging:
            root = os.path.join(staging, plan.root_name)
            errors = _write_entries(plan, root, worker)
            try:
                if any(isinstance(error, ExportCancelled) for error in errors):
                    raise _ExportInterrupted()
                if errors:
                    raise _ExportFailed()
                _check_export_cancelled(worker)
                with zipfile.ZipFile(staging_path, 'w',
                                     zipfile.ZIP_DEFLATED) as archive:
                    # 根目录总是有一条：空导出解开之后也是一个（空的）同名
                    # 文件夹，而不是一个看不出内容的空压缩包。
                    archive.writestr(f'{plan.root_name}/', b'')
                    for relative in plan.directories:
                        _check_export_cancelled(worker)
                        archive.writestr(f'{plan.root_name}/{relative}/', b'')
                    for entry in plan.entries:
                        _check_export_cancelled(worker)
                        source = entry.target_for(root)
                        if os.path.isfile(source):
                            archive.write(
                                source, f'{plan.root_name}/{entry.relative_path}')
                # Closing the archive may take time too. Publication is the
                # boundary after which cancellation can no longer preserve
                # the previous target.
                _check_export_cancelled(worker)
            except _ExportInterrupted:
                _record_export_cancelled(plan, errors)
            except _ExportFailed:
                pass
            except OSError as exc:
                logger.exception('Could not write the archive %s', zip_path)
                errors.append(str(exc))
            else:
                os.replace(staging_path, str(zip_path))
    finally:
        _remove_quietly(staging_path)
        plan.close()
    _finished(worker, str(zip_path), errors)
    return str(zip_path)


def _write_entries(plan, root, worker):
    """Write every entry under ``root``; returns the failures.

    One unwritable file must not throw the rest away: the failures are
    collected and reported once, the way an image export reports them.
    A cancelled export is a failure of its own - it did not write
    everything it promised, and reporting that as a success would be a
    lie.  What it did write is taken back, so nothing half-finished is
    left behind pretending to be an export.
    """
    errors = []
    created_files = []
    created_directories = []
    bodies = plan.bodies
    if bodies is not None:
        bodies._export_worker = worker
        bodies.written_files = 0
        bodies.skipped_files = 0
        bodies.cancelled = False
    _begin(worker, plan.file_count)
    try:
        # 根目录本身总是出现：导出一个空画布/空文件夹时，用户看到的应该
        # 是一个空文件夹，而不是"什么都没发生"。
        if not os.path.isdir(root):
            os.makedirs(root, exist_ok=True)
            created_directories.append(root)
    except OSError as exc:
        logger.exception('Could not create %s', root)
        errors.append(f'{plan.root_name}: {exc}')
    cancelled = _cancelled(worker)
    for index, entry in enumerate(plan.entries):
        if _cancelled(worker):
            cancelled = True
            break
        target = entry.target_for(root)
        existed = os.path.exists(target)
        try:
            written = entry.write_to(target)
        except _ExportInterrupted:
            cancelled = True
            break
        except Exception as exc:                    # noqa: BLE001
            logger.exception('Could not export %s', entry.relative_path)
            errors.append(f'{entry.relative_path}: {exc}')
        else:
            if written:
                if bodies is not None:
                    bodies.written_files += 1
                if not existed:
                    created_files.append(target)
            elif bodies is not None:
                # 写手决定没有东西可写（正文是空的文档、空的脑图）：
                # 不是错误，但也不能算成一个文件。
                bodies.skipped_files += 1
        _progress(worker, index + 1)
        if _cancelled(worker):
            cancelled = True
            break
    cancelled = cancelled or _cancelled(worker)
    for relative in plan.directories:
        if cancelled or _cancelled(worker):
            cancelled = True
            break
        path = os.path.join(root, *relative.split('/'))
        try:
            if not os.path.isdir(path):
                os.makedirs(path, exist_ok=True)
                created_directories.append(path)
        except OSError as exc:
            logger.exception('Could not create %s', relative)
            errors.append(f'{relative}: {exc}')
    cancelled = cancelled or _cancelled(worker)
    if cancelled:
        if bodies is not None:
            bodies.cancelled = True
        # 用户自己按的取消键：把这次写出来的东西撤掉，别留下半棵树
        # 冒充一次完成的导出。用户原来就在那里的文件不动。
        _remove_files(created_files)
        _remove_directories(created_directories)
        errors.append(ExportCancelled(
            '导出已取消：已经写出的文件被撤掉了，其余没有导出。'))
    elif errors:
        # 出错的导出至少不留一个空文件夹在原地装作成功。
        _remove_empty_directories(created_directories)
    return errors


# ── Small helpers for writing ──────────────────────────────────


def _staging_path(target):
    """A sibling of ``target`` a writer may use before the real file exists.

    The extension is kept: a writer that guesses the format from the
    name (pandoc does) has to see the same one it will finally get.
    """
    directory = os.path.dirname(target)
    stem, extension = os.path.splitext(os.path.basename(target))
    token = uuid.uuid4().hex[:8]
    return os.path.join(directory, f'.{stem}.{token}.partial{extension}')


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _remove_files(paths):
    for path in paths:
        _remove_quietly(path)


def _remove_directories(paths):
    """Take back the directories this export created, deepest first.

    ``os.rmdir`` only removes empty ones, so a directory that still
    holds the user's own files stays where it is.
    """
    for path in sorted(paths, key=len, reverse=True):
        try:
            os.rmdir(path)
        except OSError:
            pass


def _remove_empty_directories(paths):
    """Take back the directories this export created that stayed empty."""
    for path in sorted(paths, key=len, reverse=True):
        try:
            if os.path.isdir(path) and not os.listdir(path):
                os.rmdir(path)
        except OSError:
            pass


def _cancelled(worker):
    return bool(worker is not None and getattr(worker, 'canceled', False))


def _check_export_cancelled(worker):
    if _cancelled(worker):
        raise _ExportInterrupted()


def _record_export_cancelled(plan, errors):
    if plan.bodies is not None:
        plan.bodies.cancelled = True
    if not any(isinstance(error, ExportCancelled) for error in errors):
        errors.append(ExportCancelled('导出已取消，未发布新的压缩包。'))


def _begin(worker, count):
    if worker is not None and hasattr(worker, 'begin_processing'):
        worker.begin_processing.emit(count)


def _progress(worker, value):
    if worker is not None and hasattr(worker, 'progress'):
        worker.progress.emit(value)


def _finished(worker, path, errors):
    if worker is not None and hasattr(worker, 'finished'):
        worker.finished.emit(path, list(errors))
