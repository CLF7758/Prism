"""Exercise the shipped QSS and native paint path in an isolated process."""
import os
from pathlib import Path
import subprocess
import sys

import pytest
from PyQt6 import QtGui, QtWidgets


@pytest.mark.parametrize('platform', ['offscreen', 'windows'])
def test_resource_tree_theme_paint_does_not_crash(platform):
    if platform == 'windows' and sys.platform != 'win32':
        pytest.skip('Windows native style regression')
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, QT_QPA_PLATFORM=platform, PYTHONIOENCODING='utf-8')
    result = subprocess.run(
        [sys.executable, '-X', 'faulthandler', 'tests/_resource_section_startup_probe.py'],
        cwd=root, env=env, capture_output=True, text=True, encoding='utf-8',
        errors='replace', timeout=30,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert 'PAINT_OK' in output
    assert 'QPainter::' not in output


def test_main_window_paints_with_shipped_theme(main_window, qapp, qtlog):
    root = Path(__file__).resolve().parents[1]
    qss = (root / 'prism/assets/theme.qss').read_text(encoding='utf-8')
    qss = qss.replace('url(assets/icons/', 'url(' + (root / 'prism/assets/icons').as_posix() + '/')
    previous = qapp.styleSheet()
    try:
        qapp.setStyleSheet(qss)
        main_window.resize(1200, 800)
        main_window.ensurePolished()
        image = QtGui.QImage(main_window.size(), QtGui.QImage.Format.Format_ARGB32)
        main_window.render(image)
        assert not any('QPainter::' in record.message for record in qtlog.records)
        tree = main_window.view.category_panel.resource_tree
        option = QtWidgets.QStyleOptionViewItem()
        tree.initViewItemOption(option)
        assert option.decorationSize.isValid()
        assert option.widget is tree
    finally:
        qapp.setStyleSheet(previous)
