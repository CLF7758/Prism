"""B6：界面缩放（§3.1）。

规范要 100 / 125 / 150 / 200%，并且**不许和 Windows 的系统 DPI 重复缩放**
（Qt 6 自己按系统 DPI 缩，32 px 行高不会再被乘一次；这一档只加用户自己选的）。

缩放比例必须在 QApplication 建起来之前交给 Qt（`QT_SCALE_FACTOR`），所以界面上
改这一档是"重启后生效" —— 这里验的就是：档位记得住、因子算得对、环境变量设得对、
菜单如实告诉用户要重启。
"""
from unittest.mock import patch

from PyQt6 import QtWidgets

from prism import ui_scale
from prism.config import PrismSettings


# ── 因子换算 ──────────────────────────────────────────────────────────

def test_one_hundred_percent_means_leave_qt_alone(qapp):
    assert ui_scale.scale_factor(100) is None


def test_the_other_steps_map_to_a_factor(qapp):
    assert ui_scale.scale_factor(125) == '1.25'
    assert ui_scale.scale_factor(150) == '1.5'
    assert ui_scale.scale_factor(200) == '2.0'


def test_unknown_values_are_ignored(qapp):
    for value in (0, 33, 175, 400, 'x', None):
        assert ui_scale.scale_factor(value) is None


def test_the_menu_offers_exactly_the_four_steps(qapp):
    assert ui_scale.SCALES == (100, 125, 150, 200)


# ── 记住档位 ──────────────────────────────────────────────────────────

def test_the_scale_is_remembered(qapp):
    settings = PrismSettings()
    assert ui_scale.remembered_scale(settings) == 100
    assert ui_scale.remember_scale(150, settings)
    assert ui_scale.remembered_scale(settings) == 150


def test_a_nonsense_value_falls_back_to_one_hundred(qapp):
    settings = PrismSettings()
    settings.setValue(ui_scale.SETTINGS_KEY, 999)
    settings.sync()
    assert ui_scale.remembered_scale(settings) == 100
    assert ui_scale.remember_scale(999, settings) is False


# ── 启动时交给 Qt ─────────────────────────────────────────────────────

def test_the_environment_gets_the_factor_before_qt_starts(qapp):
    settings = PrismSettings()
    ui_scale.remember_scale(125, settings)
    environ = {}

    factor = ui_scale.apply_scale_environment(settings, environ)

    assert factor == '1.25'
    assert environ['QT_SCALE_FACTOR'] == '1.25'


def test_one_hundred_leaves_the_environment_alone(qapp):
    settings = PrismSettings()
    ui_scale.remember_scale(100, settings)
    environ = {}

    assert ui_scale.apply_scale_environment(settings, environ) is None
    assert 'QT_SCALE_FACTOR' not in environ


def test_an_explicit_environment_variable_wins(qapp):
    """启动脚本或用户显式设的 QT_SCALE_FACTOR 优先级更高。"""
    settings = PrismSettings()
    ui_scale.remember_scale(200, settings)
    environ = {'QT_SCALE_FACTOR': '1.5'}

    assert ui_scale.apply_scale_environment(settings, environ) == '1.5'
    assert environ['QT_SCALE_FACTOR'] == '1.5'


# ── 菜单 ──────────────────────────────────────────────────────────────

def test_the_layout_menu_has_the_scale_steps(main_window):
    window = main_window
    window._build_layout_menu()
    labels = [action.text() for action in window.scale_actions.actions()]
    assert labels == ['100%', '125%', '150%', '200%']
    assert window.scale_actions.checkedAction().text() == '100%'


def test_choosing_a_step_remembers_it_and_says_it_needs_a_restart(
        main_window):
    window = main_window
    window._build_layout_menu()
    with patch('PyQt6.QtWidgets.QMessageBox.information') as told:
        window._change_ui_scale(150)
    assert told.called, '要如实告诉用户重启后生效'
    assert '重启' in told.call_args[0][2]
    assert PrismSettings().value(ui_scale.SETTINGS_KEY, 100, type=int) == 150


def test_the_menu_ticks_the_remembered_step(main_window):
    window = main_window
    ui_scale.remember_scale(200, window.view.settings)
    window._build_layout_menu()
    assert window.scale_actions.checkedAction().text() == '200%'
    ui_scale.remember_scale(100, window.view.settings)
