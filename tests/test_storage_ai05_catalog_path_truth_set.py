"""E01 独立真值集（目录/SQLite 路径）：与场景路径同一套顺序口径。

`ProjectAssetCatalog.query()` 的文档承诺「**两个数据源，一套口径**」。
这条用例把同一批素材分别放在两个数据源里，**手写**期望顺序，然后逐项对照：

* 场景路径：素材在内存里（`_title` 优先，再 `filename`，再 '未命名'）。
* 目录路径：素材只在 SQLite 目录里（`title` 列 + `filename` 列）。

排序键的契约是 `(画布路径, 标题, 保存 id)`，标题口径是**显示出来的那一个**
（`_build_result` 用 `_title or filename or '未命名'`）。所以目录路径必须用
**同一个规则**取标题，否则同一个查询在「目录就绪」与「回退场景」之间
会给出**不同的顺序**。

真值是手写的：素材 2 的标题 'Apple' 排在素材 1 的标题 'Zebra' 之前；
如果按文件名排（'a.jpg' < 'b.jpg'）就会反过来 —— 那正是被测的缺陷。
"""
import json
import sqlite3
from types import SimpleNamespace

from prism.project_catalog import ProjectAssetCatalog
from prism.project_open_scope import ProjectOpenScope


#: 手写夹具：标题与文件名**故意不同**，两条的相对顺序只由标题决定。
#:
#:   id   title     filename   canvas
#:   1    'Zebra'   'a.jpg'    'A'
#:   2    'Apple'   'b.jpg'    'A'
#:
#: 按标题：2 在前（'apple' < 'zebra'）
#: 按文件名：1 在前（'a.jpg' < 'b.jpg'）—— 这是被测缺陷会给出的顺序。
CATALOG_ROWS = (
    (1, 'Zebra', 'a.jpg', 'A'),
    (2, 'Apple', 'b.jpg', 'A'),
)


def _scope(tmp_path, rows):
    source = tmp_path / 'catalog-source.prism'
    with sqlite3.connect(source) as db:
        db.executescript(
            'CREATE TABLE items(id INTEGER PRIMARY KEY,type TEXT,x,y,z,scale,'
            'rotation,flip,data);'
            'CREATE TABLE sqlar(item_id INTEGER PRIMARY KEY,data BLOB);')
        for key, title, filename, canvas in rows:
            db.execute(
                'INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)',
                (key, 'pixmap', 0, 0, 0, 1, 0, 0,
                 json.dumps({'canvasId': canvas, 'title': title,
                             'filename': filename, 'tags': [],
                             'imageWidth': 16, 'imageHeight': 16},
                            ensure_ascii=False)))
    scope = ProjectOpenScope(source, tmp_path / 'catalog.sqlite')
    assert scope.build().complete
    return scope


class _StubScene:
    """一个没有 live 素材的场景：结果只能来自目录。

    注意它必须是**类**而不是 ``SimpleNamespace``：显示代理
    (``CatalogAssetProxy``) 会给场景建一个 weakref，命名空间实例不支持。
    """

    def __init__(self, live=()):
        self.project_id = 'test-project'
        self._live = list(live)
        self.workspace_pages = [dict(id='A', kind='canvas', title='A')]
        self._partial_project_source = None
        self._partial_project_session = None

    def items(self):
        return list(self._live)

    def iter_assets(self):
        return iter(list(self._live))

    def active_items(self):
        return list(self._live)


def _service():
    return SimpleNamespace(nodes=[], trashed_nodes=lambda: [])


def test_the_catalog_path_orders_by_the_title_it_displays(tmp_path):
    """目录路径：顺序按**显示出来的标题**，不是按文件名。

    真值（手写）：['Apple', 'Zebra']，即 id 2 在前。
    """
    scope = _scope(tmp_path, CATALOG_ROWS)
    catalog = ProjectAssetCatalog(_StubScene(), _service(),
                                  scope_provider=lambda: scope)
    results = catalog.query()
    assert [r.item.save_id for r in results] == [2, 1]
    # 顺便钉住显示值：标题就是显示出来的那一个
    assert [r.title for r in results] == ['Apple', 'Zebra']


def test_both_data_sources_give_the_same_order(tmp_path):
    """场景路径与目录路径对同一批素材给出**同一个顺序**。

    这是 query 文档里「两个数据源，一套口径」的直接检验。
    """
    scope = _scope(tmp_path, CATALOG_ROWS)

    catalog_only = ProjectAssetCatalog(_StubScene(), _service(),
                                       scope_provider=lambda: scope)
    by_catalog = [r.item.save_id for r in catalog_only.query()]

    # 同样的两条素材放进内存：没有 scope_provider → 走场景路径
    live = []
    for key, title, filename, canvas in CATALOG_ROWS:
        live.append(SimpleNamespace(
            save_id=key, TYPE='pixmap', _canvas_id=canvas, _title=title,
            filename=filename, _tags=[], _rating=0, _notes='',
            _deleted_at=None, isVisible=lambda: True))
    catalog_scene = ProjectAssetCatalog(_StubScene(live), _service())
    by_scene = [r.item.save_id for r in catalog_scene.query()]

    assert by_catalog == by_scene
    assert by_catalog == [2, 1]
