"""拖一个文件夹进来 = 按文件夹结构建画布。

用户的原话：「文件夹里面所有的图片还有视频就在这个画布了」。规则是他定的：

* **一层文件夹一个画布**；
* 文件夹里直接是图片视频 —— 就只建那一个；
* 套了子文件夹 —— 每层各建一个，父子关系跟着目录层级走；
* 同名画布自动加序号（`WorkspaceService.next_title` 本来就这么做）。

`folder_trees` 由拖放那边收集（有媒体的目录 -> 目录名），这里验的是拿到它
之后怎么建画布、怎么把素材分过去。
"""
import os

import pytest
from PyQt6 import QtGui

from prism.items import PrismPixmapItem


class _FakeItem:
    """只带画布归属和文件名的最小替身。

    `canvas_id` 要是真属性 —— 归属走的是 `commands.ChangeMetadata`，
    它读 `item.canvas_id` 再写 `item._canvas_id`。
    """

    def __init__(self, filename):
        self.filename = filename
        self.save_id = 1
        self._canvas_id = 'default-canvas'

    @property
    def canvas_id(self):
        return self._canvas_id

    @canvas_id.setter
    def canvas_id(self, value):
        self._canvas_id = value

    def scene(self):
        """`ChangeMetadata` 会问图元在哪个场景里 —— 替身没有场景，回 None。"""
        return None


def _pages(view):
    return [page for page in getattr(view.scene, 'workspace_pages', []) or []
            if page.get('kind') == 'canvas']


def _titles(view):
    return [page.get('title') for page in _pages(view)]


def _canvases_of(view, trees, items=()):
    """跑一遍建画布，返回 {目录: 画布 id}。"""
    return view._create_canvases_for_folders(trees, list(items))


# ── 一层文件夹 ───────────────────────────────────────────────────


def test_a_flat_folder_becomes_one_canvas(view, qapp):
    """文件夹里直接是图片视频 —— 只建一个画布。"""
    root = os.path.normpath(r'C:\参考\Stray')
    trees = {root: 'Stray'}
    item = _FakeItem(os.path.join(root, 'ref_001.png'))

    canvases = _canvases_of(view, trees, [item])

    assert len(canvases) == 1
    assert list(_titles(view))[-1] == 'Stray'
    assert item._canvas_id == canvases[root]
    assert item._canvas_id != 'default-canvas'


def test_items_land_on_the_canvas_of_their_own_folder(view, qapp):
    """两个文件夹的素材各归各的画布，不会全塞进第一个。"""
    left = os.path.normpath(r'C:\参考\Stray')
    right = os.path.normpath(r'C:\参考\Control')
    trees = {left: 'Stray', right: 'Control'}
    a = _FakeItem(os.path.join(left, 'a.png'))
    b = _FakeItem(os.path.join(right, 'b.png'))

    canvases = _canvases_of(view, trees, [a, b])

    assert a._canvas_id == canvases[left]
    assert b._canvas_id == canvases[right]
    assert a._canvas_id != b._canvas_id


# ── 套子文件夹 ───────────────────────────────────────────────────


def test_nested_folders_become_nested_canvases(view, qapp):
    """文件夹套文件夹 —— 每层各建一个，父子关系跟着目录走。"""
    root = os.path.normpath(r'C:\参考')
    inner = os.path.join(root, 'Stray')
    trees = {root: '参考', inner: 'Stray'}
    outside = _FakeItem(os.path.join(root, 'top.png'))
    inside = _FakeItem(os.path.join(inner, 'deep.png'))

    canvases = _canvases_of(view, trees, [outside, inside])

    pages = {page['id']: page for page in _pages(view)}
    assert len(canvases) == 2
    assert pages[canvases[inner]]['parent'] == canvases[root]
    assert outside._canvas_id == canvases[root]
    assert inside._canvas_id == canvases[inner]


def test_a_folder_holding_only_subfolders_gets_no_empty_canvas(view, qapp):
    """中间那层只有子文件夹、自己没有媒体 —— 不建空画布。

    子画布挂到最近的、真的建出来的祖先上。
    """
    root = os.path.normpath(r'C:\参考')
    middle = os.path.join(root, 'middle')          # 只有子文件夹
    leaf = os.path.join(middle, 'Stray')
    trees = {root: '参考', leaf: 'Stray'}           # middle 不在里面

    canvases = _canvases_of(view, {root: '参考'}, [])
    canvases.update(_canvases_of(view, trees, [
        _FakeItem(os.path.join(leaf, 'x.png'))]))

    pages = {page['id']: page for page in _pages(view)}
    assert 'Stray' not in [page['title'] for page in pages.values()
                           if page['id'] == canvases[root]]
    assert canvases[leaf] in pages
    assert pages[canvases[leaf]]['parent'] == canvases[root]


# ── 重名 ─────────────────────────────────────────────────────────


def test_a_second_folder_with_the_same_name_gets_a_number(view, qapp):
    """重名画布自动加序号，不再堆两个一模一样的名字。"""
    first = os.path.normpath(r'D:\a\Stray')
    second = os.path.normpath(r'D:\b\Stray')

    _canvases_of(view, {first: 'Stray'}, [])
    _canvases_of(view, {second: 'Stray'}, [])

    assert 'Stray' in _titles(view)
    assert 'Stray 2' in _titles(view)


# ── 边界 ─────────────────────────────────────────────────────────


def test_items_outside_every_dropped_folder_are_left_alone(view, qapp):
    """只在画布上、不属于这次拖进来的任何文件夹的素材，归属不许动。"""
    root = os.path.normpath(r'C:\参考\Stray')
    stranger = _FakeItem(os.path.normpath(r'C:\别处\x.png'))

    _canvases_of(view, {root: 'Stray'}, [stranger])

    assert stranger._canvas_id == 'default-canvas'


def test_an_item_without_a_filename_is_left_alone(view, qapp):
    """画出来的线、粘进来的文字没有文件名 —— 不碰它们。"""
    root = os.path.normpath(r'C:\参考\Stray')
    nameless = _FakeItem('')
    nameless._canvas_id = 'default-canvas'

    _canvases_of(view, {root: 'Stray'}, [nameless])

    assert nameless._canvas_id == 'default-canvas'


def test_no_folders_means_nothing_happens(view, qapp):
    before = len(_pages(view))
    assert view._create_canvases_for_folders({}, []) == {}
    assert len(_pages(view)) == before


def test_the_new_canvases_reach_the_sidebar_tree(view, qapp):
    """建完画布，左栏资源树里必须真的出现它们。

    回归：用户报「拖进去以后没有给我创建画布」。画布**建出来了**（数据里有、
    素材也归好了），但左栏看不见 —— `rebuild_workspace_tree` 原本只在打开
    工程和回收站操作时被调用，新建页面走的那条路只管页签，资源树不知道多了
    东西。
    """
    root = os.path.normpath(r'C:\参考\Stray')
    _canvases_of(view, {root: 'Stray'}, [])

    service = view.category_panel.resource_service
    pages = [node for node in service.nodes
             if node.get('section') == 'canvas'
             and node.get('nodeType') == 'page']
    assert 'Stray' in [node.get('title') for node in pages]


def test_the_items_are_moved_through_the_undo_stack(view, qapp):
    """素材归属变化要能撤销 —— 和左栏把素材拖到画布行走同一条路。"""
    root = os.path.normpath(r'C:\参考\Stray')
    item = PrismPixmapItem(QtGui.QImage(20, 15,
                                        QtGui.QImage.Format.Format_RGB32))
    item.filename = os.path.join(root, 'a.png')
    view.scene.addItem(item)
    before = item._canvas_id

    _canvases_of(view, {root: 'Stray'}, [item])

    assert item._canvas_id != before, '归属该被改掉'
    view.undo_stack.undo()
    assert item._canvas_id == before, 'Ctrl+Z 该把它送回去'


# ── 拖放那一头：目录树收得对不对 ─────────────────────────────────


def _drop(view, paths):
    """把一组本地路径当成一次拖放丢给 view，不真的加载素材。"""
    from PyQt6 import QtCore, QtGui

    captured = {}

    def capture(files, pos=None, **kwargs):
        captured['files'] = files
        captured.update(kwargs)

    view.do_insert_images = capture
    mime = QtCore.QMimeData()
    mime.setUrls([QtCore.QUrl.fromLocalFile(str(p)) for p in paths])
    event = QtGui.QDropEvent(
        QtCore.QPointF(10, 10), QtCore.Qt.DropAction.CopyAction, mime,
        QtCore.Qt.MouseButton.LeftButton,
        QtCore.Qt.KeyboardModifier.NoModifier)
    view.dropEvent(event)
    return captured


def test_dropping_a_folder_collects_the_media_directory_tree(view, qapp,
                                                             tmp_path):
    """目录树只记**有媒体**的目录；空目录不进去。"""
    root = tmp_path / '参考'
    inner = root / 'Stray'
    empty = root / '空目录'
    inner.mkdir(parents=True)
    empty.mkdir()
    (root / 'top.png').write_bytes(b'x')
    (inner / 'deep.mp4').write_bytes(b'x')

    captured = _drop(view, [root])

    trees = captured.get('folder_trees') or {}
    assert trees == {os.path.normpath(str(root)): '参考',
                     os.path.normpath(str(inner)): 'Stray'}
    assert os.path.normpath(str(empty)) not in trees
    # 分类那条路照旧（用户没要求动它）
    assert captured.get('folder_categories') == {
        os.path.normpath(str(root)): '参考'}
    collected = {os.path.normpath(u.toLocalFile()) for u in captured['files']}
    assert collected == {os.path.normpath(str(root / 'top.png')),
                         os.path.normpath(str(inner / 'deep.mp4'))}


def test_dropping_plain_files_asks_for_no_canvases(view, qapp, tmp_path):
    """拖的是单个文件（不是文件夹）—— 不建画布，素材照旧归当前画布。"""
    picture = tmp_path / 'one.png'
    picture.write_bytes(b'x')

    captured = _drop(view, [picture])

    assert captured.get('folder_trees') is None
    assert captured.get('folder_categories') is None
    assert len(captured['files']) == 1


def test_a_folder_without_media_builds_nothing(view, qapp, tmp_path):
    """文件夹里没有图片视频 —— 和以前一样什么都不做。"""
    root = tmp_path / '文档'
    root.mkdir()
    (root / 'readme.txt').write_text('x')

    captured = _drop(view, [root])

    assert captured == {}
