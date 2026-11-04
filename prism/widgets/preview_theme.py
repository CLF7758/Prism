"""Use the application's actual theme in isolated preview processes."""
import sys
from pathlib import Path
from PyQt6 import QtGui
from prism import constants
from prism.utils import create_palette_from_dict


def apply_preview_theme(app):
    app.setPalette(create_palette_from_dict(constants.COLORS))
    font = QtGui.QFont()
    font.setFamilies(['Microsoft YaHei UI', 'Microsoft YaHei', 'Segoe UI', 'sans-serif'])
    font.setPixelSize(13)
    app.setFont(font)
    assets = (Path(sys._MEIPASS) / 'prism/assets' if getattr(sys, 'frozen', False)
              else Path(__file__).resolve().parents[1] / 'assets')
    qss = (assets / 'theme.qss').read_text(encoding='utf-8')
    app.setStyleSheet(qss.replace('url(assets/icons/',
        'url(' + (assets / 'icons').as_posix() + '/'))
    from .dialog_theme import install
    install(app)
