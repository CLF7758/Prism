"""T9 的 ExportService：把导出意图翻译成导出器。

任务书 T9 要求它管四种导出（原图、调整图、当前页面、当前场景），§3.3
要求导出时必须让用户**明确**选的是哪一种。这个文件测的正是这层映射 ——
尤其是任务书 T2 那条：

    没有可验证原始字节时，不得假装提供无损原图导出。

也就是「原图导出」不能悄悄降级成「重新编码一张差不多的图」。
"""
import pytest

from prism.export_service import (ExportService, ExportTarget,
                                  OriginalExportUnavailable, TARGET_LABELS)


class FakeItem:
    """一个够用的假素材：只关心有没有原字节。"""

    def __init__(self, payload=None, save_id='x'):
        self.save_id = save_id
        self._payload = payload

    def original_bytes(self):
        return self._payload


class FakeScene:
    """只实现服务用到的那几个方法。"""

    def __init__(self, items=()):
        self._items = list(items)

    def items_for_save(self):
        return list(self._items)

    def selectedItems(self, user_only=False):        # noqa: N802
        return [i for i in self._items if getattr(i, 'selected', False)]


def plain_item(selected=False):
    item = FakeItem(payload=b'\x89PNG\r\n\x1a\n' + b'0' * 20)
    item.selected = selected
    return item


def generated_item(selected=False):
    """画布上生成的素材：没有原字节。"""
    item = FakeItem(payload=None)
    item.selected = selected
    return item


@pytest.fixture
def service():
    return ExportService(FakeScene([plain_item(), plain_item()]))


# ── 四种目标都要能出计划 ─────────────────────────────────────────

@pytest.mark.parametrize('target', list(ExportTarget))
def test_every_target_produces_a_plan(service, target):
    items = [plain_item()] if target in (ExportTarget.ORIGINAL,
                                         ExportTarget.ADJUSTED) else None
    plan = service.plan(target, items=items)
    assert plan.target is target
    assert plan.exporter is not None


def test_every_target_has_a_label():
    """界面上要摆这几个选项，每个都得有话说。"""
    for target in ExportTarget:
        assert target in TARGET_LABELS
        assert TARGET_LABELS[target].strip()


# ── 原图 vs 调整后：这两条最容易混 ───────────────────────────────

def test_original_does_not_reencode(service):
    plan = service.plan(ExportTarget.ORIGINAL, items=[plain_item()])
    assert plan.reencodes is False
    assert plan.lossless is True
    assert plan.options == {'adjusted': False}


def test_adjusted_reencodes_and_says_so(service):
    plan = service.plan(ExportTarget.ADJUSTED, items=[plain_item()])
    assert plan.reencodes is True
    # 有原字节也不能说"无损"——它本来就是重新编码出来的
    assert plan.lossless is False
    assert plan.options == {'adjusted': True}
    assert any('不会修改原图' in note for note in plan.notes), (
        '任务书要求明确告诉用户这是新文件。实得 %r' % (plan.notes,))


def test_original_and_adjusted_use_different_options(service):
    """同一条路径、同一个导出器，靠 adjusted 区分 —— 别把两者搞成一回事。"""
    original = service.plan(ExportTarget.ORIGINAL, items=[plain_item()])
    adjusted = service.plan(ExportTarget.ADJUSTED, items=[plain_item()])
    assert original.exporter is adjusted.exporter
    assert original.options != adjusted.options


# ── 原图不能假装（任务书 T2）───────────────────────────────────

def test_original_refuses_when_nothing_has_bytes(service):
    with pytest.raises(OriginalExportUnavailable) as info:
        service.plan(ExportTarget.ORIGINAL, items=[generated_item()])
    assert '原始字节' in str(info.value)


def test_original_with_some_missing_bytes_still_works(service):
    """部分缺原字节照样能导 —— 有原字节的逐字节复制，其余导渲染结果。

    但这**不是无损**的，所以 lossless 要为 False，说明里也要如实写出来。
    第一版实现把这种情况也当成"拒绝"，接上界面就会发现行为比原来差了：
    原来只是问用户一句。
    """
    plan = service.plan(ExportTarget.ORIGINAL,
                        items=[plain_item(), generated_item()])
    assert plan.reencodes is False, '导出器本身仍走原始 payload 那条路'
    assert plan.lossless is False, '有素材要重新编码，就不能声称无损'
    # 这一条进的是 warnings 而不是 notes：界面要先问用户一句再走，因为
    # 他可能不知道有几张会变样。notes 是"显示完就往下走"的那一类。
    assert any('1 / 2' in warning for warning in plan.warnings), (
        '必须说清有几张要重新编码，而且要用户点头。实得 %r'
        % (plan.warnings,))
    assert not plan.notes, '这件事不该混进"不用确认"的那一类'


def test_all_ready_goes_into_notes_not_warnings(service):
    """全都有原字节时不用问用户 —— 那是"告诉一声"，不是"要确认"。"""
    plan = service.plan(ExportTarget.ORIGINAL, items=[plain_item()])
    assert plan.notes, '有多少张能逐字节复制，值得说一句'
    assert not plan.warnings, '全都好好的，不该拦着用户'


def test_scene_export_has_no_warnings(service):
    """场景导出本来就不是原图，没什么要用户确认的。"""
    plan = service.plan(ExportTarget.SCENE)
    assert not plan.warnings


def test_partial_readiness_reports_both_counts(service):
    readiness = ExportService.check_original(
        [plain_item(), plain_item(), generated_item()])
    assert readiness.total == 3
    assert readiness.with_bytes == 2
    assert readiness.without_bytes == 1
    assert readiness.partial is True
    assert readiness.all_ready is False
    assert readiness.none_ready is False
    assert readiness.lossless is False


def test_adjusted_still_works_without_bytes(service):
    """没有原字节只是做不了**原图**导出，调整后导出照做。"""
    plan = service.plan(ExportTarget.ADJUSTED, items=[generated_item()])
    assert plan.exporter is not None


def test_original_with_no_items_is_refused(service):
    with pytest.raises(ValueError):
        service.plan(ExportTarget.ORIGINAL, items=[])


def test_check_original_explains_itself():
    ready = ExportService.check_original([plain_item(), plain_item()])
    assert ready.all_ready is True
    assert ready.lossless is True
    assert '2 个素材' in ready.summary()

    empty = ExportService.check_original([])
    assert empty.total == 0
    assert empty.all_ready is False
    assert empty.none_ready is True, '一个都没有，也算"全都没有原字节"'
    assert empty.summary().strip()

    nothing = ExportService.check_original([generated_item()])
    assert nothing.none_ready is True
    assert nothing.summary().strip()


def test_check_original_survives_a_broken_item():
    """素材对象的 original_bytes 抛异常时，不能把整个导出带崩。

    那张按"没有原字节"处理，其余照常。
    """
    class Angry(FakeItem):
        def original_bytes(self):
            raise RuntimeError('我不干了')

    ready = ExportService.check_original([Angry(), plain_item()])
    assert ready.with_bytes == 1
    assert ready.without_bytes == 1
    assert ready.partial is True

    only = ExportService.check_original([Angry()])
    assert only.none_ready is True


def test_check_original_handles_missing_method():
    """没有 original_bytes 接口的对象（比如文本素材）不算有原字节。"""
    class Bare:
        save_id = 'y'

    ready = ExportService.check_original([Bare()])
    assert ready.none_ready is True
    assert ready.total == 1


# ── 哪批素材 ────────────────────────────────────────────────────

def test_items_come_from_the_scene_when_not_given():
    scene = FakeScene([plain_item(), plain_item(), plain_item()])
    service = ExportService(scene)
    assert len(service.items_for()) == 3


def test_selected_only_picks_the_selected_ones():
    scene = FakeScene([plain_item(True), plain_item(False),
                       plain_item(True)])
    service = ExportService(scene)
    assert len(service.items_for(selected_only=True)) == 2


def test_explicit_items_win_over_the_scene():
    """服务不该去猜界面选了什么 —— 传了就用传的。"""
    service = ExportService(FakeScene([plain_item()] * 5))
    mine = [plain_item()]
    assert service.items_for(items=mine) == mine


def test_no_scene_is_not_a_crash():
    service = ExportService(None)
    assert service.items_for() == []
    assert service.items_for(selected_only=True) == []


def test_list_is_copied_not_aliased():
    """返回的列表不该和 scene 内部那个是同一个对象。"""
    scene = FakeScene([plain_item()])
    service = ExportService(scene)
    got = service.items_for()
    got.append(plain_item())
    assert len(scene.items_for_save()) == 1


# ── 场景导出 ────────────────────────────────────────────────────

def test_scene_export_uses_the_pixmap_exporter(service):
    plan = service.plan(ExportTarget.SCENE)
    from prism.fileio.export import SceneToPixmapExporter
    assert plan.exporter is SceneToPixmapExporter


def test_scene_export_warns_it_is_not_an_original(service):
    """用户容易以为"导出场景"就是导出原图，所以要提示。"""
    plan = service.plan(ExportTarget.SCENE)
    assert any('合成结果' in note for note in plan.notes)


# ── 文档与脑图：交给各自的模块 ──────────────────────────────────

def test_document_and_mindmap_point_at_their_modules(service):
    assert service.plan(ExportTarget.DOCUMENT).exporter == \
        'prism.fileio.document_export'
    assert service.plan(ExportTarget.MINDMAP).exporter == \
        'prism.fileio.mindmap_export'


def test_unknown_target_is_rejected(service):
    with pytest.raises(ValueError):
        service.plan('随便什么都行')
