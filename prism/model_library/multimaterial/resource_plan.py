"""Detached command resource staging and registration policy."""
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class ResourcePlan:
    resource_ids: tuple
    files: tuple

    def register(self, existing, stored):
        if len(stored) < len(self.resource_ids):
            raise ValueError('Incomplete stored material resources')
        result = deepcopy(existing)
        for rid, resource in zip(self.resource_ids, stored):
            if 'sha256:' + resource.sha256 != rid:
                raise ValueError('Texture changed during commit')
            result[rid] = dict(attachment_id=resource.attachment_id,
                sha256=resource.sha256, locator=resource.name, size=resource.size_bytes)
        return result

def plan_resources(definitions, registered, paths):
    ids, files, seen = [], [], set(registered)
    for definition in definitions:
        for use in definition.textures:
            rid = use.resource_id
            if not rid or rid in seen:
                continue
            path = paths.get(rid)
            if not path:
                raise ValueError('Unregistered material texture has no source: ' + rid)
            seen.add(rid)
            ids.append(rid)
            files.append((path, Path(path).name))
    return ResourcePlan(tuple(ids), tuple(files))
