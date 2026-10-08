# This file is part of Prism.
#
# Prism is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Prism is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Prism.  If not, see <https://www.gnu.org/licenses/>.

from prism.ui_tokens import COLOURS_DARK

APPNAME = 'Prism'
APPNAME_FULL = f'{APPNAME} Design Inspiration Board'
# Keep in sync with [project] version in pyproject.toml; tests/test_version.py
# fails if the two ever drift apart again.
VERSION = '0.3.6'
WEBSITE = 'https://github.com/CLF7758/prism'
# The credit line for the original project is kept verbatim: its license
# headers and copyright notices stay untouched in the source files, and this
# project credits Rebecca Breu as the original author.
COPYRIGHT = 'Copyright © 2021-2024 Rebecca Breu'
COPYRIGHT_PROJECT = 'Copyright © 2026 CLF7758'

CHANGED_SYMBOL = '✎'

# `COLORS` 是 Qt palette 要的 (r, g, b) 形式，值全部来自 ui_tokens —— 规范
# §15.3 要求"主题 Token 统一源"，所以这里**不许再写裸的色值**。改颜色请改
# prism/ui_tokens.py。
#
# 注：CHANGED_SYMBOL 那个 ✎ 按规范 §6 应该换成 SVG 图标（"不得用 ✎、◆、⚙
# 拼图标"），但它现在是设置界面表格里的一个**字符标记**而不是图标，且要动
# 三个文件；留给 B5 图标集统一处理。


def _rgb(token):
    """Token 的 HEX → (r, g, b)。"""
    value = COLOURS_DARK[token].lstrip('#')
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


COLORS = {
    'Active:Base': _rgb('app-bg'),
    'Active:AlternateBase': _rgb('panel-bg'),
    'Active:Window': _rgb('app-bg'),
    'Active:Button': _rgb('input-bg'),
    'Active:Text': _rgb('text-primary'),
    'Active:HighlightedText': _rgb('primary-text'),
    'Active:WindowText': _rgb('text-primary'),
    'Active:ButtonText': _rgb('text-primary'),
    'Active:Highlight': _rgb('accent'),
    'Active:Link': _rgb('accent'),

    'Disabled:Base': _rgb('app-bg'),
    'Disabled:Window': _rgb('app-bg') + (50,),
    'Disabled:WindowText': _rgb('text-disabled'),
    'Disabled:Light': (0, 0, 0, 0),
    'Disabled:Text': _rgb('text-disabled'),

    # Prism specific:
    # 画布上选中项的描边 —— 规范 §10"选中单体描边 1.5 accent"。
    'Scene:Selection': _rgb('accent'),
    'Scene:Canvas': _rgb('app-bg'),
    'Scene:Text': _rgb('text-primary'),
}
