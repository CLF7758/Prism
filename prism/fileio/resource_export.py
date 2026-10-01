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
An asset whose original payload is gone (a picture drawn on the canvas, or
one from an old project) is exported as the current rendering instead, and
that is reported rather than passed off as lossless.

Both destinations - a directory or a ``.zip`` - are written from the same
:class:`ExportPlan`, so the archive always holds exactly what the folder
would have held; there is no second code path that could drift.

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
import zipfile

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
    """

    relative_path: str
    payload: bytes = None
    source_path: str = None
    writer: object = None
    #: True when the content is the file the user imported, untouched.
    lossless: bool = True

    def target_for(self, root):
        """The real path this entry writes to under ``root``."""
        return os.path.join(root, *self.relative_path.split('/'))

    def write_to(self, target):
        directory = os.path.dirname(target)
        if directory:
            os.makedirs(directory, exist_ok=True)
        if self.writer is not None:
            self.writer(target)
        elif self.source_path is not None:
            shutil.copy2(self.source_path, target)
        else:
            with open(target, 'wb') as handle:
                handle.write(self.payload or b'')


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

    @property
    def file_count(self):
        return len(self.entries)


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
            skipped_assets=tuple(self.skipped_assets))


class ResourceSubtreeExporter:
    """Turn one node of the resource tree into an :class:`ExportPlan`.

    ``service`` is the :class:`prism.workspace_service.ResourceTreeService`
    behind the tree, ``pages`` the project's page list (the one holding the
    document HTML and the mind map tree).  ``canvas_items`` is called with a
    canvas page id and returns that canvas' assets.
    """

    def __init__(self, service, pages=None, attachments=None,
                 canvas_items=None):
        self.service = service
        self.pages = {page['id']: page for page in (pages or [])
                      if isinstance(page, dict) and page.get('id')}
        self.attachments = attachments or {}
        self.canvas_items = canvas_items or (lambda page_id: ())

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
            if node is None or node.get('deletedAt') is not None:
                raise ResourceExportError(_('This item no longer exists.'))
            if node.get('section') != section:
                raise ResourceExportError(
                    _('This item belongs to another section.'))
        title = node['title'] if node is not None else SECTION_TITLES[section]
        builder = _PlanBuilder()
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
        page = self.pages.get(node.get('pageId')) or {}
        name = path or safe_name(node['title'])
        if section == 'document':
            self._add_document(node, page, name + '.docx', builder)
        elif section == 'mindmap':
            self._add_mindmap(node, page, name + '.xmind', builder)
        else:
            self._add_canvas(node, path, builder)

    # ── Pages ─────────────────────────────────────────────────

    def _add_document(self, node, page, relative_path, builder):
        """A document goes out as .docx, pictures inside it included."""
        html = page.get('content') or ''
        attachments = self.attachments

        def write_document(target):
            from prism.fileio.document_export import export_note_to_word

            export_note_to_word(html, attachments, target)

        builder.add(ExportEntry(relative_path, writer=write_document))

    def _add_mindmap(self, node, page, relative_path, builder):
        """A mind map goes out as .xmind, node pictures included."""
        tree = page.get('content')
        if not isinstance(tree, dict) or not tree:
            builder.notes.append(
                _('Skipped the empty mind map “{title}”.').format(
                    title=node['title']))
            return
        attachments = self.attachments

        def write_mindmap(target):
            from prism.fileio.mindmap_export import export_mindmap

            export_mindmap(tree, target, 'xmind', attachments)

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
        items = [item for item in self.canvas_items(node.get('pageId'))
                 if item is not None]
        taken = set()
        for item in items:
            self._add_asset(item, prefix, builder, taken)
        if not items:
            builder.notes.append(
                _('The canvas “{title}” is empty.').format(title=node['title']))

    # ── Canvas assets ─────────────────────────────────────────

    def _add_asset(self, item, prefix, builder, taken):
        kind = getattr(item, 'TYPE', '')
        try:
            if kind == 'pixmap':
                payload, extension, lossless = self._image_data(item)
                source_path = None
            elif kind in ('video', 'glb'):
                payload, extension, source_path = self._file_data(item, kind)
                lossless = payload is not None or source_path is not None
            else:
                # Annotations (pen strokes, text) are not assets to take
                # away.
                return
        except Exception:                           # noqa: BLE001
            # One unreadable asset must not cost the user the whole export.
            logger.exception('Could not read the asset %s', _asset_title(item))
            builder.skipped_assets.append(_asset_title(item))
            return
        if payload is None and source_path is None:
            builder.skipped_assets.append(_asset_title(item))
            return
        if lossless:
            builder.lossless_assets += 1
        else:
            builder.reencoded_assets += 1
        name = _unique_name(taken, self._asset_name(item, extension))
        builder.add(ExportEntry(prefix + name, payload=payload,
                                source_path=source_path, lossless=lossless))

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
        payload, extension = item.pixmap_to_bytes()
        return payload, str(extension).lstrip('.').lower(), False

    @staticmethod
    def _file_data(item, kind):
        """(payload, extension, source_path) for a video or a 3D model.

        These are either stored inside the project or referenced on disk;
        both are exported as they are, without going through a decoder.
        """
        blob = getattr(item, '_video_blob', None) if kind == 'video' \
            else getattr(item, '_glb_blob', None)
        path = None
        url = getattr(item, '_video_url', None) if kind == 'video' \
            else getattr(item, '_glb_path', None)
        if kind == 'video' and url is not None and url.isLocalFile():
            path = url.toLocalFile()
        elif isinstance(url, str) and url:
            path = url
        extension = _extension_of(getattr(item, 'filename', '')) or (
            'mp4' if kind == 'video' else 'glb')
        if blob:
            return blob, extension, None
        if path and os.path.isfile(path):
            return None, _extension_of(path) or extension, path
        return None, extension, None

    def _asset_name(self, item, extension):
        stem = os.path.splitext(
            os.path.basename(getattr(item, 'filename', '') or ''))[0]
        stem = safe_name(stem) if stem else ''
        if not stem or stem == _EMPTY_NAME:
            save_id = getattr(item, 'save_id', None) or 0
            stem = f'{save_id:04d}'
        return f'{stem}.{extension}'


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
    errors = _write_entries(plan, root, worker)
    _finished(worker, root, errors)
    return root


def write_zip(plan, zip_path, worker=None):
    """Write ``plan`` as one ``.zip`` archive.

    The files are staged in a temporary directory first, so the archive is
    built from the very same writer the folder export uses - a ``.docx`` in
    the zip is the ``.docx`` the folder would have received.

    :returns: the path of the archive.
    """
    errors = []
    with tempfile.TemporaryDirectory(prefix='prism-export-') as staging:
        root = os.path.join(staging, plan.root_name)
        errors = _write_entries(plan, root, worker)
        try:
            with zipfile.ZipFile(str(zip_path), 'w',
                                 zipfile.ZIP_DEFLATED) as archive:
                # 根目录总是有一条：空导出解开之后也是一个（空的）同名
                # 文件夹，而不是一个看不出内容的空压缩包。
                archive.writestr(f'{plan.root_name}/', b'')
                for relative in plan.directories:
                    archive.writestr(f'{plan.root_name}/{relative}/', b'')
                for entry in plan.entries:
                    source = entry.target_for(root)
                    if os.path.isfile(source):
                        archive.write(
                            source, f'{plan.root_name}/{entry.relative_path}')
        except OSError as exc:
            logger.exception('Could not write the archive %s', zip_path)
            errors.append(str(exc))
    _finished(worker, str(zip_path), errors)
    return str(zip_path)


def _write_entries(plan, root, worker):
    """Write every entry under ``root``; returns the failures.

    One unwritable file must not throw the rest away: the failures are
    collected and reported once, the way an image export reports them.
    """
    errors = []
    _begin(worker, plan.file_count)
    try:
        # 根目录本身总是出现：导出一个空画布/空文件夹时，用户看到的应该
        # 是一个空文件夹，而不是"什么都没发生"。
        os.makedirs(root, exist_ok=True)
    except OSError as exc:
        logger.exception('Could not create %s', root)
        errors.append(f'{plan.root_name}: {exc}')
    for index, entry in enumerate(plan.entries):
        if _cancelled(worker):
            break
        try:
            entry.write_to(entry.target_for(root))
        except Exception as exc:                    # noqa: BLE001
            logger.exception('Could not export %s', entry.relative_path)
            errors.append(f'{entry.relative_path}: {exc}')
        _progress(worker, index + 1)
    for relative in plan.directories:
        try:
            os.makedirs(os.path.join(root, *relative.split('/')), exist_ok=True)
        except OSError as exc:
            logger.exception('Could not create %s', relative)
            errors.append(f'{relative}: {exc}')
    return errors


def _cancelled(worker):
    return bool(worker is not None and getattr(worker, 'canceled', False))


def _begin(worker, count):
    if worker is not None and hasattr(worker, 'begin_processing'):
        worker.begin_processing.emit(count)


def _progress(worker, value):
    if worker is not None and hasattr(worker, 'progress'):
        worker.progress.emit(value)


def _finished(worker, path, errors):
    if worker is not None and hasattr(worker, 'finished'):
        worker.finished.emit(path, list(errors))
