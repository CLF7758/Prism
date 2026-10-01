"""Prism 的设计 Token —— **唯一来源**。

依据 `桌面/Prism-全套UI与交互开发规范-v1.md`：

* §4.1 字体确定值
* §4.2 全量文字角色表（32 个角色）
* §5.1 默认深色主题 / §5.2 浅色主题对应表 / §5 文件夹可选色
* §6 共享组件精确规范
* §12.2 文档排版 / §4.3 脑图文字

**为什么要有这个模块**：规范 §15.3 要求"主题 Token 统一源导出为 Qt 样式和
Web CSS 变量"，§6 要求"共享 Token 和组件必须复用，不能为每个页面复制一套
近似样式"。所以颜色、字号、行高、间距**只在这里写一次**，别处一律引用。

**Token 名不许自己发明**（规范写 MUST）—— `COLOURS_DARK` / `COLOURS_LIGHT`
的键和 §5.1 表格**逐行对应**，改名前先改规范。

用法::

    from prism.ui_tokens import active_tokens, text_style

    tokens = active_tokens()            # {'app-bg': '#191A1D', ...}
    css = tokens['app-bg']
    style = text_style('body')          # {'size': 13, 'line_height': 20, ...}

给 QSS 用（QSS 没有变量，所以由 ``qss_variables()`` 提供 `{token}` 占位替换）::

    background: {app-bg};
    color: {text-primary};

WebEngine 侧用 ``css_variables()`` 生成 `:root { --app-bg: #191A1D; ... }`。
"""
import dataclasses
import re

# ── §4.1 字体 ────────────────────────────────────────────────────────
#
# Windows UI 中文：Microsoft YaHei UI；回退 Segoe UI，再回退
# Noto Sans CJK SC / sans-serif。禁止普通 UI 用宋体、装饰字体、等宽字体
# 或 emoji 充当图标。

UI_FONT_FAMILIES = (
    'Microsoft YaHei UI',
    'Segoe UI',
    'Noto Sans CJK SC',
    'sans-serif',
)

#: macOS 用系统 UI 字体，中文 PingFang SC。
UI_FONT_FAMILIES_MAC = (
    'PingFang SC',
    'Helvetica Neue',
    'sans-serif',
)

#: 快捷键 kbd / 技术路径用。规范 §4.2 末几行。
MONO_FONT_FAMILIES = (
    'Consolas',
    'monospace',
)

#: 文档正文与脑图正文用（§4.3）。
DOCUMENT_FONT_FAMILIES = (
    'Microsoft YaHei',
    'sans-serif',
)


# ── §6 间距基准 ──────────────────────────────────────────────────────
#
# 间距基准：4/8/12/16/24/32 px。普通布局默认 gap 8，面板内边距 16，
# 左右树内边距 8。禁止任意加大控件制造空白。

SPACING = {
    'xs': 4,
    'sm': 8,
    'md': 12,
    'lg': 16,
    'xl': 24,
    'xxl': 32,
}

GAP_DEFAULT = 8
PANEL_PADDING = 16
TREE_PADDING = 8


# ── §5.1 默认深色主题 ────────────────────────────────────────────────

COLOURS_DARK = {
    'app-bg': '#191A1D',                # 应用底色、画布工作台
    'panel-bg': '#222327',              # 左右栏
    'toolbar-bg': '#25262B',            # 头部、工具栏
    'input-bg': '#2C2E34',              # 输入框、内嵌控件
    'hover-bg': '#30333A',              # 行/按钮悬停
    'pressed-bg': '#383C45',            # 按下状态
    'selected-bg': '#263E5A',           # 导航当前项、选中按钮底色
    'inactive-selected-bg': '#343942',  # 失去窗口激活的选择
    'overlay-bg': '#2A2C32',            # 菜单、浮层、弹窗
    'border-subtle': '#363940',         # 普通分隔线
    'border-strong': '#565D69',         # 控件边界、明显区隔
    'text-primary': '#E7E9ED',          # 主要文字
    'text-secondary': '#AAB0BB',        # 标签、辅助正文
    'text-disabled': '#727883',         # 真正禁用的控件
    'accent': '#79AEFF',                # 焦点线、选中描边、链接
    'primary-fill': '#285DA8',          # 主按钮底色
    'primary-hover': '#326CBD',         # 主按钮悬停
    'primary-pressed': '#214F90',       # 主按钮按下
    'primary-text': '#FFFFFF',          # 主按钮文字
    'success': '#79CCA6',               # 成功文字/图标
    'warning': '#E7BA70',               # 警告文字/图标
    'danger': '#F08A94',                # 错误/删除文字
    'scrim': '#00000066',               # 覆盖抽屉和模态遮罩
    'paper-bg': '#FAFAF7',              # 文档纸张
    'paper-text': '#24272C',            # 文档正文
    'paper-muted': '#626971',           # 文档辅助信息
}


# ── §5.2 浅色主题对应表 ──────────────────────────────────────────────
#
# 保留可切换浅色和跟随系统模式；默认深色。浅色不能直接反相图片或文档。
# 纸张 Token 不变；primary-* 与深色一致。

COLOURS_LIGHT = {
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


# ── §5 文件夹可选色 ──────────────────────────────────────────────────
#
# 仅染文件夹图标和小标识，不把整行涂成分类色。画布/脑图/文档图标默认
# 蓝/紫/青，但普通行文字统一主文字色。

FOLDER_COLOURS = {
    'gray': '#9EA5B1',
    'blue': '#79AEFF',
    'cyan': '#70C8D0',
    'green': '#87C99B',
    'yellow': '#D9BF76',
    'orange': '#E2A173',
    'red': '#E58E96',
    'purple': '#B49CE8',
}

#: 页面类型图标默认色（§5 末段）。
SECTION_ICON_COLOURS = {
    'canvas': '#79AEFF',    # 蓝
    'mindmap': '#B49CE8',   # 紫
    'document': '#70C8D0',  # 青
    'tag': '#9EA5B1',       # 灰
}

#: 文档纸张上的可选文件夹色不适用；文档内链接色见 DOCUMENT_TEXT。
DOCUMENT_TEXT = {
    'link': '#285DA8',
    'quote-border': '#B3C2D6',
    'code-bg': '#ECEEF2',
    'table-border': '#D9DCE2',
    'table-header-bg': '#EEF0F3',
}


# ── §4.2 全量文字角色表 ──────────────────────────────────────────────
#
# 100% 界面缩放下的默认值。未列出的新增普通 UI 文字继承 body 13/20/400，
# 不允许各页自行发明字号。行高为文字框高度，控件整体高度见 COMPONENTS。
#
# 表里写成 "12/13" 这类二选一的，这里拆成两个独立角色，各自有确定值 ——
# 规范 §0 要求"默认值不可自行改成范围值"。


@dataclasses.dataclass(frozen=True)
class TextStyle:
    """一个文字角色的确定值。"""

    size: int           # px
    line_height: int    # px
    weight: int         # 400 / 500 / 600
    colour: str         # COLOURS_* 里的 Token 名

    def font_px(self):
        return self.size

    def qss(self, colour_value):
        """给 QSS 用的一小段声明。`colour_value` 由调用方从 Token 表取。"""
        return 'font-size: %dpx; font-weight: %d; color: %s;' % (
            self.size, self.weight, colour_value)


TEXT_ROLES = {
    # 角色名(规范原文顺序)         字号 行高 字重 颜色 Token
    'titlebar': TextStyle(12, 18, 400, 'text-secondary'),
    'project-name': TextStyle(14, 20, 600, 'text-primary'),
    'tree-section-title': TextStyle(12, 18, 600, 'text-secondary'),
    'tree-node': TextStyle(13, 20, 400, 'text-primary'),
    'tree-node-current': TextStyle(13, 20, 500, 'text-primary'),
    'tree-aux': TextStyle(12, 18, 400, 'text-secondary'),
    'breadcrumb': TextStyle(13, 20, 400, 'text-secondary'),
    'breadcrumb-current': TextStyle(13, 20, 600, 'text-primary'),
    'toolbar-button': TextStyle(13, 20, 400, 'text-primary'),
    'input': TextStyle(13, 20, 400, 'text-primary'),
    'input-placeholder': TextStyle(13, 20, 400, 'text-secondary'),
    'menu-item': TextStyle(13, 20, 400, 'text-primary'),
    'menu-shortcut': TextStyle(12, 18, 400, 'text-secondary'),
    'inspector-title': TextStyle(14, 20, 600, 'text-primary'),
    'inspector-section-title': TextStyle(12, 18, 600, 'text-secondary'),
    'inspector-field-label': TextStyle(12, 18, 400, 'text-secondary'),
    'inspector-field-value': TextStyle(13, 20, 400, 'text-primary'),
    'tag-chip': TextStyle(12, 18, 400, 'text-primary'),
    'asset-card-name': TextStyle(12, 18, 400, 'text-primary'),
    'asset-card-meta': TextStyle(11, 16, 400, 'text-secondary'),
    'asset-list-row': TextStyle(13, 20, 400, 'text-primary'),
    'asset-list-header': TextStyle(12, 18, 600, 'text-secondary'),
    'empty-state-title': TextStyle(18, 26, 600, 'text-primary'),
    'empty-state-body': TextStyle(13, 20, 400, 'text-secondary'),
    'dialog-title': TextStyle(16, 24, 600, 'text-primary'),
    'dialog-body': TextStyle(13, 20, 400, 'text-primary'),
    'tooltip': TextStyle(12, 18, 400, 'text-primary'),
    'toast': TextStyle(13, 20, 400, 'text-primary'),
    'status-bar': TextStyle(12, 18, 400, 'text-secondary'),
    'error-hint': TextStyle(12, 18, 400, 'danger'),
    'kbd': TextStyle(12, 18, 400, 'text-secondary'),
    # 规范 §4.2 没有单独列 body，但末段写明"未列出的新增普通 UI 文字
    # 继承 body 13/20/400"，所以它是显式存在的兜底角色。
    'body': TextStyle(13, 20, 400, 'text-primary'),
}

#: 没写明角色时的兜底（§4.2 末段）。
TEXT_ROLE_FALLBACK = 'body'


# ── §6 共享组件几何 ──────────────────────────────────────────────────

COMPONENTS = {
    # 图标
    'icon-canvas': 24,          # 24 viewBox，统一 1.75 描边
    'icon-size': 16,            # 16×16
    'icon-main-tool': 20,       # 主工具 20×20
    'icon-tree-arrow': 12,      # 树展开箭头 12×12
    'icon-stroke': 1.75,
    # 图标按钮
    'icon-button': 32,          # 32×32 热区
    'icon-button-radius': 6,
    'icon-button-tree': 24,     # 树行内按钮 24×24
    # 文字按钮
    'button-height': 32,
    'button-padding-x': 12,
    'button-radius': 6,
    'button-min-width': 64,
    # 输入框
    'input-height': 32,
    'input-padding-x': 10,
    'input-border': 1,
    'input-radius': 6,
    'input-focus-border': 2,    # 焦点 2 px accent 外框
    # 文本域
    'textarea-min-height': 80,
    'textarea-padding': 10,
    'textarea-line-height': 20,
    'textarea-max-height': 160,  # 超过后内部滚动
    # 树
    'tree-row-height': 32,
    'tree-row-padding-x': 8,
    'tree-row-radius': 5,
    'tree-section-head-height': 32,
    'tree-section-plus': 24,
    'tree-indent': 16,          # §7.3 层级缩进 depth×16
    'tree-arrow-slot': 16,
    'tree-icon-slot': 20,
    'tree-count-slot': 32,
    'tree-more-slot': 24,
    'tree-current-marker': 2,   # 左侧 2 px accent 标记
    # Inspector
    'inspector-width': 304,
    'inspector-header-height': 48,
    'inspector-section-head-height': 32,
    # Chip
    'chip-height': 24,
    'chip-radius': 4,
    'chip-padding-x': 8,
    'chip-close': 12,
    'chip-gap': 4,
    # 菜单
    'menu-min-width': 224,
    'menu-max-width': 320,
    'menu-padding': 6,
    'menu-radius': 8,
    'menu-item-height': 30,
    'menu-separator-height': 9,
    'menu-separator-margin': 4,
    # Tooltip / 弹窗 / Toast
    'tooltip-max-width': 280,
    'tooltip-padding': (8, 10),
    'tooltip-radius': 6,
    'tooltip-delay-ms': 600,
    'dialog-width': 480,
    'dialog-width-large': 640,
    'dialog-padding': 24,
    'dialog-radius': 12,
    'dialog-button-gap': 8,
    'toast-min-width': 240,
    'toast-max-width': 440,
    'toast-min-height': 40,
    'toast-padding': 12,
    'toast-radius': 8,
    'toast-bottom-margin': 16,
    # 滚动条
    'scrollbar-track': 8,
    'scrollbar-thumb': 4,
    'scrollbar-thumb-hover': 6,
    'scrollbar-thumb-min': 32,
    # 分段按钮
    'segmented-height': 28,
    'segmented-radius': 6,
    'segmented-padding': 2,
    # 空状态
    'empty-icon': 48,
    'empty-icon-title-gap': 16,
    'empty-title-body-gap': 8,
    'empty-body-button-gap': 16,
    # 浮层阴影与动效（§6）
    'shadow-overlay': '0 8 24 rgba(0,0,0,0.28)',
    'duration-hover-ms': 100,
    'duration-drawer-ms': 160,
    'duration-collapse-ms': 120,
}


# ── §3.1 窗口布局几何 ────────────────────────────────────────────────

LAYOUT = {
    'titlebar-height': 36,
    'header-height': 48,
    'left-panel-width': 248,
    'left-panel-min': 208,
    'left-panel-max': 360,
    'right-panel-width': 304,
    'right-panel-min': 272,
    'right-panel-max': 400,
    'centre-min-width': 520,        # 双侧栏模式
    'statusbar-height': 24,
    'splitter-width': 1,
    'splitter-hotzone': 6,
    'page-toolbar-height': 40,
    'filter-bar-height': 36,
    'window-default': (1440, 900),
    'window-min': (900, 600),
}


# ── §10.1 绘制悬浮条几何 ─────────────────────────────────────────────

DRAW_BAR = {
    'width': 440,
    'height': 56,
    'radius': 10,
    'button': 40,
    'icon': 24,
    'gap': 4,
    'padding-x': 8,
    'separator-height': 28,
    'separator-width': 1,
    'separator-margin-x': 4,
    'top-margin': 12,           # 距中栏顶部工具区下沿
    'colour-bar-height': 3,
}


# ── §12.2 文档排版 / §4.3 脑图文字 ───────────────────────────────────
#
# 文档字号用 pt 概念，界面 px 与文档 pt 必须分开（§4.1）。
# 这里记的是 CSS px 值，CSS 里 pt→px 由编辑器自己换算。

DOCUMENT_TEXT_ROLES = {
    'body': (16, 1.75, 400, None),          # 12 pt = 16 CSS px
    'doc-title': (28, 1.35, 600, None),
    'h1': (24, 1.4, 600, None),
    'h2': (20, 1.45, 600, None),
    'h3': (18, 1.5, 600, None),
    'h4': (16, 1.6, 600, None),
    'h5': (16, 1.6, 600, None),
    'h6': (16, 1.6, 600, None),
    'quote': (16, 1.75, 400, 'paper-muted'),
    'inline-code': (14, None, 400, None),
    'code-block': (14, 1.6, 400, None),
    'table': (14, 1.6, 400, None),
    'caption': (12, 1.5, 400, 'paper-muted'),
}

#: 文档字号下拉（pt）与手动范围（§12.2）。
DOCUMENT_PT_PRESETS = (8, 9, 10, 11, 12, 14, 16, 18, 20, 24, 28, 36, 48, 72)
DOCUMENT_PT_RANGE = (6, 96)

#: 文档纸张（§12.1）。
DOCUMENT_PAPER = {
    'width': 794,               # CSS px，100% 时近 A4 宽
    'padding-x': 64,
    'padding-y': 56,
    'min-content-width': 320,
    'surround-margin': 24,
    'shadow': '0 4 16 #00000024',
}

MINDMAP_TEXT_ROLES = {
    'root': (22, 30, 600),
    'level-1': (16, 24, 500),
    'node': (14, 22, 400),
    'note-preview': (12, 18, 400),
}

#: 脑图节点几何（§11.2）。
MINDMAP_NODES = {
    'root': {'fill': '#285DA8', 'text': '#FFFFFF', 'radius': 10,
             'padding': (16, 24), 'min-size': (160, 62)},
    'level-1': {'fill': '#2D3645', 'text': 'text-primary', 'radius': 8,
                'padding': (10, 16), 'min-size': (120, 44)},
    'node': {'fill': '#282C34', 'text': 'text-primary', 'radius': 6,
             'padding': (8, 12), 'min-size': (88, 38)},
    'edge-width': 2,
    'edge-colour': '#737F92',
    'parent-child-gap': 64,
    'sibling-gap': 20,
    'selection-outline': 2,
    'selection-outline-offset': 3,
    'collapse-button': 16,
    'collapse-hit': 24,
    'max-text-width': 240,
    'max-text-width-root': 360,
}


# ── 导出接口 ─────────────────────────────────────────────────────────

def tokens_dark():
    """深色主题的全部色彩 Token（副本，改它不影响模块常量）。"""
    return dict(COLOURS_DARK)


def tokens_light():
    """浅色主题的全部色彩 Token。"""
    return dict(COLOURS_LIGHT)


def active_tokens(mode='dark'):
    """按模式取色彩 Token。"""
    return tokens_light() if mode == 'light' else tokens_dark()


def text_style(role):
    """取一个文字角色；没写明的角色回落到 body（§4.2 末段）。"""
    return TEXT_ROLES.get(role, TEXT_ROLES[TEXT_ROLE_FALLBACK])


def qss_variables(mode='dark'):
    """QSS 用的 `{token}` 占位替换表。

    QSS 没有变量，所以 `theme.qss` 里写 `background: {app-bg};`，
    由生成器用这张表替换。校验时用 ``missing_placeholders()``。
    """
    variables = dict(active_tokens(mode))
    # 非色彩的也一起给，省得 QSS 里硬编码数字
    variables['font-ui'] = ', '.join(UI_FONT_FAMILIES)
    variables['font-mono'] = ', '.join(MONO_FONT_FAMILIES)
    for name, value in SPACING.items():
        variables['space-%s' % name] = str(value)
    for name, value in COMPONENTS.items():
        if isinstance(value, (int, str)):
            variables['cmp-%s' % name] = str(value)
    for name, value in LAYOUT.items():
        if isinstance(value, int):
            variables['layout-%s' % name] = str(value)
    return variables


PLACEHOLDER = re.compile(r'\{([a-z0-9][a-z0-9-]*)\}')


def missing_placeholders(qss_text, mode='dark'):
    """QSS 模板里用到、但 Token 表里没有的占位 —— 生成前必须为空。

    规范 §0：不能用"看起来实现了"的假入口；一个拼错的 Token 名会让
    QSS 静默失效，所以这里显式暴露出来。
    """
    known = qss_variables(mode)
    used = set(PLACEHOLDER.findall(qss_text))
    # QSS 自带的 `{` 不多，但仍要排除明显不是 Token 的（含大写或过长）
    used = {name for name in used if len(name) <= 40}
    return sorted(used - set(known))


def css_variables(mode='dark'):
    """给 WebEngine 侧的 `:root { --token: value; }` 片段（§15.3）。"""
    lines = [':root {']
    for name, value in sorted(active_tokens(mode).items()):
        lines.append('  --%s: %s;' % (name, value))
    for name, value in SPACING.items():
        lines.append('  --space-%s: %dpx;' % (name, value))
    lines.append('}')
    return '\n'.join(lines)


def style_for(role_name, mode='dark'):
    """文字角色的完整取值：字号 / 行高 / 字重 / 颜色值。

    比 `text_style()` 更方便 —— 颜色已经解析成 HEX。
    """
    style = text_style(role_name)
    return {
        'size': style.size,
        'line_height': style.line_height,
        'weight': style.weight,
        'colour': active_tokens(mode)[style.colour],
        'colour_token': style.colour,
    }
