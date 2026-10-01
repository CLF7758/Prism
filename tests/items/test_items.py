from PyQt6 import QtGui

from prism.items import sort_by_filename, PrismPixmapItem, PrismTextItem


def test_sort_by_filename(view):
    item1 = PrismPixmapItem(QtGui.QImage())

    item2 = PrismPixmapItem(QtGui.QImage())
    item2.filename = 'foo.png'
    item2.save_id = 66

    item3 = PrismPixmapItem(QtGui.QImage())
    item3.save_id = 33

    item4 = PrismPixmapItem(QtGui.QImage())
    item4.filename = 'bar.png'
    item4.save_id = 77

    item5 = PrismPixmapItem(QtGui.QImage())
    item5.save_id = 22

    result = sort_by_filename([item1, item2, item3, item4, item5])
    assert result == [item4, item2, item5, item3, item1]


def test_sort_by_filename_when_only_by_filename(view):
    item1 = PrismPixmapItem(QtGui.QImage())
    item1.filename = 'foo.png'
    item2 = PrismPixmapItem(QtGui.QImage())
    item2.filename = 'bar.png'
    assert sort_by_filename([item1, item2]) == [item2, item1]


def test_sort_by_filename_when_only_by_save_id(view):
    item1 = PrismPixmapItem(QtGui.QImage())
    item1.save_id = 66
    item2 = PrismPixmapItem(QtGui.QImage())
    item2.save_id = 33
    assert sort_by_filename([item1, item2]) == [item2, item1]


def test_sort_by_filename_deals_with_text_items(view):
    item1 = PrismTextItem('Foo')
    item2 = PrismTextItem('Bar')
    assert len(sort_by_filename([item1, item2])) == 2
