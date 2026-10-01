"""Guards against the runtime and packaged versions drifting apart.

The version used to be declared twice - once in prism/constants.py and once in
pyproject.toml - and the two had diverged (0.1.0 vs 0.3.4-dev). pyproject.toml
is the single source of truth; these tests keep constants.py honest.
"""
import os.path
import re

from prism import constants

VERSION_PATTERN = r'^version\s*=\s*"([^"]+)"'


def read_pyproject_version():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'pyproject.toml'), encoding='utf-8') as f:
        text = f.read()
    match = re.search(VERSION_PATTERN, text, re.MULTILINE)
    assert match is not None, 'pyproject.toml declares no project version'
    return match.group(1)


def test_runtime_version_matches_pyproject():
    assert constants.VERSION == read_pyproject_version()


def test_version_is_pep440_compatible():
    """Pip and PyPI reject versions such as the old '0.3.4-dev'."""
    assert constants.VERSION == constants.VERSION.strip()
    assert re.fullmatch(
        r'\d+(\.\d+)*((a|b|rc)\d+)?(\.post\d+)?(\.dev\d+)?',
        constants.VERSION), constants.VERSION
