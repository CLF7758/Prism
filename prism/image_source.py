"""Original image bytes and formats; no graphics object is required."""
import logging
import os

logger = logging.getLogger(__name__)


def generated_png_bytes(item):
    """Create a lossless source only for genuinely source-less generated pixels.

    A named imported image with lost bytes is not a generated image. Never
    legitimize its preview as a replacement original during save.
    """
    if (getattr(item, '_source_path', None) or getattr(item, '_prism_source', None)
            or getattr(item, 'filename', None)):
        raise ValueError('导入图片的原件未留存，已停止保存，不会把预览图当作原件。')
    from PyQt6 import QtCore
    image = item.pixmap().toImage()
    if image.isNull():
        raise ValueError('没有可保存的原始像素。')
    data = QtCore.QByteArray()
    buffer = QtCore.QBuffer(data)
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, 'PNG'):
        raise ValueError('无法无损保存生成的图片。')
    return bytes(data), 'png'

class ImageSourceMixin:
    def get_filename_for_export(self, imgformat, save_id_default=None):
        save_id = self.save_id or save_id_default
        assert save_id is not None

        if self.filename:
            basename = os.path.splitext(os.path.basename(self.filename))[0]
            return f'{save_id:04}-{basename}.{imgformat}'
        else:
            return f'{save_id:04}.{imgformat}'

    @staticmethod
    def _normalise_format(value):
        if not value:
            return None
        value = str(value).lower().strip()
        return value[1:] if value.startswith('.') else value

    @staticmethod
    def _format_from_filename(filename):
        if not filename:
            return None
        return ImageSourceMixin._normalise_format(
            os.path.splitext(str(filename))[1])

    def set_source_blob(self, blob, source_format=None):
        """Keep the bytes that were imported as the lossless source.

        The canvas may use a decoded or adjusted QPixmap, but this payload
        is never rewritten merely because the project is saved or the
        preview controls change.  It is also intentionally independent of
        the current storage-format preference.
        """
        self._display_source_revision = getattr(self, '_display_source_revision', 0)+1
        self._display_thumbnail = None
        if hasattr(self, '_overview_payload'):
            self._overview_payload = None
        self._grayscale_cache = None
        self._display_decode_request = None
        self._display_decode_failed = False
        if blob is None:
            self._source_blob = None
            self._source_format = None
            self._source_path = None
            self._prism_source = None
            self._preview_adjusted = False
            return
        self._source_blob = bytes(blob)
        self._source_format = (
            self._normalise_format(source_format)
            or self._format_from_filename(self.filename)
            or self._format_from_bytes(self._source_blob)
            or 'png')
        self._preview_adjusted = False

    @staticmethod
    def _format_from_bytes(blob):
        signatures = ((b'\x89PNG\r\n\x1a\n', 'png'),
                      (b'\xff\xd8\xff', 'jpg'),
                      (b'GIF87a', 'gif'),
                      (b'GIF89a', 'gif'),
                      (b'RIFF', 'webp'),
                      (b'BM', 'bmp'))
        for signature, format_name in signatures:
            if blob.startswith(signature):
                return format_name
        return None

    def original_bytes(self):
        """Return the imported bytes, or ``None`` for generated images.

        When the source blob was deferred (only the file path or the
        .prism archive reference was kept during import/load), read the
        original bytes from disk on demand.  This keeps import memory
        usage low while still preserving the exact original file bytes
        when saving.
        """
        if self._source_blob is not None:
            return self._source_blob
        if self._prism_source:
            # The embedded reference is authoritative. Do not mask a busy,
            # corrupt or missing object by guessing from a filename on disk.
            from prism.source_store import read_archive
            from prism.storage.contracts import StorageError, StorageErrorKind
            blob = read_archive(self._prism_source)
            if not blob:
                raise StorageError(StorageErrorKind.MISSING,
                                   'Embedded original image is missing; no replacement was used')
            self._source_blob = blob
            if self._source_format is None:
                self._source_format = self._format_from_bytes(blob) or 'png'
            return blob
        if self._source_path and os.path.isfile(self._source_path):
            try:
                with open(self._source_path, 'rb') as f:
                    self._source_blob = f.read()
                if self._source_format is None:
                    self._source_format = (
                        self._format_from_filename(self._source_path)
                        or self._format_from_bytes(self._source_blob)
                        or 'png')
                return self._source_blob
            except (OSError, TypeError):
                logger.warning('Could not read retained original source %s', self._source_path, exc_info=True)
                raise
        if self._source_path:
            raise FileNotFoundError('Retained original image is missing: '+str(self._source_path))
        return None

    def original_format(self):
        return self._source_format or self._format_from_filename(self.filename)

    def has_original_source(self):
        return bool(self._source_blob) or (
            self._source_path and os.path.isfile(self._source_path)) or (
            self._prism_source is not None)

    def materialize_source(self):
        """Force deferred source bytes into memory.

        Call before the source file might disappear (e.g. staging
        directory cleanup in zip imports).  After this call the blob
        lives in ``_source_blob`` regardless of whether the file still
        exists.
        """
        if self._source_blob is not None:
            return
        self.original_bytes()
