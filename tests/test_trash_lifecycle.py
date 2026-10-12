"""阶段 C：素材回收站（软删除）生命周期。

覆盖任务书 §7 的验收点：删除进回收站、计数联动、撤销/重做、保存重开仍在
回收站、恢复保属性、永久删除清字节、标注走硬删除、混合选择一步撤销、
删除态对搜索/导出/查重不可见。
"""
import json

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtGui import QUndoStack

from prism import commands
from prism.items import PrismPixmapItem, PrismTextItem
from prism.project_catalog import ProjectAssetCatalog

_KEEP_ALIVE = []


def make_scene():
    from prism.scene import PrismGraphicsScene
    stack = QUndoStack()
    scene = PrismGraphicsScene(stack)
    _KEEP_ALIVE.append((scene, stack))
    return scene


def photo(view, name='a.png', canvas=None, tags=(), rating=0):
    image = QtGui.QImage(8, 8, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('#336699'))
    item = PrismPixmapItem(image, filename=name)
    item.init_metadata()
    if canvas:
        item._canvas_id = canvas
    item._tags = list(tags)
    item._rating = rating
    view.scene.addItem(item)
    return item


def catalog(view):
    return ProjectAssetCatalog(view.scene, view.category_panel.resource_service)


# ── 删除 → 进回收站 ────────────────────────────────────────────

def test_delete_asset_goes_to_trash_and_stays_in_scene(view):
    item = photo(view)
    item.setSelected(True)
    cat = catalog(view)
    assert cat.total_count() == 1
    assert cat.trash_asset_count() == 0

    view.on_action_delete_items()

    # 数据没丢：图元还在场景里，只是隐藏 + 不可选中 + 打了时间戳。
    assert item in view.scene.items()
    assert item.isVisible() is False
    assert item._deleted_at is not None
    assert bool(item.flags() & QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsSelectable) is False
    assert cat.total_count() == 0
    assert cat.trash_asset_count() == 1


def test_nav_counts_reflect_trash_across_project(view):
    photo(view, 'one.png', canvas='c1')
    photo(view, 'two.png', canvas='c2')
    panel = view.category_panel
    panel.update_counts()
    assert panel._nav_counts['all'].text() == '2'
    assert panel._nav_counts['trash'].text() == '0'

    item = photo(view, 'three.png', canvas='c1')
    item.setSelected(True)
    view.on_action_delete_items()
    panel.update_counts()
    assert panel._nav_counts['all'].text() == '2'
    assert panel._nav_counts['trash'].text() == '1'


# ── 撤销 / 重做 ────────────────────────────────────────────────

def test_undo_restores_trashed_asset(view):
    item = photo(view)
    item.setSelected(True)
    view.on_action_delete_items()
    assert item._deleted_at is not None

    view.undo_stack.undo()

    assert item._deleted_at is None
    assert item.isVisible() is True
    assert bool(item.flags() & QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsSelectable) is True
    assert catalog(view).total_count() == 1
    assert catalog(view).trash_asset_count() == 0


def test_redo_trashes_again(view):
    item = photo(view)
    item.setSelected(True)
    view.on_action_delete_items()
    view.undo_stack.undo()
    view.undo_stack.redo()
    assert item._deleted_at is not None
    assert item.isVisible() is False
    assert catalog(view).trash_asset_count() == 1


# ── 恢复保属性 ─────────────────────────────────────────────────

def test_restore_preserves_asset_properties(view):
    item = photo(view, 'keep.png', tags=['ref'], rating=4)
    item.setPos(120, 80)
    item.setScale(1.7)
    item.setRotation(15)
    before = (item.pos().x(), item.pos().y(), item.scale(), item.rotation(),
              item.filename, list(item._tags), item._rating, item.flip())

    item.setSelected(True)
    view.on_action_delete_items()
    view.undo_stack.push(commands.RestoreItems(view.scene, [item]))

    after = (item.pos().x(), item.pos().y(), item.scale(), item.rotation(),
             item.filename, list(item._tags), item._rating, item.flip())
    assert after == before
    assert item._deleted_at is None
    assert item.isVisible() is True


# ── 保存 / 重开 持久化 ─────────────────────────────────────────

def test_trashed_state_persists_through_save_and_reopen(tmpfile, view, imgdata3x3):
    from prism.fileio.sql import SQLiteIO

    item = photo(view, 'gone.png')
    item.setSelected(True)
    view.on_action_delete_items()

    io = SQLiteIO(tmpfile, view.scene, create_new=True)
    io.write()
    # deletedAt 落在 items.data 的 JSON 里 —— 没有动表结构。
    row = io.fetchone('SELECT data FROM items WHERE id=?', (item.save_id,))
    assert json.loads(row[0]).get('deletedAt') is not None
    # 原始字节还在 sqlar 里（软删除不删数据）。
    assert io.fetchone('SELECT data FROM sqlar WHERE item_id=?',
                       (item.save_id,))[0] is not None
    del io

    # 重开进一个全新场景。
    scene2 = make_scene()
    io2 = SQLiteIO(tmpfile, scene2, readonly=True)
    io2.read()
    scene2.add_queued_items()
    reloaded = [i for i in scene2.items() if isinstance(i, PrismPixmapItem)]
    assert len(reloaded) == 1
    reopened = reloaded[0]
    assert reopened._deleted_at is not None
    assert reopened.isVisible() is False
    assert catalog_for(scene2).total_count() == 0
    assert catalog_for(scene2).trash_asset_count() == 1


def catalog_for(scene):
    return ProjectAssetCatalog(scene, None)


def test_purge_removes_item_and_drops_bytes_on_save(tmpfile, view, imgdata3x3):
    from prism.fileio.sql import SQLiteIO

    item = photo(view, 'purge.png')
    item.setSelected(True)
    view.on_action_delete_items()

    io = SQLiteIO(tmpfile, view.scene, create_new=True)
    io.write()
    # save_id 是写库时才分配的，所以要 write() 之后再取。
    save_id = item.save_id
    assert save_id is not None
    assert io.fetchone('SELECT COUNT(*) FROM items WHERE id=?', (save_id,))[0] == 1
    del io

    # 永久删除 = 移出场景（并丢归档引用），保存后数据库记录和字节都没了。
    view.scene._prism_archive_items.discard(item)
    view.scene.removeItem(item)
    view.undo_stack.clear()

    io = SQLiteIO(tmpfile, view.scene)
    io.write()
    assert io.fetchone('SELECT COUNT(*) FROM items WHERE id=?', (save_id,))[0] == 0
    assert io.fetchone('SELECT COUNT(*) FROM sqlar WHERE item_id=?', (save_id,))[0] == 0
    del io


# ── 标注走硬删除 / 混合选择 ────────────────────────────────────

def test_annotation_is_hard_deleted_not_trashed(view):
    note = PrismTextItem('hello')
    view.scene.addItem(note)
    note.setSelected(True)
    view.on_action_delete_items()
    # 文字注释没有原始字节要保，直接删，不进回收站。
    assert note not in view.scene.items()
    assert catalog(view).trash_asset_count() == 0


def test_mixed_selection_trashes_asset_and_deletes_annotation_in_one_undo(view):
    asset = photo(view, 'mix.png')
    note = PrismTextItem('caption')
    view.scene.addItem(note)
    asset.setSelected(True)
    note.setSelected(True)

    view.on_action_delete_items()
    assert asset._deleted_at is not None       # 素材软删除
    assert note not in view.scene.items()      # 标注硬删除

    view.undo_stack.undo()                     # 一次撤销两类全回来
    assert asset._deleted_at is None
    assert asset in view.scene.items()
    assert note in view.scene.items()


# ── 删除态对搜索 / 导出 / 查重不可见 ───────────────────────────

def test_trashed_asset_hidden_from_search_export_and_duplicate(view):
    from prism import project_search, duplicate_scan

    keep = photo(view, 'keep.png', tags=['x'])
    gone = photo(view, 'gone.png', tags=['x'])
    gone.setSelected(True)
    view.on_action_delete_items()

    # 搜索
    service = view.category_panel.resource_service
    results = project_search.search_project(view.scene, service, 'gone')
    assert all(r.get('item') is not gone for r in results)

    # 导出服务
    export_items = view.export_service.items_for(selected_only=False)
    assert gone not in export_items
    assert keep in export_items

    # 查重扫描
    sources = duplicate_scan.collect_sources(
        view.scene, duplicate_scan.ScanScope.ALL_ASSETS)
    scanned = [source.item for source in sources]
    assert gone not in scanned
    assert keep in scanned


# ── 回收站面板：素材页签 ───────────────────────────────────────

def test_trash_panel_lists_and_restores_asset(view, qtbot):
    from prism.widgets.trash_panel import TrashPanel

    item = photo(view, 'panel.png')
    item.setSelected(True)
    view.on_action_delete_items()

    panel = TrashPanel(view)
    qtbot.addWidget(panel)
    assert panel.asset_list.count() == 1

    panel.asset_list.item(0).setSelected(True)
    panel.restore_selected_assets()
    assert item._deleted_at is None
    assert item.isVisible() is True
    assert panel.asset_list.count() == 0


def test_trash_panel_purge_emits_assets_purged(view, qtbot):
    from prism.widgets.trash_panel import TrashPanel

    item = photo(view, 'purge-panel.png')
    item.setSelected(True)
    view.on_action_delete_items()

    panel = TrashPanel(view)
    qtbot.addWidget(panel)
    panel.asset_list.item(0).setSelected(True)
    received = []
    panel.assets_purged.connect(lambda items: received.append(list(items)))
    # 接上主窗口真正的永久删除处理器，模拟实机接线：面板发信号 → 主窗口
    # 把图元移出场景 → 面板刷新时它就没了。
    window = view.window()
    panel.assets_purged.connect(window._on_assets_purged)
    # QMessageBox.question 被 conftest 统一答 Yes，不会阻塞。
    panel.purge_selected_assets()
    assert received and received[0][0] is item
    assert item not in view.scene.items()
    assert panel.asset_list.count() == 0


def test_trash_panel_has_two_tabs(view, qtbot):
    from prism.widgets.trash_panel import TrashPanel

    panel = TrashPanel(view)
    qtbot.addWidget(panel)
    titles = [panel.tabs.tabText(i) for i in range(panel.tabs.count())]
    assert titles == ['素材', '页面 / 文件夹']
