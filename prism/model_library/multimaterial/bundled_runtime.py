"""Locate shipped runtimes, never a user's external F3D/Python installation."""
import importlib.util
from pathlib import Path
import sys


def assimp_path():
    base=(Path(sys._MEIPASS)/'prism' if getattr(sys,'frozen',False)
          else Path(__file__).resolve().parents[2])
    return base/'assets'/'model-runtime'/'assimp-vc145-mt.dll'


def available():
    return assimp_path().is_file() and importlib.util.find_spec('PyQt6.QtWebEngineWidgets') is not None
