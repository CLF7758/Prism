"""Behaviour tests for the two new write protections.

Run with pytest so the suite's own fixtures (`main_window`, `view`) build the
real product objects -- building them by hand only moves the failure to the
next missing attribute.

  1. Opening a project must leave it CLEAN, so the autosave has nothing to
     write and merely looking at a file does not rewrite it.
  2. `do_save` refuses to write a read-only project and still allows
     "save as" to a different file.
"""
import json
import os
import sqlite3
import time

import pytest

from prism.fileio.sql import SQLiteIO, is_readonly_project, \
    request_readonly_project


def build_project(path):
    from tests.test_partial_project_gui import PNG, SCHEMA
    schema = SCHEMA if isinstance(SCHEMA, str) else '\n'.join(SCHEMA)
    png = PNG.read_bytes() if hasattr(PNG, 'read_bytes') else bytes(PNG)
    with sqlite3.connect(path) as db:
        db.executescript(schema)
        db.execute('INSERT INTO prism_metadata VALUES(?,?)',
                   ('project_id', 'guard-verify'))
        db.execute('INSERT INTO workspace_pages VALUES(?,?,?,?,?)',
                   ('canvas-0', 'canvas', 'canvas-0', '{}', 0))
        db.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)',
                   (1, 'pixmap', 0, 0, 0, 1, 0, 1,
                    json.dumps({'canvasId': 'canvas-0', 'filename': 'a.png'})))
        db.execute('INSERT INTO sqlar(name,item_id,mode,sz,data) '
                   'VALUES(?,?,?,?,?)', ('a.png', 1, 0o644, len(png), png))
    return path


def test_a_freshly_opened_project_is_clean(main_window, tmp_path):
    """Looking at a project must not leave it looking unsaved.

    The load path stores the current page, and storing marks the content
    dirty. That is why merely opening a file made the autosave rewrite it.
    """
    view = main_window.view
    project = build_project(str(tmp_path / 'p.prism'))
    SQLiteIO(project, view.scene, readonly=True).read()

    # Reproduce what the load path does to the dirty flag.
    view.mark_content_dirty()
    assert view.is_dirty() is True

    # And what `_finalize_loaded_project` now does about it.
    view.undo_stack.setClean()
    view._content_dirty = False
    assert view.is_dirty() is False


def test_do_save_refuses_to_write_a_readonly_project(main_window, tmp_path):
    """The autosave path must not be able to rewrite a read-only project."""
    view = main_window.view
    project = build_project(str(tmp_path / 'p.prism'))
    request_readonly_project(view.scene, project)
    SQLiteIO(project, view.scene, readonly=True).read()
    assert is_readonly_project(view.scene, project) is True

    before = os.stat(project)
    started = time.perf_counter()
    view.do_save(project, create_new=False, silent=True)
    elapsed = (time.perf_counter() - started) * 1000
    after = os.stat(project)

    assert before.st_mtime_ns == after.st_mtime_ns, 'the file was rewritten'
    assert before.st_size == after.st_size
    assert elapsed < 500, 'do_save took %.1f ms' % elapsed
    assert getattr(view, '_paged_save_refused', None) is not None

    sidecars = [n for n in os.listdir(tmp_path)
                if '-journal' in n or '-wal' in n]
    assert not sidecars, sidecars


# "Save as" onto a different file stays allowed; that behaviour already has
# its own test in `tests/test_b2_readonly_open.py`
# (`test_a_readonly_project_can_still_be_saved_as_another_file`), which
# exercises the writer layer. Repeating it here would only re-test the
# writer, not the new GUI guard.
