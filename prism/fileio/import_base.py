"""Shared pieces used by the individual file importers."""


class ImportFileError(Exception):
    """Raised when a file cannot be read as the requested format."""


__all__ = ['ImportFileError']
