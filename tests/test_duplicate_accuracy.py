"""查重的误报和漏报 —— 用一批"已知答案"的图来量。

任务书 §6 的算法要求写着：

    先用固定样例比较误报和漏报，再决定默认算法和阈值。

这个文件就是那批固定样例。做法是**程序化生成**图片而不是拿外部素材 ——
生成的可复现、能进版本库、而且"期望答案"是构造时就定死的，不是事后
看着结果补的。

每个样例是一句话：**图 A 和图 B 应不应该算重复**。跑完之后数两件事：

    漏报（false negative）：该算重复的没算出来 —— 用户会留下重复文件
    误报（false positive）：不该算的算成了重复 —— 用户会删掉不该删的

后者更危险。所以"不该算"那组比"该算"的更多也更刁。

**【样例图的设计很要紧，第一版就栽在这】**

第一版用逐像素的取模噪声造图，结果三条"该算重复"的全漏报了：噪声图在
缩放或 JPEG 重编码之后 dHash 几乎全变。而 dHash 是**感知**哈希，它假设
图有大块结构 —— 真实照片正是那样。换成少数几个大色块之后，缩放和重编码
的影响就落回它该在的量级上。

同理，第一版拿"横条纹 vs 竖条纹"当反例，结果误报了：dHash 只看横向相邻
像素的梯度，规则条纹在它眼里本来就很像。那不是缺陷，是这类算法的已知
边界 —— 但它**不适合当反例**，所以换成了布局不同的色块。
"""
import pytest
from PyQt6 import QtCore, QtGui

from prism.similarity import (average_color, color_distance,
                              difference_hash, hamming_distance)

#: 服务里用的默认阈值（`duplicate_scan.DuplicateScanService`）。样例按这
#: 两个数判定，调阈值时这里跟着改，就能看出代价。
MAX_DISTANCE = 4
COLOR_LIMIT = 40

#: 样例图边长。dHash 只看 8×8 的梯度，64 够用；再大只是让逐像素的
#: Python 循环变慢，而这组样例是用来频繁跑的。
SIDE = 64

GREYS = [(40, 40, 40), (110, 110, 110), (180, 180, 180), (240, 240, 240)]
#: 彩色版本。**别拿灰阶测"换色"** —— 灰阶图 R=G=B，把 RGB 通道换位之后
#: 像素值一个都没变，那测出来的是"两张相同的图"（第二版就栽在这）。
COLOURS = [(190, 60, 40), (60, 140, 190), (200, 190, 60), (80, 180, 90)]


def blocks(layout, colours):
    """把一张图切成 4×4 的大色块。

    `layout` 是 16 个索引，`colours` 是颜色表。用色块而不是逐像素噪声，
    是因为 dHash 是感知哈希：噪声图缩放后哈希全变，色块图不会。
    """
    cell = SIDE // 4

    def paint(x, y):
        index = layout[(y // cell) * 4 + (x // cell)]
        return colours[index % len(colours)]

    return paint


def default_paint(seed):
    order = [(i * 7 + seed * 3) % 16 for i in range(16)]
    return blocks(order, GREYS)


def make_image(paint=None, seed=0, width=SIDE, height=SIDE):
    """造一张图。`paint` 拿到 (x, y) 返回 (r, g, b)。"""
    if paint is None:
        paint = default_paint(seed)
    image = QtGui.QImage(width, height, QtGui.QImage.Format.Format_RGB32)
    for y in range(height):
        for x in range(width):
            image.setPixel(x, y, QtGui.QColor(*paint(x, y)).rgb())
    return image


def scaled(image, factor=0.5):
    """缩一半再放大回来 —— 相当于"这张图被缩过之后又保存了"。"""
    small = image.scaled(max(1, int(image.width() * factor)),
                         max(1, int(image.height() * factor)),
                         QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
                         QtCore.Qt.TransformationMode.SmoothTransformation)
    return small.scaled(image.width(), image.height(),
                        QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
                        QtCore.Qt.TransformationMode.SmoothTransformation)


def brightened(image, delta):
    out = QtGui.QImage(image)
    for y in range(out.height()):
        for x in range(out.width()):
            colour = QtGui.QColor(out.pixel(x, y))
            out.setPixel(x, y, QtGui.QColor(
                max(0, min(255, colour.red() + delta)),
                max(0, min(255, colour.green() + delta)),
                max(0, min(255, colour.blue() + delta))).rgb())
    return out


def recoloured(image):
    """把 RGB 通道换位 —— 结构一模一样，颜色完全不同。

    **只对彩色图有效**：灰阶图的 R=G=B，换位之后一个像素都不变。
    """
    out = QtGui.QImage(image)
    for y in range(out.height()):
        for x in range(out.width()):
            colour = QtGui.QColor(out.pixel(x, y))
            out.setPixel(x, y, QtGui.QColor(
                colour.green(), colour.blue(), colour.red()).rgb())
    return out


def reencoded(image, quality=70):
    """过一遍 JPEG —— 相当于"同一张图另存了一次"。"""
    buffer = QtCore.QBuffer()          # QBuffer 在 QtCore，不在 QtGui
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, 'JPEG', quality)
    back = QtGui.QImage()
    back.loadFromData(buffer.data(), 'JPEG')
    return back


def is_duplicate(left, right):
    """按服务里的判据算：结构哈希 + 平均色两道都要过。"""
    left_hash = difference_hash(left)
    right_hash = difference_hash(right)
    if left_hash is None or right_hash is None:
        return False
    if hamming_distance(left_hash, right_hash) > MAX_DISTANCE:
        return False
    return color_distance(average_color(left),
                          average_color(right)) <= COLOR_LIMIT


def measure(left, right):
    """把两个距离量出来，方便看"离阈值还有多远"。"""
    left_image, right_image = left(), right()
    return (hamming_distance(difference_hash(left_image),
                             difference_hash(right_image)),
            color_distance(average_color(left_image),
                           average_color(right_image)))


# ── 该算重复的（算不出来就是漏报）────────────────────────────────

POSITIVE_CASES = [
    ('同一张图',
     lambda: make_image(seed=1), lambda: make_image(seed=1)),
    ('另存成 JPEG（有损重编码）',
     lambda: make_image(seed=1), lambda: reencoded(make_image(seed=1))),
    ('缩到一半再放大',
     lambda: make_image(seed=1), lambda: scaled(make_image(seed=1))),
    ('整体提亮 12',
     lambda: make_image(seed=1), lambda: brightened(make_image(seed=1), 12)),
    ('整体压暗 12',
     lambda: make_image(seed=1), lambda: brightened(make_image(seed=1), -12)),
]


@pytest.mark.parametrize('name,left,right',
                         POSITIVE_CASES, ids=[c[0] for c in POSITIVE_CASES])
def test_lookalikes_are_caught(name, left, right):
    """这些应该被判成重复。判不出来就是漏报。"""
    assert is_duplicate(left(), right()) is True, (
        f'{name} 漏报了（距离 {measure(left, right)}，'
        f'阈值 {MAX_DISTANCE}/{COLOR_LIMIT}）')


# ── 不该算重复的（算成重复就是误报）──────────────────────────────

NEGATIVE_CASES = [
    ('色块布局不同',
     lambda: make_image(seed=1), lambda: make_image(seed=2)),
    ('明暗分布完全不同',
     lambda: make_image(seed=1),
     lambda: make_image(paint=blocks([(i * 5) % 16 for i in range(16)],
                                     [(10, 10, 10), (230, 230, 230)]))),
    ('纯红 vs 纯蓝',
     lambda: make_image(paint=lambda x, y: (200, 20, 20)),
     lambda: make_image(paint=lambda x, y: (20, 20, 200))),
    ('结构相同但整体换了色',
     lambda: make_image(paint=blocks(
         [(i * 7 + 3) % 16 for i in range(16)], COLOURS)),
     lambda: recoloured(make_image(paint=blocks(
         [(i * 7 + 3) % 16 for i in range(16)], COLOURS)))),
]


@pytest.mark.parametrize('name,left,right',
                         NEGATIVE_CASES, ids=[c[0] for c in NEGATIVE_CASES])
def test_unrelated_images_are_not_flagged(name, left, right):
    """这些**不该**被判成重复。判成重复就是误报，比漏报更危险 ——

    用户会照着结果删文件，删错了没法撤销。
    """
    assert is_duplicate(left(), right()) is False, (
        f'{name} 误报了（距离 {measure(left, right)}，'
        f'阈值 {MAX_DISTANCE}/{COLOR_LIMIT}）')


# ── 把两边的数报出来 ────────────────────────────────────────────

def test_report_false_positives_and_negatives():
    """把误报/漏报数打出来，调阈值时能一眼看到代价。"""
    false_negatives = [name for name, left, right in POSITIVE_CASES
                       if not is_duplicate(left(), right())]
    false_positives = [name for name, left, right in NEGATIVE_CASES
                       if is_duplicate(left(), right())]

    print(f'\n阈值：hamming <= {MAX_DISTANCE}，色差 <= {COLOR_LIMIT}')
    print(f'  漏报 {len(false_negatives)} / {len(POSITIVE_CASES)}'
          + (f'：{false_negatives}' if false_negatives else ''))
    print(f'  误报 {len(false_positives)} / {len(NEGATIVE_CASES)}'
          + (f'：{false_positives}' if false_positives else ''))

    assert not false_positives, f'不该算成重复的算成了重复：{false_positives}'
    assert not false_negatives, f'该算成重复的没算出来：{false_negatives}'


def test_positive_cases_have_room_below_the_threshold():
    """量一下"该算重复"那组离阈值最近的是谁。

    离得越近，以后稍微调紧一点就会开始漏报。留至少 1 格余量。
    """
    worst = None
    for name, left, right in POSITIVE_CASES:
        distance, colour = measure(left, right)
        if worst is None or distance > worst[1]:
            worst = (name, distance, colour)

    name, distance, colour = worst
    print(f'\n该判重复的里面离阈值最近的是「{name}」：'
          f'距离 {distance}（阈值 {MAX_DISTANCE}），色差 {colour}')
    assert distance < MAX_DISTANCE, (
        f'「{name}」的距离是 {distance}，正好卡在阈值上，没有余量')
    assert colour < COLOR_LIMIT, (
        f'「{name}」的色差是 {colour}，正好卡在阈值上')


def test_colour_swap_is_held_back_by_at_least_one_judge():
    """换色那条：结构或颜色，至少有一条判据把它挡住。

    两种判据的分工是这样的 ——

      * **结构哈希**管"长得像不像"（明暗分布、梯度），对缩放和重编码宽容
      * **平均色**管"颜色像不像"，挡住那些结构像但配色完全不同的图

    「结构相同但整体换了色」这条**两条都超限**：换 RGB 通道顺带改了亮度
    (190,60,40) 的亮度约 95，(60,40,190) 约 61，而 dHash 看的正是亮度
    梯度。这比只有一条判据挡着更稳，所以不去换一个"只动颜色不动亮度"的
    变换来迁就断言 —— 那成了为测试挑样例。
    """
    layout = blocks([(i * 7 + 3) % 16 for i in range(16)], COLOURS)
    plain = make_image(paint=layout)
    shifted = recoloured(plain)

    structure = hamming_distance(difference_hash(plain),
                                 difference_hash(shifted))
    colour = color_distance(average_color(plain), average_color(shifted))
    print(f'\n换色那条：结构距离 {structure}（阈值 {MAX_DISTANCE}），'
          f'色差 {colour}（阈值 {COLOR_LIMIT}）')

    assert structure > MAX_DISTANCE or colour > COLOR_LIMIT, (
        f'两条判据都没挡住：结构 {structure}，色差 {colour}')


def test_colour_distance_can_separate_what_structure_cannot():
    """单独验一下色差这道真的有用。

    拿两张**亮度分布完全一样、只有颜色不同**的图 —— 结构哈希给 0 距离，
    全靠色差。这也说明为什么两道判据缺一不可。
    """
    layout = blocks([(i * 7 + 3) % 16 for i in range(16)],
                    [(90, 90, 90), (170, 170, 170)])
    # 同样的明暗，但一个是黄调一个是蓝调。用两套亮度相同的色：
    #   灰 (90,90,90)      vs 蓝 (60,60,150)   亮度相近
    #   灰 (170,170,170)   vs 黄 (180,175,150) 亮度相近
    warm = make_image(paint=blocks([(i * 7 + 3) % 16 for i in range(16)],
                                   [(120, 100, 40), (200, 180, 120)]))
    cool = make_image(paint=blocks([(i * 7 + 3) % 16 for i in range(16)],
                                   [(40, 100, 120), (120, 180, 200)]))
    structure = hamming_distance(difference_hash(warm),
                                 difference_hash(cool))
    colour = color_distance(average_color(warm), average_color(cool))
    print(f'\n同明暗不同配色：结构距离 {structure}，色差 {colour}')
    assert colour > COLOR_LIMIT, (
        f'这套配色本来就该被色差挡住，实得 {colour}')


def test_structure_distance_is_what_separates_layout_changes():
    """反过来，「色块布局不同」那条靠的是结构哈希。

    平均色可能接近（都是灰阶），是布局把它们分开的。
    """
    distance, _colour = measure(lambda: make_image(seed=1),
                                lambda: make_image(seed=2))
    assert distance > MAX_DISTANCE, (
        f'布局不同应该体现在结构距离上，实得 {distance}')
