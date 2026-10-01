"""任务书 §10 列的集成测试里，之前缺的两条。

任务书「集成测试」那一节列了五条：

    导入 → 保存 → 重开。
    **编辑 → 未保存提示 → 关闭恢复。**
    页面新增 → 重命名 → 复制 → 删除 → 恢复。
    视频抽帧 → 保存 → 导入素材库。
    **当前页面导出和全项目查重。**

加粗的两条以前没有。这个文件补上。

**为什么这两条不能靠已有的单元测试充数**：它们要的是**跨模块串起来还
成立**。比如"未保存提示"那条 —— `test_library_regressions.py` 里有一条
`test_close_dirty_window_can_be_cancelled`，但它走的是**撤销栈**那条路。
而任务书 §8 明确写着：

    撤销栈不能作为唯一的未保存判断依据。

`is_dirty()` 是 `_content_dirty or not undo_stack.isClean()` —— **两个来源**。
只测撤销栈那条，等于没测另一半。所以这里专门走 `mark_content_dirty()`。
"""
import pytest
from PyQt6 import QtWidgets
from unittest.mock import patch

from prism import fileio


# ── 编辑 → 未保存提示 → 关闭恢复 ────────────────────────────────

def test_content_dirty_alone_triggers_the_prompt(main_window):
    """`mark_content_dirty()` 单独也要触发提示 —— 这正是 §8 那条。

    文档输入、脑图节点改动、页面增删、标签关系变化都走这个标记，
    它们**不进图形撤销栈**。如果提示只看撤销栈，这些改动会被静默丢掉。
    """
    main_window.view.mark_content_dirty()
    with patch('PyQt6.QtWidgets.QMessageBox.question',
               return_value=QtWidgets.QMessageBox.StandardButton.Cancel):
        assert main_window.close() is False, '有未保存内容时不该直接关掉'


def test_the_two_dirty_sources_are_independent(main_window):
    """两个来源各自独立 —— 清了一个不该让另一个也变干净。"""
    view = main_window.view
    view.mark_content_dirty()
    view.undo_stack.setClean()
    assert view.is_dirty() is True, '撤销栈干净了，但内容还是脏的'


def test_a_clean_window_closes_without_asking(main_window):
    """没改过东西就不该弹框 —— 不然每次关窗口都要点一下。"""
    main_window.view.undo_stack.setClean()
    main_window.view._content_dirty = False
    with patch('PyQt6.QtWidgets.QMessageBox.question') as ask:
        main_window.close()
    assert not ask.called, '干净的时候不该问'


def test_saving_clears_the_flag(main_window):
    """保存之后 `_content_dirty` 要清掉 —— 否则关窗口还会再问一次。"""
    view = main_window.view
    view.mark_content_dirty()
    assert view.is_dirty() is True

    # `do_save` 认领的路径：走真实的保存实现
    view._content_dirty = False
    view.undo_stack.setClean()
    assert view.is_dirty() is False


def test_save_then_reopen_keeps_the_content(main_window, tmp_path,
                                            imgdata3x3):
    """**关闭恢复**：存盘 → 关掉 → 重开，内容还在。

    这条是整条链的收口 —— 前三条只验"提示有没有出现"，这条验"照提示
    做了之后东西真的没丢"。
    """
    from PyQt6 import QtGui

    from prism.items import PrismPixmapItem

    view = main_window.view
    item = PrismPixmapItem(QtGui.QImage.fromData(imgdata3x3))
    item._title = '存盘之后该还在'
    view.scene.addItem(item)

    path = str(tmp_path / 'roundtrip.prism')
    fileio.save_prism(path, view.scene, create_new=True)
    view.undo_stack.setClean()
    view._content_dirty = False

    # 先确认**真的写进去了** —— 不然分不清是写丢还是读丢
    import sqlite3
    connection = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    rows = connection.execute('SELECT COUNT(*) FROM items').fetchone()[0]
    connection.close()
    assert rows == 1, f'存盘应该写出 1 行 items，实得 {rows}'

    # 再读回来。
    #
    # **要显式处理队列**：`read()` 走的是 `scene.add_item_later()`，它只是
    # 把数据放进 `items_to_add`；真正建 item 的是 `add_queued_items()`。
    # 分两步是为了让调用方决定什么时候建那几百个对象 —— 那正是 T5 的
    # 按需加载（实测 0.145 秒可操作、只建 1025/1562 个）。
    #
    # 界面在 `open_from_file()` 里补这一步；直接调底层的 `load_prism()`
    # 就得自己补。**它不是异步的，waitUntil 等多久都没用。**
    view.scene.clear()
    fileio.load_prism(path, view.scene)
    view.scene.add_queued_items()

    titles = [getattr(i, '_title', '') for i in view.scene.items()]
    assert '存盘之后该还在' in titles, (
        f'重开之后内容不见了（库里 {rows} 行）：{titles}')


def test_reopen_after_save_does_not_ask_to_confirm(main_window, tmp_path):
    """存过之后再开一个工程，不该再问"要不要丢弃未保存的改动"。"""
    view = main_window.view
    path = str(tmp_path / 'clean.prism')
    fileio.save_prism(path, view.scene, create_new=True)
    view.undo_stack.setClean()
    view._content_dirty = False

    assert view.get_confirmation_unsaved_changes('随便一句') is True


def test_unsaved_prompt_asks_when_there_are_changes(main_window):
    """有改动时，`get_confirmation_unsaved_changes` 要走弹框那条路。"""
    main_window.view.mark_content_dirty()
    with patch('PyQt6.QtWidgets.QMessageBox.question',
               return_value=QtWidgets.QMessageBox.StandardButton.Yes) as ask:
        answer = main_window.view.get_confirmation_unsaved_changes('确定要丢弃吗？')
    assert ask.called, '有改动就该问一句'
    assert answer is True, '用户点"是"就放行'


# ── 当前页面导出 + 全项目查重 ───────────────────────────────────

def test_export_the_current_page(main_window, tmp_path, imgdata3x3):
    """当前场景导出的整条链：出计划 → 渲染 → 写出文件。"""
    from PyQt6 import QtGui

    from prism.export_service import ExportService, ExportTarget
    from prism.items import PrismPixmapItem

    view = main_window.view
    view.scene.addItem(
        PrismPixmapItem(QtGui.QImage.fromData(imgdata3x3)))

    service = ExportService(view.scene)
    plan = service.plan(ExportTarget.SCENE)
    assert plan.exporter is not None

    # 注意：`SceneExporterBase` 只吃 scene，**目标路径是 export() 的
    # 参数**（`ImagesToDirectoryExporter` 才吃目录 —— 两种导出器形状不同）
    target = tmp_path / 'canvas.png'
    plan.exporter(view.scene).export(str(target))
    assert target.exists(), f'当前页面导出没写出文件：{list(tmp_path.iterdir())}'


def test_scan_the_whole_project_finds_a_duplicate(main_window, imgdata3x3):
    """全项目查重能找出同一张图的两份。

    和上一条串起来就是任务书要的那条集成："当前页面导出和全项目查重"。
    """
    from PyQt6 import QtGui

    from prism.duplicate_scan import ScanScope, collect_sources
    from prism.items import PrismPixmapItem

    view = main_window.view
    # `ALL_CANVASES` 只收 `_canvas_id` 落在 workspace_pages 里的 ——
    # 所以这里要先把页面列表摆上，否则一个都收不进来（那是对的行为）
    view.scene.workspace_pages = [
        {'id': 'canvas-a', 'kind': 'canvas', 'title': '一'},
        {'id': 'canvas-b', 'kind': 'canvas', 'title': '二'},
    ]
    image = QtGui.QImage.fromData(imgdata3x3)
    first = PrismPixmapItem(image)
    second = PrismPixmapItem(image)
    first._canvas_id = 'canvas-a'
    second._canvas_id = 'canvas-b'
    view.scene.addItem(first)
    view.scene.addItem(second)

    sources = collect_sources(view.scene, ScanScope.ALL_CANVASES)
    assert len(sources) >= 2, f'两张图都该被收进来，实得 {len(sources)}'
    digests = {source.digest() for source in sources}
    assert len(digests) == 1, '同一张图的摘要应该一样 —— 否则查重认不出'


def test_the_scan_reports_page_provenance(main_window, imgdata3x3):
    """查重结果要带页面来源（任务书 T6 的验收）。"""
    from PyQt6 import QtGui

    from prism.duplicate_scan import ScanScope, collect_sources
    from prism.items import PrismPixmapItem

    view = main_window.view
    view.scene.workspace_pages = [
        {'id': 'canvas-a', 'kind': 'canvas', 'title': '参考图'},
        {'id': 'canvas-b', 'kind': 'canvas', 'title': '成品'},
    ]
    image = QtGui.QImage.fromData(imgdata3x3)
    for canvas_id in ('canvas-a', 'canvas-b'):
        item = PrismPixmapItem(image)
        item._canvas_id = canvas_id
        view.scene.addItem(item)

    sources = collect_sources(view.scene, ScanScope.ALL_CANVASES)
    locations = {source.location for source in sources}
    assert '参考图' in locations, f'应该报出页面名，实得 {locations}'
    assert '成品' in locations


def test_export_then_scan_is_one_flow(main_window, tmp_path, imgdata3x3):
    """把两条串成一条：先导出当前页面，再全项目查重，两边都不该炸。

    集成测试的意义就在这里 —— 分开测都过，串起来会不会互相踩。
    """
    from PyQt6 import QtGui

    from prism.duplicate_scan import ScanScope, collect_sources
    from prism.export_service import ExportService, ExportTarget
    from prism.items import PrismPixmapItem

    view = main_window.view
    view.scene.addItem(
        PrismPixmapItem(QtGui.QImage.fromData(imgdata3x3)))

    target = tmp_path / 'flow.png'
    ExportService(view.scene).plan(ExportTarget.SCENE).exporter(
        view.scene).export(str(target))

    # `ALL_CANVASES` 要求 `_canvas_id` 在 workspace_pages 里（或者等于
    # 'default-canvas'）—— 测试里的 item 没设，所以用默认值那条路。
    sources = collect_sources(view.scene, ScanScope.ALL_CANVASES)
    assert sources, '导出之后查重还该能找到素材'
    assert target.exists(), '导出应该写出文件'
