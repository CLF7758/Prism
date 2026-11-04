"""Apply the model-domain save-chain integration (newline aware).

Every replacement is verified, so a silent no-op is impossible.  Running it
twice is safe: an already applied replacement is skipped.
"""
import io
import sys

EDITS = [
    ('prism/fileio/partial_save.py', [
        (
            "    'material_records': ('id', ('payload',), ('payload',)),\n",
            "    'material_records': ('id', ('payload',), ('payload',)),\n"
            "    'model_records': ('id', ('payload',), ('payload',)),\n",
            1,
        ),
        (
            "            if table not in ('workspace_nodes', 'material_records'):\n",
            "            if table not in ('workspace_nodes', 'material_records', 'model_records'):\n",
            2,
        ),
    ]),
    ('prism/partial_project.py', [
        (
            "            domain_changes = self._domains.capture(\n"
            "                getattr(scene, 'material_records', ()) or (),\n"
            "                getattr(scene, 'workspace_nodes', ()) or (),\n"
            "                explicit_deletions=self._domain_deletions)\n",
            "            domain_changes = self._domains.capture(\n"
            "                getattr(scene, 'material_records', ()) or (),\n"
            "                getattr(scene, 'workspace_nodes', ()) or (),\n"
            "                getattr(scene, 'model_records', ()) or (),\n"
            "                explicit_deletions=self._domain_deletions)\n",
            1,
        ),
        (
            "                for table in ('material_records', 'workspace_nodes'):\n",
            "                for table in ('material_records', 'workspace_nodes', 'model_records'):\n",
            1,
        ),
        (
            "        return [name for name in ('material_records', 'workspace_nodes', 'material_library_state')\n",
            "        return [name for name in ('material_records', 'workspace_nodes', 'model_records',\n"
            "                                  'material_library_state')\n",
            1,
        ),
        (
            "            if change.table in ('material_records', 'workspace_nodes') and change.after is None:\n",
            "            if change.table in ('material_records', 'workspace_nodes', 'model_records') \\\n"
            "                    and change.after is None:\n",
            1,
        ),
    ]),
]


def apply(path, replacements):
    with io.open(path, 'r', encoding='utf-8', newline='') as handle:
        text = handle.read()
    newline = '\r\n' if '\r\n' in text else '\n'
    failures = []
    for old, new, expected in replacements:
        old_n = old.replace('\n', newline)
        new_n = new.replace('\n', newline)
        if new_n in text and old_n not in text:
            continue
        count = text.count(old_n)
        if count != expected:
            failures.append((path, 'found %d, expected %d :: %s'
                             % (count, expected, old.splitlines()[0][:60])))
            continue
        text = text.replace(old_n, new_n)
    with io.open(path, 'w', encoding='utf-8', newline='') as handle:
        handle.write(text)
    return failures


def main():
    failures = []
    for path, replacements in EDITS:
        failures.extend(apply(path, replacements))
    if failures:
        for path, reason in failures:
            print('FAILED', path, '::', reason)
        return 1
    print('model-domain save-chain edits applied')
    return 0


if __name__ == '__main__':
    sys.exit(main())
