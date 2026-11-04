"""Read the existing pure schema without importing the GUI fileio package.

fileio.__init__ currently initializes settings/logging and imports Qt. Storage
maintenance must reuse its schema while remaining an isolated, headless tool.
The path is application-owned; no project content is executed.
"""
from functools import cache
import importlib.util
from pathlib import Path


@cache
def legacy_schema():
    path = Path(__file__).resolve().parents[1] / 'fileio/schema.py'
    spec = importlib.util.spec_from_file_location('_prism_storage_legacy_schema', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
