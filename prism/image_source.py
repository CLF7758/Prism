"""Original image bytes and formats; no graphics object is required."""
import logging
import os

logger = logging.getLogger(__name__)

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
                logger.debug('Could not read deferred source %s',
                             self._source_path, exc_info=True)
        if self._prism_source:
            prism_path, item_id = self._prism_source
            try:
                from prism.source_store import read_archive
                blob = read_archive((prism_path, item_id))
                if blob:
                    self._source_blob = blob
                    if self._source_format is None:
                        self._source_format = (
                            self._format_from_bytes(self._source_blob)
                            or 'png')
                    return self._source_blob
            except Exception:
                logger.debug('Could not read deferred source from %s',
                             prism_path, exc_info=True)
        if self._prism_source and self.filename and os.path.isfile(self.filename):
            try:
                with open(self.filename, 'rb') as source:
                    self._source_blob = source.read()
                return self._source_blob or None
            except OSError:
                logger.debug('Could not recover original %s', self.filename, exc_info=True)
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
