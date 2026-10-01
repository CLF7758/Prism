"""T2.5 收集箱的两个新能力：批量设置标签、选择目标页面。

任务书 T2.5 验收：

> 收集箱记录可以**批量设置标签**、**选择目标页面**、取消导入和删除。

后两项本来就有，前两项是这次补的。测的重点不是「按钮存在」，而是
**标签和页面真的传下去了** —— 这是最容易做成摆设的地方（界面加了
控件，但导入那一步把值丢了）。
"""
import pytest
from PyQt6 import QtCore, QtGui, QtWidgets

from prism.clipboard_capture import (ClipboardCaptureService,
                                     ClipboardSnapshot, InboxItem)
from prism.widgets.clipboard_inbox import ClipboardInboxDialog


def make_snapshot(payload=b'\x89PNG\r\n\x1a\n' + b'0' * 32, suffix='png'):
    return ClipboardSnapshot(payload, suffix, 'image/png', False)


def make_item(**kwargs):
    return InboxItem(make_snapshot(), **kwargs)


@pytest.fixture
def service(qapp):
    return ClipboardCaptureService()


@pytest.fixture
def dialog(service):
    return ClipboardInboxDialog(service)


# ── InboxItem 的标签字段 ─────────────────────────────────────────

def test_new_item_has_no_tags():
    assert make_item().tags == []


def test_tags_can_be_set():
    item = make_item()
    item.tags = ['参考', '待整理']
    assert item.tags == ['参考', '待整理']


def test_tags_do_not_share_a_list_between_items():
    """两条采集不能共用同一个列表对象 —— 改一个会连带改另一个。"""
    shared = ['参考']
    first = make_item(tags=shared)
    second = make_item(tags=shared)
    first.tags.append('其它')
    assert second.tags == ['参考']
    assert shared == ['参考']


def test_tags_from_constructor_are_copied():
    source = ['a']
    item = make_item(tags=source)
    item.tags.append('b')
    assert source == ['a']


# ── 目标页面下拉 ─────────────────────────────────────────────────

def test_page_box_defaults_to_current_canvas(dialog):
    assert dialog.page_box.count() == 1
    assert dialog.page_box.currentData() is None
    assert 'canvas' in dialog.page_box.currentText().lower() or \
        dialog.page_box.currentText() == '当前画布'


def test_refresh_pages_lists_the_workspace(qapp):
    service = ClipboardCaptureService()
    pages = [
        {'id': 'p1', 'kind': 'canvas', 'title': '画布一'},
        {'id': 'p2', 'kind': 'document', 'title': '笔记'},
    ]
    dialog = ClipboardInboxDialog(service, pages_provider=lambda: pages)
    dialog.refresh_pages()
    # 一行「当前画布」加上两个页面
    assert dialog.page_box.count() == 3
    assert dialog.page_box.itemData(1) == 'p1'
    assert dialog.page_box.itemData(2) == 'p2'
    assert '画布一' in dialog.page_box.itemText(1)


def test_refresh_pages_skips_unusable_entries(qapp):
    service = ClipboardCaptureService()
    pages = [
        {'id': 'ok', 'kind': 'canvas', 'title': '好页面'},
        {'kind': 'canvas', 'title': '没有 id'},
        'not a dict',
        None,
    ]
    dialog = ClipboardInboxDialog(service, pages_provider=lambda: pages)
    dialog.refresh_pages()
    assert dialog.page_box.count() == 2          # 当前画布 + 一个有效的


def test_refresh_pages_survives_a_broken_provider(qapp):
    """列页面出错不该让对话框崩 —— 那是界面上的一条辅助信息。"""
    def boom():
        raise RuntimeError('工作区炸了')

    service = ClipboardCaptureService()
    dialog = ClipboardInboxDialog(service, pages_provider=boom)
    dialog.refresh_pages()
    assert dialog.page_box.count() == 1


def test_refresh_pages_keeps_the_selection(qapp):
    service = ClipboardCaptureService()
    pages = [{'id': 'p1', 'kind': 'canvas', 'title': '一'}]
    dialog = ClipboardInboxDialog(service, pages_provider=lambda: pages)
    dialog.refresh_pages()
    dialog.page_box.setCurrentIndex(1)
    dialog.refresh_pages()
    assert dialog.page_box.currentData() == 'p1'


def test_no_provider_still_gives_the_current_canvas(dialog):
    dialog.refresh_pages()
    assert dialog.page_box.count() == 1
    assert dialog.page_box.currentData() is None


# ── 导入时把页面传下去 ───────────────────────────────────────────

def test_import_sends_the_chosen_page(qapp, monkeypatch):
    service = ClipboardCaptureService()
    # 不放真图片，只关心信号参数
    service.accept(make_snapshot())
    dialog = ClipboardInboxDialog(
        service, pages_provider=lambda: [
            {'id': 'p2', 'kind': 'document', 'title': '笔记'}])
    dialog.refresh_pages()
    dialog.page_box.setCurrentIndex(1)
    dialog.list_widget.setCurrentRow(0)

    seen = []
    dialog.import_requested.connect(lambda items, pid: seen.append(pid))
    dialog._import_selected()
    assert seen == ['p2']


def test_import_defaults_to_none_for_current_canvas(qapp):
    service = ClipboardCaptureService()
    service.accept(make_snapshot())
    dialog = ClipboardInboxDialog(service)
    dialog.list_widget.setCurrentRow(0)

    seen = []
    dialog.import_requested.connect(lambda items, pid: seen.append(pid))
    dialog._import_selected()
    assert seen == [None]


def test_import_without_selection_emits_nothing(qapp):
    service = ClipboardCaptureService()
    service.accept(make_snapshot())
    dialog = ClipboardInboxDialog(service)

    seen = []
    dialog.import_requested.connect(lambda items, pid: seen.append(pid))
    dialog.list_widget.clearSelection()
    dialog._import_selected()
    assert seen == []


# ── 批量设置标签 ─────────────────────────────────────────────────

def test_assign_tags_to_several_captures(qapp, monkeypatch):
    service = ClipboardCaptureService()
    # 三条内容各不相同 —— 服务按内容摘要去重，一样的东西只会留一条
    # （那正是 T2.5 要的行为，见 ClipboardCaptureService.accept）。
    for index in range(3):
        service.accept(make_snapshot(
            payload=b'\x89PNG\r\n\x1a\n' + f'{index}'.encode() * 32))
    dialog = ClipboardInboxDialog(service)
    for row in range(dialog.list_widget.count()):
        dialog.list_widget.item(row).setSelected(True)

    monkeypatch.setattr(QtWidgets.QInputDialog, 'getText',
                        staticmethod(lambda *a, **k: ('参考, 待整理', True)))
    dialog._assign_tags()
    assert [item.tags for item in service.items] == [
        ['参考', '待整理']] * 3


def test_assign_tags_strips_whitespace_and_drops_empties(qapp, monkeypatch):
    service = ClipboardCaptureService()
    service.accept(make_snapshot())
    dialog = ClipboardInboxDialog(service)
    dialog.list_widget.setCurrentRow(0)

    monkeypatch.setattr(QtWidgets.QInputDialog, 'getText',
                        staticmethod(lambda *a, **k: ('  a ,, b , ', True)))
    dialog._assign_tags()
    assert service.items[0].tags == ['a', 'b']


def test_assign_tags_cancelled_changes_nothing(qapp, monkeypatch):
    service = ClipboardCaptureService()
    service.accept(make_snapshot())
    dialog = ClipboardInboxDialog(service)
    dialog.list_widget.setCurrentRow(0)

    monkeypatch.setattr(QtWidgets.QInputDialog, 'getText',
                        staticmethod(lambda *a, **k: ('新标签', False)))
    dialog._assign_tags()
    assert service.items[0].tags == []


def test_assign_tags_without_selection_reports_and_returns(qapp):
    service = ClipboardCaptureService()
    dialog = ClipboardInboxDialog(service)
    dialog._assign_tags()                        # 不该炸
    assert 'Select' in dialog.feedback.text()


def test_assign_tags_prefills_the_current_tags(qapp, monkeypatch):
    service = ClipboardCaptureService()
    service.accept(make_snapshot())
    dialog = ClipboardInboxDialog(service)
    dialog.list_widget.setCurrentRow(0)
    service.items[0].tags = ['已有']

    captured = {}

    def fake(parent, title, label, text=''):
        captured['text'] = text
        return ('', False)

    monkeypatch.setattr(QtWidgets.QInputDialog, 'getText',
                        staticmethod(fake))
    dialog._assign_tags()
    assert captured['text'] == '已有'
