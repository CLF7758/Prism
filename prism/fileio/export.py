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

import base64
import logging
import os
import pathlib
import tempfile
from xml.etree import ElementTree as ET

from PyQt6 import QtCore, QtGui

from .errors import PrismFileIOError
from prism import constants, widgets
from prism.items import PrismPixmapItem


logger = logging.getLogger(__name__)

#: How much of one original is held in memory while streaming it out. The
#: whole point of the on-disk path: a 100k export holds one chunk of one
#: original, never the file and never the project.
STREAM_CHUNK = 1024 * 1024


class ExporterRegistry(dict):

    DEFAULT_TYPE = 0

    def __getitem__(self, key):
        key = key.removeprefix('.')
        exp = self.get(key, super().__getitem__(self.DEFAULT_TYPE))
        logger.debug(f'Exporter for type {key}: {exp}')
        return exp


exporter_registry = ExporterRegistry()


def register_exporter(cls):
    exporter_registry[cls.TYPE] = cls
    return cls


class ExporterBase:

    def emit_begin_processing(self, worker, start):
        if worker:
            worker.begin_processing.emit(start)

    def emit_progress(self, worker, progress):
        if worker:
            worker.progress.emit(progress)

    def emit_finished(self, worker, filename, errors):
        filename = str(filename)
        if worker:
            worker.finished.emit(filename, errors)

    def emit_user_input_required(self, worker, msg):
        if worker:
            worker.user_input_required.emit(msg)

    def handle_export_error(self, filename, error, worker):
        filename = str(filename)
        logger.debug(f'Export failed: {error}')
        if worker:
            worker.finished.emit(filename, [str(error)])
            return
        else:
            e = error if isinstance(error, Exception) else None
            raise PrismFileIOError(msg=str(error), filename=filename) from e


class SceneExporterBase(ExporterBase):
    """For exporting the scene to a single image."""

    def __init__(self, scene):
        self.scene = scene
        self.scene.cancel_active_modes()
        self.scene.deselect_all_items()
        # Selection outlines/handles will be rendered to the exported
        # image, so deselect first. (Alternatively, pass an attribute
        # to paint functions to not paint them?)
        self.items = [item for item in self.scene.items_for_save() if item.isVisible()]
        rect = self.scene.itemsBoundingRect(items=self.items)
        self.source_rect = rect
        logger.trace(f'Items bounding rect: {rect}')
        size = QtCore.QSize(max(1, int(rect.width())), max(1, int(rect.height())))
        logger.trace(f'Export size without margins: {size}')
        self.margin = max(size.width(), size.height()) * 0.03
        self.default_size = size.grownBy(
            QtCore.QMargins(*([int(self.margin)] * 4)))
        #: 实际导出尺寸。`get_user_input()` 之后会被用户选的尺寸覆盖；
        #: **不给默认值的话，忘了问用户就 AttributeError** —— 而
        #: `export(filename)` 的签名看不出少了什么。默认尺寸本来就是
        #: "不问用户时该用的那个"，拿它打底比抛异常合理。
        self.size = self.default_size
        logger.debug(f'Default export margin: {self.margin}')
        logger.debug(f'Default export size with margins: {self.default_size}')


@register_exporter
class SceneToPixmapExporter(SceneExporterBase):

    TYPE = ExporterRegistry.DEFAULT_TYPE

    def get_user_input(self, parent):
        """Ask user for final export size.

        **可选步骤**：不问就用 `default_size`（按内容算的那个）。
        界面上的正常流程是「先问尺寸 → 再 export」，但直接 export 也
        应该是可用的，不该炸。
        """

        dialog = widgets.SceneToPixmapExporterDialog(
            parent=parent,
            default_size=self.default_size,
        )
        if dialog.exec():
            size = dialog.value()
            logger.debug(f'Got export size {size}')
            self.size = size
            return True
        else:
            return False

    def render_to_image(self):
        logger.debug(f'Final export size: {self.size}')
        margin = self.margin * self.size.width() / self.default_size.width()
        logger.debug(f'Final export margin: {margin}')

        image = QtGui.QImage(self.size, QtGui.QImage.Format.Format_RGB32)
        image.fill(QtGui.QColor(*constants.COLORS['Scene:Canvas']))
        painter = QtGui.QPainter(image)
        target_rect = QtCore.QRectF(
            margin,
            margin,
            self.size.width() - 2 * margin,
            self.size.height() - 2 * margin)
        logger.trace(f'Final export target_rect: {target_rect}')
        self.scene.render(painter,
                          source=self.source_rect,
                          target=target_rect)
        painter.end()
        return image

    def export(self, filename, worker=None):
        logger.debug(f'Exporting scene to {filename}')
        self.emit_begin_processing(worker, 1)
        image = self.render_to_image()

        if worker and worker.canceled:
            logger.debug('Export canceled')
            self.emit_finished(worker, filename, [])
            return

        if not image.save(filename, quality=90):
            self.handle_export_error(filename, 'Error writing file', worker)
            return

        logger.debug('Export finished')
        self.emit_progress(worker, 1)
        self.emit_finished(worker, filename, [])


@register_exporter
class SceneToSVGExporter(SceneExporterBase):

    TYPE = 'svg'

    def get_user_input(self, parent):
        self.size = self.default_size
        return True

    def _get_textstyles(self, item):
        fontstylemap = {
            QtGui.QFont.Style.StyleNormal: 'normal',
            QtGui.QFont.Style.StyleItalic: 'italic',
            QtGui.QFont.Style.StyleOblique: 'oblique',
        }

        font = item.font()
        fontsize = font.pointSize() * item.scale()
        families = ', '.join(font.families())
        fontstyle = fontstylemap[font.style()]

        return ('white-space:pre',
                f'font-size:{fontsize}pt',
                f'font-family:{families}',
                f'font-weight:{font.weight()}',
                f'font-stretch:{font.stretch()}',
                f'font-style:{fontstyle}')

    def render_to_svg(self, worker=None):
        svg = ET.Element(
            'svg',
            attrib={'width': str(self.size.width()),
                    'height': str(self.size.height()),
                    'xmlns': 'http://www.w3.org/2000/svg',
                    'xmlns:xlink': 'http://www.w3.org/1999/xlink',
                    })

        rect = self.source_rect
        offset = rect.topLeft() - QtCore.QPointF(self.margin, self.margin)

        for i, item in enumerate(sorted(self.items,
                                        key=lambda x: x.zValue())):
            if item.TYPE not in ('text', 'pixmap'):
                continue
            # z order in SVG specified via the order of elements in the tree
            pos = item.pos() - offset
            anchor = pos

            if item.TYPE == 'text':
                styles = self._get_textstyles(item)
                element = ET.Element(
                    'text',
                    attrib={'style': ';'.join(styles),
                            'dominant-baseline': 'hanging'})
                element.text = item.toPlainText()
            if item.TYPE == 'pixmap':
                width = item.width * item.scale()
                height = item.height * item.scale()
                pixmap, imgformat = item.pixmap_to_bytes(
                    apply_grayscale=True,
                    apply_crop=True)
                pixmap = base64.b64encode(pixmap).decode('ascii')
                element = ET.Element(
                    'image',
                    attrib={
                        'xlink:href':
                        f'data:image/{imgformat};base64,{pixmap}',
                        'width': str(width),
                        'height': str(height),
                        'image-rendering': ('crisp-edges' if item.scale() > 2
                                            else 'optimizeQuality')})
                pos = pos + item.crop.topLeft()

            transforms = []
            if item.flip() == -1:
                # The following is not recognised by Inkscape and not an
                # official standard:
                # element.set('transform-origin', f'{anchor.x()} {anchor.y()}')
                # Thus we need to fix the origin manually
                transforms.append(f'translate({anchor.x()} {anchor.y()})')
                transforms.append(f'scale({item.flip()} 1)')
                transforms.append(f'translate(-{anchor.x()} -{anchor.y()})')
            transforms.append(
                f'rotate({item.rotation()} {anchor.x()} {anchor.y()})')

            element.set('transform', ' '.join(transforms))
            element.set('x', str(pos.x()))
            element.set('y', str(pos.y()))
            element.set('opacity', str(item.opacity()))

            svg.append(element)
            self.emit_progress(worker, i)
            if worker and worker.canceled:
                return

        return svg

    def export(self, filename, worker=None):
        logger.debug(f'Exporting scene to {filename}')
        self.emit_begin_processing(worker, len(self.scene.items()))
        svg = self.render_to_svg(worker)

        if worker and worker.canceled:
            logger.debug('Export canceled')
            worker.finished.emit(filename, [])
            return

        tree = ET.ElementTree(svg)
        ET.indent(tree, space='  ')

        try:
            with open(filename, 'w') as f:
                tree.write(f, encoding='unicode', xml_declaration=True)
        except OSError as e:
            self.handle_export_error(filename, e, worker)
            return

        logger.debug('Export finished')
        self.emit_finished(worker, filename, [])


class ImagesToDirectoryExporter(ExporterBase):
    """Export all images to a folder.

    Not registered in the registry as it is accessed via its own menu entry,
    not auto-detected by file extension.

    ``source`` is an optional
    :class:`prism.export_service.OnDiskExportSource`: the assets of a partly
    loaded project that the scene never held. They are resolved one bounded
    batch at a time and streamed out of the project's own archive, so a 100k
    export never holds more than one chunk of one original in memory.

    ``unique_names`` numbers a colliding file name (``name (2).png``) instead
    of asking about every single one — a whole-project export must not pop
    ten thousand dialogs. The interactive single-page export keeps the
    question, because there the user may really want to overwrite.
    """

    def __init__(self, scene, dirname, adjusted=False, items=None, source=None,
                 unique_names=False):
        self.scene = scene
        self.dirname = dirname
        self.adjusted = adjusted
        self.source = source
        self.unique_names = bool(unique_names)
        if items is None:
            items = [item for item in self.scene.items_by_type(PrismPixmapItem.TYPE)
                     if not getattr(item, '_deleted_at', None)]
        self.items = items
        self.max_save_id = 0
        for item in self.items:
            if item.save_id:
                self.max_save_id = max(self.max_save_id, item.save_id)
        self.num_total = len(self.items) + len(source or ())
        self.start_from = 0
        self.handle_existing = None
        #: Assets that could not be written at all (missing originals). They
        #: are reported once at the end, never silently dropped.
        self.failures = []

    def export(self, worker=None):
        logger.debug(f'Exporting images to {self.dirname}')
        logger.debug(f'Starting at {self.start_from}')

        self.emit_begin_processing(worker, self.num_total)
        self.emit_progress(worker, self.start_from)

        # 目录驱动的导出一定自动避让：它可能有十万个文件，逐个弹窗既没人
        # 点得完，也不是用户按这个菜单时的预期。单页交互导出保持原来的
        # 询问 —— 那里用户可能真的想覆盖。
        taken = self._taken_names() if (self.unique_names
                                        or self.source is not None) else None

        for i, item in enumerate(
                self.items[self.start_from:], start=self.start_from):
            if worker and worker.canceled:
                logger.debug('Export canceled')
                worker.finished.emit(self.dirname, [])
                return

            pixmap = None
            imgformat = None
            if not self.adjusted:
                # Prefer the exact imported payload.  This is important for
                # PNG alpha, HDR/EXR and any format Qt would otherwise
                # re-encode on the way out.
                try:
                    getter = getattr(item, 'original_bytes', None)
                    pixmap = getter() if callable(getter) else None
                    if not pixmap:
                        raise ValueError('原件不存在，已停止原件导出；不会用预览图替代。')
                    imgformat = item.original_format()
                except Exception as error:
                    self.handle_export_error(getattr(item, 'filename', None) or self.dirname, error, worker)
                    return
            else:
                pixmap, imgformat = item.pixmap_to_bytes()

            if item.save_id:
                filename = item.get_filename_for_export(imgformat)
            else:
                self.max_save_id += 1
                save_id = self.max_save_id
                filename = item.get_filename_for_export(imgformat, save_id)

            try:
                path = pathlib.Path(self.dirname) / filename
                path_exists = path.exists()
            except OSError as e:
                self.handle_export_error(self.dirname, e, worker)
                return

            if path_exists and taken is None:
                logger.debug(f'File already exists: {path}')
                if self.handle_existing is None:
                    self.start_from = i
                    self.emit_user_input_required(worker, str(path))
                    return
                else:
                    if self.handle_existing == 'skip':
                        self.handle_existing = None
                        logger.debug('Skipping file')
                        continue
                    elif self.handle_existing == 'skip_all':
                        logger.debug('Skipping file')
                        continue
                    elif self.handle_existing == 'overwrite':
                        self.handle_existing = None
                        logger.debug('Overwrite file')
                    elif self.handle_existing == 'overwrite_all':
                        logger.debug('Overwrite file')
            elif path_exists:
                # 自动避让：宁可写成 `name (2).png`，也不覆盖用户已有文件。
                logger.debug(f'File already exists, using a free name: {path}')
                path = self._numbered_path(path, taken)
                filename = path.name

            logger.debug(f'Writing file: {path}')
            try:
                # Stage each file so a failed write cannot leave a truncated
                # file bearing the final original's name or destroy an older export.
                descriptor, temporary = tempfile.mkstemp(prefix='.prism-export-', dir=path.parent)
                try:
                    with os.fdopen(descriptor, 'wb') as output:
                        output.write(pixmap)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temporary, path)
                finally:
                    pathlib.Path(temporary).unlink(missing_ok=True)
            except OSError as e:
                self.handle_export_error(path, e, worker)
                return

            if getattr(item, '_source_path', None) or getattr(item, '_prism_source', None):
                item._source_blob = None

            self.emit_progress(worker, i)

        if self.source is not None:
            if not self._export_source(worker, taken):
                logger.debug('Export canceled')
                worker.finished.emit(self.dirname, [])
                return

        self.emit_finished(worker, self.dirname, list(self.failures))

    # ── 还没加载的素材 ─────────────────────────────────────────

    def _export_source(self, worker, taken):
        """Write the assets that were never loaded into the scene.

        One bounded directory batch at a time, one chunk of one original at a
        time. Returns False when the worker was canceled.
        """
        source = self.source
        source.cancel = (
            (lambda: bool(worker.canceled)) if worker is not None else None)
        done = 0
        try:
            for batch in source.batches():
                for candidate in batch:
                    if worker and worker.canceled:
                        return False
                    if self._write_candidate(candidate, taken, worker) is False:
                        return False
                    done += 1
                    self.emit_progress(worker, len(self.items)+done)
        except Exception as error:                     # noqa: BLE001
            from prism.project_open_scope import OpenScopeCancelled
            if isinstance(error, OpenScopeCancelled):
                logger.debug('Export canceled while reading the directory')
                return False
            # 目录中途不可用（工程被换掉、缓存过期）必须报出来，不能当成
            # 「导出完成」—— 半份结果不能被说成全部。
            logger.exception('Could not read the project directory')
            self.failures.append(str(error))
        if source.unresolved:
            # 取项时还在、真正去取时已经不在了：也要说，不能当作导全了。
            self.failures.append(
                f'{len(source.unresolved)} asset(s) disappeared from the '
                'project during the export and were not written: '
                + ', '.join(str(key) for key in source.unresolved[:5]))
        return True

    def _write_candidate(self, candidate, taken, worker):
        """Stream one on-disk original to disk, or record why it cannot be.

        :returns: ``False`` only when the worker was canceled.
        """
        imgformat = candidate.original_format() or 'png'
        filename = candidate.get_filename_for_export(imgformat)
        try:
            path = pathlib.Path(self.dirname) / filename
            path_exists = path.exists()
        except OSError as error:
            self.failures.append(f'{filename}: {error}')
            return True
        if path_exists:
            # 十万级的导出逐个弹窗没有意义；宁可自动避让，也不覆盖。
            logger.debug(f'File already exists, using a free name: {path}')
            path = self._numbered_path(path, taken)
            filename = path.name
        written, canceled = 0, False
        try:
            with candidate.open_original_stream() as stream:
                with open(path, 'wb') as output:
                    while True:
                        if worker and worker.canceled:
                            canceled = True
                            break
                        chunk = stream.read(STREAM_CHUNK)
                        if not chunk:
                            break
                        output.write(chunk)
                        written += len(chunk)
        except Exception as error:                     # noqa: BLE001
            logger.warning('Could not export %s', candidate, exc_info=True)
            path.unlink(missing_ok=True)
            self.failures.append(f'{filename}: {error}')
            return True
        if canceled:
            # 半截文件不是导出结果：删掉再收工。
            path.unlink(missing_ok=True)
            return False
        if not written:
            # 空文件不是「导出成功」：说清楚这一条没导出去。
            path.unlink(missing_ok=True)
            self.failures.append(
                f'{filename}: {candidate} 没有任何原始字节，未导出')
            return True
        logger.debug(f'Wrote {written} original bytes to {path}')

    # ── 文件名避让 ─────────────────────────────────────────────

    def _taken_names(self):
        """Directory contents plus this run's names, case-folded.

        Seeded from disk so an existing file is numbered, never overwritten.
        """
        try:
            entries = os.listdir(self.dirname)
        except OSError:
            entries = ()
        return {entry.casefold() for entry in entries}

    def _numbered_path(self, path, taken):
        """``name (2).ext`` — reuse the resource exporter's rule verbatim."""
        from prism.fileio.resource_export import _unique_name
        if taken is None:
            taken = set()
        return path.with_name(_unique_name(taken, path.name))
