"""One preview contract for the library ball and the native model adapter."""
import copy
import hashlib
import json
from functools import lru_cache
from pathlib import Path

ASSET_ROOT = Path(__file__).resolve().parent / 'assets' / 'materials'


@lru_cache(maxsize=1)
def _profile():
    return json.loads((ASSET_ROOT / 'preview-profile.json').read_text(encoding='utf-8'))


def preview_profile(library='material'):
    profile = copy.deepcopy(_profile())
    overrides = profile.pop('libraries', {})
    profile.update(overrides.get(library, {}))
    return profile


def profile_signature(library='material'):
    return hashlib.sha256(json.dumps(preview_profile(library), sort_keys=True).encode()).hexdigest()
