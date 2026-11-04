"""Saving one edit must not rewrite thousands of unrelated page/material rows."""
import copy
from PyQt6 import QtGui
from prism.fileio.sql import SQLiteIO
from prism.scene import PrismGraphicsScene


def test_default_note_updates_the_row_that_is_read(qapp, tmp_path):
    scene = PrismGraphicsScene(QtGui.QUndoStack())
    scene.note_html = '<p>first version</p>'
    writer = SQLiteIO(str(tmp_path/'notes.prism'), scene, create_new=True)
    writer.create_schema_on_new()
    writer.write_board_extras()
    scene.note_html = '<p>second version</p>'
    writer.write_board_extras()
    assert writer.fetchone('SELECT html FROM board_notes WHERE board_id=0')[0] == scene.note_html
    assert writer.fetchone('SELECT count(*) FROM board_notes WHERE board_id=0')[0] == 1
    writer._close_connection()


def test_one_edit_only_writes_one_row_in_large_page_library(qapp, tmp_path):
    scene = PrismGraphicsScene(QtGui.QUndoStack())
    scene.workspace_pages = [dict(id=f'page-{i}', kind='document' if i<500 else 'mindmap',
                                 title=f'Page {i}', content='<p>Original note</p>' if i<500 else {'data':{'text':'Root'}},
                                 tags=[], parent=None, permanent=False) for i in range(1000)]
    template = dict(projectId=scene.project_id, schemaVersion=1, name='Material', notes='', folderId=None,
                    tags=[], textures={}, preview={}, nativePackages=[], status='draft',
                    createdAt='2026-10-20', updatedAt='2026-10-20', deletedAt=None)
    scene.material_records = [dict(copy.deepcopy(template), id=f'mat-{i}') for i in range(5000)]
    writer = SQLiteIO(str(tmp_path/'large.prism'), scene, create_new=True)
    writer.create_schema_on_new()
    writer.write_board_extras()
    before = writer.connection.total_changes
    writer.write_board_extras()
    assert writer.connection.total_changes-before == 0
    scene.workspace_pages[123]['content'] = '<p>Only changed note</p>'
    before = writer.connection.total_changes
    writer.write_board_extras()
    assert writer.connection.total_changes-before == 1
    scene.material_records[4000]['notes'] = 'Only changed material'
    before = writer.connection.total_changes
    writer.write_board_extras()
    assert writer.connection.total_changes-before == 1
    scene.workspace_pages.pop()
    before = writer.connection.total_changes
    writer.write_board_extras()
    assert writer.connection.total_changes-before == 1
    assert writer.fetchone('SELECT count(*) FROM workspace_pages')[0] == 999
    assert writer.fetchone('SELECT count(*) FROM material_records')[0] == 5000
    writer._close_connection()
