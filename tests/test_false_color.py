"""T8：假色显示。

把明暗映射成色带，一眼看出哪里过曝、哪里压死。直方图只能告诉你
*有多少*像素被裁掉，这张图告诉你它们*在哪儿* —— 这是曝光和通道控件
都做不到的那件事。
"""
import pytest
from PyQt6 import QtGui

from prism.fileio.image import FALSE_COLOR_BANDS, false_color_preview


def _image(pixels, width=None):
    """用灰度像素造图：pixels 是 [[亮度, ...], ...]，0-255。"""
    height = len(pixels)
    width = width or len(pixels[0])
    image = QtGui.QImage(width, height, QtGui.QImage.Format.Format_RGB32)
    for y, row in enumerate(pixels):
        for x, value in enumerate(row):
            image.setPixelColor(x, y, QtGui.QColor(value, value, value))
    return image


def _bands():
    return [colour for _, colour in FALSE_COLOR_BANDS]


def test_a_clipped_pixel_comes_out_red(qapp):
    out = false_color_preview(_image([[255]]))
    assert out.pixelColor(0, 0).getRgb()[:3] == FALSE_COLOR_BANDS[-1][1]


def test_a_crushed_pixel_comes_out_blue(qapp):
    out = false_color_preview(_image([[0]]))
    assert out.pixelColor(0, 0).getRgb()[:3] == FALSE_COLOR_BANDS[0][1]


def test_mid_grey_does_not_land_on_either_extreme(qapp):
    out = false_color_preview(_image([[128]]))
    colour = out.pixelColor(0, 0).getRgb()[:3]
    bands = _bands()
    assert colour in bands
    assert colour not in (bands[0], bands[-1]), \
        '128 既不是死黑也不是过曝'


def test_the_output_is_opaque(qapp):
    out = false_color_preview(_image([[100]]))
    assert out.pixelColor(0, 0).alpha() == 255


def test_brightness_walks_up_the_ramp_monotonically(qapp):
    """从黑到白的渐变要依次经过各档，顺序不能乱。"""
    ramp = [[value] for value in range(0, 256, 8)]
    out = false_color_preview(_image(ramp, width=1))

    seen = []
    for y in range(out.height()):
        colour = out.pixelColor(0, y).getRgb()[:3]
        if not seen or seen[-1] != colour:
            seen.append(colour)

    order = _bands()
    positions = []
    for colour in seen:
        assert colour in order, f'出现了色带以外的颜色 {colour}'
        positions.append(order.index(colour))

    assert positions == sorted(positions), f'色带顺序乱了: {positions}'
    assert len(set(positions)) >= 4, f'只跨过 {len(set(positions))} 档，太少'


def test_exposure_shifts_which_band_a_pixel_lands_in(qapp):
    """曝光滑块必须影响假色，否则覆盖层和画面对不上。"""
    image = _image([[64]])
    plain = false_color_preview(image, exposure=0.0).pixelColor(0, 0)
    brighter = false_color_preview(image, exposure=2.0).pixelColor(0, 0)

    order = _bands()
    plain_rgb = plain.getRgb()[:3]
    brighter_rgb = brighter.getRgb()[:3]
    assert plain_rgb != brighter_rgb, '加两档曝光后 64 该落到更亮的档'
    assert order.index(brighter_rgb) > order.index(plain_rgb)


def test_an_empty_image_does_not_explode(qapp):
    assert false_color_preview(QtGui.QImage()).isNull()


def test_a_transparent_image_becomes_opaque(qapp):
    """假色是看曝光的，半透明会把颜色混掉。"""
    image = QtGui.QImage(4, 4, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtGui.QColor(255, 255, 255, 0))
    out = false_color_preview(image)
    assert out.pixelColor(0, 0).alpha() == 255


def test_the_ramp_covers_black_to_white_without_gaps(qapp):
    """色带的边界要连续：0 到 1 之间不能有落不进的缝。"""
    bounds = [upper for upper, _ in FALSE_COLOR_BANDS]
    assert bounds == sorted(bounds), '边界必须递增'
    assert bounds[-1] > 1.0, '最后一档要能吃下 1.0，否则纯白无处可去'
    assert bounds[0] > 0.0, '第一档要能吃下 0.0'


def test_every_band_has_a_distinct_colour(qapp):
    colours = _bands()
    assert len(set(colours)) == len(colours), '每档颜色要不同，否则看不出分界'
