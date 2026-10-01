"""Compatibility layer re-exporting the individual file importers.

The readers used to live in this single file.  They are split up so the
Word and XMind sides can be worked on independently:

* ``import_docx``  - Word (.docx) into HTML for the note editor
* ``import_xmind`` - XMind (.xmind) into a simple-mind-map tree
* ``import_base``  - the shared ``ImportFileError``

This module stays in place as the import path the rest of the app already
uses, so nothing else had to change when the split happened.
"""
from .import_base import ImportFileError
from .import_docx import ImportReport, SKIPPED_FEATURES, docx_to_html
from .import_xmind import XMIND_LAYOUTS, xmind_layout_name, xmind_to_tree

__all__ = [
    'ImportFileError',
    'ImportReport',
    'SKIPPED_FEATURES',
    'XMIND_LAYOUTS',
    'docx_to_html',
    'xmind_layout_name',
    'xmind_to_tree',
]
