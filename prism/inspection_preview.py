"""One selected image's reversible display state, without reading source files."""
from PyQt6 import QtGui, QtWidgets

from prism import lod
from prism.item_lifecycle import is_deleted, is_record, logical_item, native_pixmap


def render_inspection(snapshot, options, *, hdr=False, filename='', source=None,
                      image_loader=None):
    """Worker-only pixel processing. QPixmap and live items stay on the GUI."""
    from pathlib import Path
    from prism.fileio.image import load_image, adjust_display_image, false_color_preview
    from prism.image_decode import decode
    from prism.performance_trace import operation
    with operation('inspector.preview_adjust_background'):
        if hdr:
            blob, path, archive = snapshot
            if blob is None and archive:
                from prism.source_store import read_archive
                blob = read_archive(archive[:2])
            if blob is not None:
                from prism.source_store import retain_bytes
                path = retain_bytes(blob, Path(filename).suffix or '.hdr')
            image, _ = (image_loader or load_image)(path or filename, options)
        else:
            if source is None:
                decoded = decode(*snapshot)
                if decoded is None:
                    raise ValueError('无法解码这张图片')
                source, _ = decoded
            image = adjust_display_image(source, options['exposure'], options['channel'])
        if image.isNull():
            raise ValueError('无法解码这张图片')
        if options.get('false_color'):
            image = false_color_preview(image)
        return image, None if hdr else source


class InspectionPreview:
    # These are display caches only. Source bytes, metadata and geometry are
    # never replaced by an inspection session. Pixmaps share their Qt storage.
    FIELDS = ('_preview_pixmap', '_overview_pixmap', '_overview_payload',
              '_display_thumbnail', '_grayscale_cache', '_derived_pixmap',
              '_preview_adjusted', '_image_size', '_display_source_pixmap')

    def __init__(self, item):
        self.item = logical_item(item)
        self.values = {key: self.item.__dict__[key] for key in self.FIELDS
                       if key in self.item.__dict__}
        self.native = QtGui.QPixmap(native_pixmap(self.item))
        store = lod.cache()
        self.levels = {level: value for level in lod.LEVELS
                       if store is not None and (value := store.get(self.item, level)) is not None}

    def restore(self):
        item = self.item
        if is_deleted(item):
            return None
        for key in self.FIELDS:
            if key in self.values:
                item.__dict__[key] = self.values[key]
            else:
                item.__dict__.pop(key, None)
        # Keep tokens monotonic: an in-flight adjusted decode must not be
        # mistaken for pixels belonging to the restored original display.
        item._display_source_revision = getattr(item, '_display_source_revision', 0) + 1
        item._overview_revision = item._display_source_revision
        item._display_decode_request = None
        item._display_decode_failed = False
        graphics = item._graphics if is_record(item) else item
        if graphics is not None:
            QtWidgets.QGraphicsPixmapItem.setPixmap(graphics, self.native)
        store = lod.cache()
        if store is not None:
            for level, pixmap in self.levels.items():
                store.store(item, level, pixmap, notify=False)
        item.update()
        return item
