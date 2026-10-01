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

import logging

from PyQt6 import QtCore

logger = logging.getLogger(__name__)

# Available languages: ('value', 'display_label')
LANGUAGES = (
    ('zh_CN', '中文'),
    ('en', 'English'),
)


class Translator:
    def __init__(self):
        self._translations = {}
        self._load()

    def _load(self):
        lang = self._read_language()
        if lang == 'zh_CN':
            from .zh_CN import zh_CN
            self._translations = zh_CN
        else:
            self._translations = {}

    @staticmethod
    def _read_language():
        fmt = QtCore.QSettings.Format.IniFormat
        scope = QtCore.QSettings.Scope.UserScope
        import sys
        settings_dir = None
        for i, arg in enumerate(sys.argv):
            if arg == '--settings-dir' and i + 1 < len(sys.argv):
                settings_dir = sys.argv[i + 1]
                break
            if arg.startswith('--settings-dir='):
                settings_dir = arg.split('=', 1)[1]
                break
        if settings_dir:
            QtCore.QSettings.setPath(fmt, scope, settings_dir)
        qs = QtCore.QSettings(fmt, scope, 'Prism', 'Prism')
        lang = qs.value('Interface/language', 'zh_CN')
        return lang

    def translate(self, text):
        return self._translations.get(text, text)

    def reload(self):
        self._load()


translator = Translator()


def _(text):
    return translator.translate(text)
