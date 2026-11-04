"""Temporary measurement: GUI-thread cost of building a page of cards."""
import sqlite3
import threading
import time

from unittest.mock import patch

from PyQt6 import QtGui

from prism.widgets.material_cards import material_thumbnail


def test_measure(main_window, qapp, qtbot, tmp_path):
    from prism.material_textures import render_signature

    main_window.show()
    main_window.open_material_library()
    page = main_window.material_library

    project = (r'C:\Users\admin\Desktop\Prism-main\Prism-main\artifacts'
               r'\material-tests\stress-300\PolyHaven-300.prism')
    connection = sqlite3.connect('file:' + project.replace('\\', '/') + '?mode=ro', uri=True)
    blobs = [row[0] for row in connection.execute(
        'SELECT blob FROM attachments WHERE length(blob)>50000 LIMIT 200')]
    connection.close()
    print('MEASURE real_blobs=%d avg_kb=%.0f' % (
        len(blobs), sum(len(b) for b in blobs) / max(len(blobs), 1) / 1024))

    records = []
    for index, blob in enumerate(blobs):
        material = page.records.create(name='m%03d' % index)
        page.records.save_preview(material['id'], render_signature(material), blob)
        records.append(page.records.get(material['id']))

    attachments = page.window_host.view.scene.attachments
    for record, blob in zip(records, blobs):
        key = record['preview']['thumbnailAttachmentId']
        attachments[key] = ('material-preview.png', 'image/png', blob)

    real_from_data = QtGui.QImage.fromData
    gui = threading.get_ident()
    on_gui = []

    def from_data(*args, **kwargs):
        if threading.get_ident() == gui:
            on_gui.append(1)
        return real_from_data(*args, **kwargs)

    cards = page.records.list_card_records(page.folder_id, '')

    # The old behaviour: the card surface loaded the picture itself.
    def old_card_thumbnail(record):
        return page._thumbnail_cache.get(record, attachments, material_thumbnail)

    page._thumbnail_cache.cache.clear()
    with patch('prism.widgets.material_cards.QtGui.QImage.fromData',
               side_effect=from_data):
        started = time.perf_counter()
        page.materials.set_records(cards, page.selected_id, old_card_thumbnail)
        old_ms = (time.perf_counter() - started) * 1000
        old_decodes = len(on_gui)

    page._thumbnail_cache.cache.clear()
    on_gui.clear()
    with patch('prism.widgets.material_cards.QtGui.QImage.fromData',
               side_effect=from_data):
        started = time.perf_counter()
        page.materials.set_records(cards, page.selected_id, page._card_thumbnail)
        new_ms = (time.perf_counter() - started) * 1000
        new_decodes = len(on_gui)

    print('MEASURE cards=%d' % len(cards))
    print('MEASURE old_ms=%.0f old_gui_decodes=%d' % (old_ms, old_decodes))
    print('MEASURE new_ms=%.0f new_gui_decodes=%d' % (new_ms, new_decodes))
