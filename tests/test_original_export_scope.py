from PyQt6 import QtGui
from prism.export_service import ExportService, ExportTarget
from prism.fileio.export import ImagesToDirectoryExporter
from prism.items import PrismPathItem, PrismTextItem
from tests.test_arrange_and_duplicate_sync import photo


def test_export_preflight_matches_image_exporter_not_annotations_or_video(view, tmp_path):
    image = photo(view)
    image.set_source_blob(b'original-bytes', 'png')
    for annotation in (PrismPathItem([(0, 0), (100, 100)]), PrismTextItem('Note')):
        view.scene.addItem(annotation)
    trashed = photo(view)
    trashed._deleted_at = '2026-10-14'
    class Video:
        TYPE = 'video'
    requested = list(view.scene.items_for_save())+[Video()]
    readiness = ExportService.check_original(requested)
    assert readiness.total == readiness.with_bytes == 1
    plan = ExportService(view.scene).plan(ExportTarget.ORIGINAL, items=requested)
    exporter = ImagesToDirectoryExporter(view.scene, str(tmp_path), adjusted=False)
    assert plan.items == tuple(exporter.items) == (image,)
    assert plan.lossless and not plan.warnings


def test_video_frame_original_is_full_resolution_before_saving_and_not_lod(view, tmp_path):
    image = QtGui.QImage(960, 540, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtGui.QColor(12, 45, 120, 128))
    view.add_frames_to_canvas([(image, 1234)])
    item = next(view.scene.items_for_save())
    original = item.original_bytes()
    assert original.startswith(b'\x89PNG')
    decoded = QtGui.QImage.fromData(original)
    assert decoded == image
    tiny = QtGui.QPixmap(8, 8)
    tiny.fill(QtGui.QColor('black'))
    item.set_preview_pixmap(tiny)
    assert item.original_bytes() == original
    plan = ExportService(view.scene).plan(ExportTarget.ORIGINAL)
    assert plan.lossless and not plan.warnings
    exporter = ImagesToDirectoryExporter(view.scene, str(tmp_path), adjusted=False)
    exporter.export()
    saved = list(tmp_path.iterdir())
    assert len(saved) == 1
    assert saved[0].read_bytes() == original


def test_video_frame_save_reopen_preserves_original_bytes(view, tmp_path):
    from prism.fileio.sql import SQLiteIO
    image = QtGui.QImage(320, 180, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtGui.QColor(40, 70, 90, 120))
    view.add_frames_to_canvas([(image, 1000)])
    original = next(view.scene.items_for_save()).original_bytes()
    project = str(tmp_path/'frames.prism')
    SQLiteIO(project, view.scene, create_new=True).write()
    view.clear_scene()
    SQLiteIO(project, view.scene, readonly=True).read()
    view.scene.add_queued_items()
    item = next(view.scene.items_for_save())
    assert item.original_bytes() == original
    assert QtGui.QImage.fromData(item.original_bytes()) == image
    assert ExportService(view.scene).plan(ExportTarget.ORIGINAL).lossless
