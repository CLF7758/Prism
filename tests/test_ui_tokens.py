"""B1：`prism/ui_tokens.py` 的 Token 与规范 §4.2 / §5.1 / §5.2 / §6 一致。

这里的期望值是**从规范原文抄下来的**，不是从 ui_tokens.py 反抄的 ——
所以改了 Token 值而没同步规范（或反过来）会被抓出来。
"""
import pytest

from prism import ui_tokens as t

#: §5.1 默认深色主题，逐行照抄（顺序也保持规范里的顺序）。
SPEC_DARK = {
    'app-bg': '#191A1D',
    'panel-bg': '#222327',
    'toolbar-bg': '#25262B',
    'input-bg': '#2C2E34',
    'hover-bg': '#30333A',
    'pressed-bg': '#383C45',
    'selected-bg': '#263E5A',
    'inactive-selected-bg': '#343942',
    'overlay-bg': '#2A2C32',
    'border-subtle': '#363940',
    'border-strong': '#565D69',
    'text-primary': '#E7E9ED',
    'text-secondary': '#AAB0BB',
    'text-disabled': '#727883',
    'accent': '#79AEFF',
    'primary-fill': '#285DA8',
    'primary-hover': '#326CBD',
    'primary-pressed': '#214F90',
    'primary-text': '#FFFFFF',
    'success': '#79CCA6',
    'warning': '#E7BA70',
    'danger': '#F08A94',
    'scrim': '#00000066',
    'paper-bg': '#FAFAF7',
    'paper-text': '#24272C',
    'paper-muted': '#626971',
}

#: §5.2 浅色对应表。纸张四个 + primary 四个与深色一致，规范原文如此。
SPEC_LIGHT = {
    'app-bg': '#F1F2F5',
    'panel-bg': '#F7F8FA',
    'toolbar-bg': '#FAFBFC',
    'input-bg': '#FFFFFF',
    'hover-bg': '#E9EDF3',
    'pressed-bg': '#DDE4EE',
    'selected-bg': '#DDEBFF',
    'inactive-selected-bg': '#E4E7ED',
    'overlay-bg': '#FFFFFF',
    'border-subtle': '#D7DCE4',
    'border-strong': '#9CA6B6',
    'text-primary': '#242831',
    'text-secondary': '#5F6878',
    'text-disabled': '#9299A5',
    'accent': '#285DA8',
    'primary-fill': '#285DA8',
    'primary-hover': '#326CBD',
    'primary-pressed': '#214F90',
    'primary-text': '#FFFFFF',
    'success': '#23774F',
    'warning': '#89601D',
    'danger': '#B93648',
    'scrim': '#00000066',
    'paper-bg': '#FAFAF7',
    'paper-text': '#24272C',
    'paper-muted': '#626971',
}

#: §5 文件夹可选色的八种。
SPEC_FOLDER_COLOURS = {
    'gray': '#9EA5B1',
    'blue': '#79AEFF',
    'cyan': '#70C8D0',
    'green': '#87C99B',
    'yellow': '#D9BF76',
    'orange': '#E2A173',
    'red': '#E58E96',
    'purple': '#B49CE8',
}


# ── 色彩 Token ────────────────────────────────────────────────

def test_dark_tokens_match_the_spec_exactly():
    assert t.COLOURS_DARK == SPEC_DARK


def test_light_tokens_match_the_spec_exactly():
    assert t.COLOURS_LIGHT == SPEC_LIGHT


def test_light_theme_defines_every_token_the_dark_one_does():
    """缺一个键，浅色主题下那个控件就会掉回硬编码颜色。"""
    assert set(t.COLOURS_LIGHT) == set(t.COLOURS_DARK)


def test_folder_colours_match_the_spec():
    assert t.FOLDER_COLOURS == SPEC_FOLDER_COLOURS


def test_folder_colours_cover_the_eight_named_colors():
    assert len(t.FOLDER_COLOURS) == 8


def test_page_type_icons_have_a_colour():
    for section in ('canvas', 'mindmap', 'document', 'tag'):
        assert section in t.SECTION_ICON_COLOURS


def test_no_token_name_was_invented():
    """§5.1 的 26 个键一个不多一个不少。"""
    assert len(t.COLOURS_DARK) == 26
    assert set(t.COLOURS_DARK) == set(SPEC_DARK)


@pytest.mark.parametrize('mode', ['dark', 'light'])
def test_every_colour_parses_as_a_real_colour(mode):
    from PyQt6 import QtGui
    for name, value in t.active_tokens(mode).items():
        colour = QtGui.QColor(value)
        assert colour.isValid(), '%s = %r 不是合法颜色' % (name, value)


# ── 文字角色 ──────────────────────────────────────────────────

def test_fallback_role_exists():
    assert t.TEXT_ROLE_FALLBACK in t.TEXT_ROLES


def test_unknown_role_falls_back_to_body():
    """§4.2：未列出的新增普通 UI 文字继承 body 13/20/400。"""
    assert t.text_style('nope-not-a-role') is t.TEXT_ROLES['body']
    assert t.text_style('body').size == 13
    assert t.text_style('body').line_height == 20
    assert t.text_style('body').weight == 400


def test_every_role_points_at_a_token_that_exists():
    for role, style in t.TEXT_ROLES.items():
        assert style.colour in t.COLOURS_DARK, (
            '角色 %s 指向不存在的 Token %s' % (role, style.colour))


def test_every_light_theme_role_also_resolves():
    for style in t.TEXT_ROLES.values():
        assert style.colour in t.COLOURS_LIGHT


@pytest.mark.parametrize('role,size,line_height,weight', [
    ('titlebar', 12, 18, 400),
    ('project-name', 14, 20, 600),
    ('tree-section-title', 12, 18, 600),
    ('tree-node', 13, 20, 400),
    ('inspector-title', 14, 20, 600),
    ('inspector-section-title', 12, 18, 600),
    ('tag-chip', 12, 18, 400),
    ('asset-card-meta', 11, 16, 400),
    ('empty-state-title', 18, 26, 600),
    ('dialog-title', 16, 24, 600),
    ('status-bar', 12, 18, 400),
    ('kbd', 12, 18, 400),
])
def test_key_roles_match_the_spec_table(role, size, line_height, weight):
    style = t.text_style(role)
    assert (style.size, style.line_height, style.weight) == (
        size, line_height, weight)


def test_no_role_uses_a_range():
    """§0：默认值不可自行改成范围值 —— 每个角色都得有确定值。"""
    for role, style in t.TEXT_ROLES.items():
        assert isinstance(style.size, int), role
        assert isinstance(style.line_height, int), role
        assert style.weight in (400, 500, 600), role


def test_style_for_resolves_the_colour_to_hex():
    resolved = t.style_for('status-bar')
    assert resolved['colour'] == SPEC_DARK['text-secondary']
    assert resolved['colour_token'] == 'text-secondary'
    assert resolved['size'] == 12


def test_error_hint_uses_the_danger_token():
    assert t.text_style('error-hint').colour == 'danger'


# ── 间距与组件 ────────────────────────────────────────────────

def test_spacing_scale_is_the_spec_one():
    assert sorted(t.SPACING.values()) == [4, 8, 12, 16, 24, 32]


def test_default_paddings_match_the_spec():
    assert t.GAP_DEFAULT == 8
    assert t.PANEL_PADDING == 16
    assert t.TREE_PADDING == 8


def test_component_geometry_matches_the_spec():
    assert t.COMPONENTS['icon-size'] == 16
    assert t.COMPONENTS['icon-main-tool'] == 20
    assert t.COMPONENTS['icon-tree-arrow'] == 12
    assert t.COMPONENTS['icon-button'] == 32
    assert t.COMPONENTS['input-height'] == 32
    assert t.COMPONENTS['tree-row-height'] == 32
    assert t.COMPONENTS['menu-item-height'] == 30
    assert t.COMPONENTS['chip-height'] == 24
    assert t.COMPONENTS['segmented-height'] == 28
    assert t.COMPONENTS['tooltip-delay-ms'] == 600
    assert t.COMPONENTS['toast-min-height'] == 40
    assert t.COMPONENTS['dialog-width'] == 480
    assert t.COMPONENTS['dialog-width-large'] == 640


def test_layout_geometry_matches_the_spec():
    assert t.LAYOUT['titlebar-height'] == 36
    assert t.LAYOUT['header-height'] == 48
    assert t.LAYOUT['left-panel-width'] == 248
    assert t.LAYOUT['right-panel-width'] == 304
    assert t.LAYOUT['statusbar-height'] == 24
    assert t.LAYOUT['centre-min-width'] == 520
    assert t.LAYOUT['window-default'] == (1440, 900)
    assert t.LAYOUT['window-min'] == (900, 600)


def test_draw_bar_geometry_matches_the_spec():
    """§10.1 的悬浮条几何 —— E5 就靠这几个数校准。"""
    assert t.DRAW_BAR['width'] == 440
    assert t.DRAW_BAR['height'] == 56
    assert t.DRAW_BAR['radius'] == 10
    assert t.DRAW_BAR['button'] == 40
    assert t.DRAW_BAR['icon'] == 24
    assert t.DRAW_BAR['gap'] == 4
    assert t.DRAW_BAR['top-margin'] == 12


def test_font_fallbacks_start_with_yahei():
    """§4.1：整体优先 Microsoft YaHei UI。"""
    assert t.UI_FONT_FAMILIES[0] == 'Microsoft YaHei UI'
    assert 'Segoe UI' in t.UI_FONT_FAMILIES
    assert 'Noto Sans CJK SC' in t.UI_FONT_FAMILIES
    assert t.MONO_FONT_FAMILIES[0] == 'Consolas'


def test_document_body_is_twelve_pt_sixteen_px():
    """§12.2：正文 12 pt = 16 CSS px，行高 1.75。"""
    size, line_height, weight, _ = t.DOCUMENT_TEXT_ROLES['body']
    assert size == 16
    assert line_height == 1.75
    assert weight == 400


def test_mindmap_node_roles_match_the_spec():
    assert t.MINDMAP_TEXT_ROLES['root'] == (22, 30, 600)
    assert t.MINDMAP_TEXT_ROLES['level-1'] == (16, 24, 500)
    assert t.MINDMAP_TEXT_ROLES['node'] == (14, 22, 400)


def test_mindmap_node_fills_match_the_spec():
    assert t.MINDMAP_NODES['root']['fill'] == '#285DA8'
    assert t.MINDMAP_NODES['level-1']['fill'] == '#2D3645'
    assert t.MINDMAP_NODES['node']['fill'] == '#282C34'
    assert t.MINDMAP_NODES['edge-colour'] == '#737F92'


# ── 给 QSS / WebEngine 的导出 ─────────────────────────────────

@pytest.mark.parametrize('mode', ['dark', 'light'])
def test_qss_variables_carry_every_colour_token(mode):
    variables = t.qss_variables(mode)
    for name, value in t.active_tokens(mode).items():
        assert variables[name] == value


def test_qss_variables_carry_fonts_spacing_and_component_sizes():
    variables = t.qss_variables()
    assert variables['font-ui'].startswith('Microsoft YaHei UI')
    assert variables['space-md'] == '12'
    assert variables['cmp-input-height'] == '32'
    assert variables['layout-left-panel-width'] == '248'


def test_missing_placeholders_flags_a_typo():
    qss = 'color: {text-primry}; background: {app-bg};'
    assert t.missing_placeholders(qss) == ['text-primry']


def test_missing_placeholders_is_empty_for_a_correct_template():
    qss = 'color: {text-primary}; background: {app-bg}; padding: {space-lg};'
    assert t.missing_placeholders(qss) == []


def test_missing_placeholders_accepts_light_mode():
    assert t.missing_placeholders('color: {accent};', mode='light') == []


def test_css_variables_emit_a_root_block():
    css = t.css_variables('dark')
    assert css.startswith(':root {')
    assert css.rstrip().endswith('}')
    assert '--app-bg: #191A1D;' in css
    assert '--space-md: 12px;' in css


def test_tokens_returned_are_copies():
    """拿到手就改，不该污染模块常量。"""
    tokens = t.tokens_dark()
    tokens['app-bg'] = '#000000'
    assert t.COLOURS_DARK['app-bg'] == '#191A1D'
