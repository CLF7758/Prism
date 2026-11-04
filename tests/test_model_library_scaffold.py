"""Scaffold acceptance: navigation is real; missing business capabilities stay off."""
import copy
import os
from pathlib import Path

import pytest
from PyQt6.QtCore import Qt

from prism.model_library.contracts import ModelFeatureUnavailable
from prism.model_library.f3d_backend import F3DBackend
from prism.model_library.service import ModelLibraryService


def _backend_without_runtime(tmp_path):
    """An F3D backend whose helper never proved itself on this machine.

    The backend now takes a ``RuntimeConfig`` and asks the controller for a
    measured handshake. The point of this file is that an *unmeasured*
    runtime keeps every entry closed, so the controller here answers "not
    available" instead of launching a helper during the scaffold check.
    """
    from prism.model_library.native_jobs import RuntimeConfig

    class _NoRuntimeController:
        def probe(self, timeout=None):
            return {'available': False, 'reason': '脚手架测试：没有原生运行时'}

    return F3DBackend(RuntimeConfig(cache_root=tmp_path),
                      controller=_NoRuntimeController())


def test_unimplemented_operations_do_not_report_success(tmp_path):
    service = ModelLibraryService()
    from prism.model_library.contracts import ModelQuery
    backend = _backend_without_runtime(tmp_path)
    operations = (
        lambda: service.query(ModelQuery()),
        lambda: service.import_paths(("example.fbx",)),
        lambda: service.export_originals(("model-id",), "unused"),
        lambda: service.open_preview("model-id"),
        lambda: service.update_appearance("model-id", None),
        lambda: backend.open(None),
    )
    for operation in operations:
        with pytest.raises(ModelFeatureUnavailable):
            operation()
    assert not service.capabilities.browse
    assert not backend.capabilities.preview


def test_sidebar_opens_model_shell_without_writing_project(main_window, qtbot):
    from prism.widgets.resource_tree_model import UI_SECTIONS
    from prism.workspace_service import TREE_SECTIONS

    scene = main_window.view.scene
    before = copy.deepcopy((scene.workspace_nodes, scene.material_records))
    tree = main_window.view.category_panel.resource_tree
    index = tree.model().index(UI_SECTIONS.index("model"), 0)
    assert index.data() == "模型库"
    assert UI_SECTIONS.index("model") == UI_SECTIONS.index("material") + 1
    assert "model" not in TREE_SECTIONS
    assert index.flags() & Qt.ItemFlag.ItemIsDropEnabled  # Model folders now accept library moves.
    main_window.show()
    tree._on_clicked(index)
    qtbot.waitUntil(
        lambda: main_window.tabs.currentWidget() is main_window.model_library,
        timeout=10000)
    library = main_window.model_library
    assert not library.details.isHidden()
    assert main_window.material_library.details.isHidden()
    assert library.cards is None  # Empty scaffold starts no extra browser surface.
    for button in (library.import_button, library.folder_button, library.export_button,
                   library.details.open_button, library.details.apply_button):
        assert not button.isEnabled()
    assert all(not slot.isEnabled() for slot in library.details.slots.values())
    assert not library.search.isEnabled()
    assert not library._summaries
    assert (scene.workspace_nodes, scene.material_records) == before
    actions = [action.text() for action in tree.menu_for_index(index).actions()]
    assert "打开模型库" in actions
    assert any("新建" in action for action in actions)  # Same folder menu as material library.
    assert all(label.text() == "—" for label in library.details.info.values())

    screenshot = os.environ.get("PRISM_MODEL_SCAFFOLD_SCREENSHOT")
    if screenshot:
        from PyQt6 import QtGui, QtWidgets
        from prism import constants
        from prism.utils import create_palette_from_dict
        app = QtWidgets.QApplication.instance()
        old_font, old_palette, old_style = app.font(), app.palette(), app.styleSheet()
        try:
            font_file = Path("C:/Windows/Fonts/msyh.ttc")
            if font_file.exists():
                QtGui.QFontDatabase.addApplicationFont(str(font_file))
            font = QtGui.QFont("Microsoft YaHei")
            font.setPixelSize(13)
            app.setFont(font)
            app.setPalette(create_palette_from_dict(constants.COLORS))
            assets = Path(__file__).resolve().parents[1] / "prism/assets"
            qss = (assets / "theme.qss").read_text(encoding="utf-8")
            qss = qss.replace("url(assets/icons/", f"url({(assets / 'icons').as_posix()}/")
            app.setStyleSheet(qss)
            main_window.resize(1440, 940)
            qtbot.wait(250)
            path = Path(screenshot)
            path.parent.mkdir(parents=True, exist_ok=True)
            assert main_window.grab().save(str(path))
        finally:
            app.setStyleSheet(old_style)
            app.setPalette(old_palette)
            app.setFont(old_font)

    main_window.open_material_library()
    qtbot.waitUntil(
        lambda: main_window.tabs.currentWidget() is main_window.material_library,
        timeout=10000)
    assert library.details.isHidden()
    assert not main_window.material_library.details.isHidden()
    assert (scene.workspace_nodes, scene.material_records) == before


def test_model_summary_uses_shared_cards_without_material_labels(main_window, qtbot):
    from prism.model_library.contracts import ModelCardSummary, ModelPage, ModelQuery

    class ReadOnlyRepository:
        def query(self, query: ModelQuery):
            return ModelPage((ModelCardSummary("contract-example", "模型摘要", "fbx"),), 1)

    library = main_window.model_library
    library.set_service(ModelLibraryService(ReadOnlyRepository()))
    # The summary query runs off the GUI thread, so the page arrives
    # asynchronously. The card surface must not be created before the page
    # is installed - that is the point of the bounded, background query.
    qtbot.waitUntil(lambda: library.cards is not None, timeout=10000)
    assert isinstance(library.cards, type(main_window.material_library.materials))
    asset = library.cards.assets[0]
    assert asset["assetKind"] == "model"
    assert asset["metaLabel"] == ""
    assert asset["dragEnabled"] is False
    assert asset["exportEnabled"] is False
    assert asset["thumbnail"].startswith("data:image/png")
    library.cards.select_record("contract-example")
    assert library.details.title.text() == "模型摘要"
    assert library.details.info["triangles"].text() == "未取得"
    assert not library.import_button.isEnabled()
    assert not library.details.open_button.isEnabled()


def test_large_preview_is_a_disabled_ui_host(qtbot):
    from prism.widgets.model_preview import ModelPreviewWindow

    dialog = ModelPreviewWindow()
    qtbot.addWidget(dialog)
    assert [dialog.mode.itemData(i) for i in range(dialog.mode.count())] == [
        'material', 'basecolor', 'normal', 'roughness', 'metallic', 'ao',
        'opacity', 'emissive', 'sss', 'displacement', 'material_edges', 'wireframe']
    assert not dialog.mode.model().item(dialog.mode.findData('wireframe')).isEnabled()
    assert not dialog.mode.isEnabled()
    assert not dialog.cover_button.isEnabled()
    assert not dialog.reset_button.isEnabled()
    assert dialog.viewport_host.currentWidget() is dialog.placeholder
