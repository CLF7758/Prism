"""Wire the model domain into the legacy SQLite writer and reader."""
import io
import sys

EDITS = [
    ('prism/fileio/sql.py', [
        # 1. read: reset and load the model records
        (
            "        self.scene.material_records = []\n"
            "        self.scene.material_library_state = {}\n",
            "        self.scene.material_records = []\n"
            "        self.scene.model_records = []\n"
            "        self.scene.material_library_state = {}\n",
            1,
        ),
        (
            "            self.scene.material_records = records\n",
            "            self.scene.material_records = records\n"
            "        table = self.fetchone(\n"
            "            \"SELECT name FROM sqlite_master WHERE type='table' \"\n"
            "            \"AND name='model_records'\")\n"
            "        if table:\n"
            "            try:\n"
            "                models = []\n"
            "                for (payload,) in self.fetchall(\n"
            "                        'SELECT payload FROM model_records ORDER BY rowid'):\n"
            "                    try:\n"
            "                        model = json.loads(payload)\n"
            "                    except (TypeError, ValueError) as exc:\n"
            "                        raise ValueError('模型库数据损坏，已停止读取以避免覆盖') from exc\n"
            "                    if not isinstance(model, dict) or not model.get('id'):\n"
            "                        raise ValueError('模型库数据无效，已停止读取以避免覆盖')\n"
            "                    models.append(model)\n"
            "                from prism.model_library.project_storage import validate_model_records\n"
            "                validate_model_records(models, self.scene.project_id)\n"
            "            except (ValueError, TypeError) as exc:\n"
            "                raise ValueError('模型库数据损坏，已停止读取以避免覆盖：' + str(exc)) from exc\n"
            "            self.scene.model_records = models\n",
            1,
        ),
        # 2. write: create the table
        (
            "        self.ex('CREATE TABLE IF NOT EXISTS material_records ('\n"
            "                'id TEXT PRIMARY KEY, payload JSON NOT NULL)')\n",
            "        self.ex('CREATE TABLE IF NOT EXISTS material_records ('\n"
            "                'id TEXT PRIMARY KEY, payload JSON NOT NULL)')\n"
            "        self.ex('CREATE TABLE IF NOT EXISTS model_records ('\n"
            "                'id TEXT PRIMARY KEY, payload JSON NOT NULL)')\n",
            1,
        ),
        # 3. write: the originals of a model are owned by the model domain and
        #    must not be dropped by the note's own attachment cleanup.
        (
            "        keep = set((getattr(scene, 'attachments', {}) or {}).keys())\n",
            "        keep = set((getattr(scene, 'attachments', {}) or {}).keys())\n"
            "        # The originals of a model live in the same table but belong to\n"
            "        # the model domain.  The note's cleanup must not delete them.\n"
            "        keep |= _model_attachment_ids(getattr(scene, 'model_records', ()) or ())\n",
            1,
        ),
        # 4. write: the records themselves
        (
            "        self._sync_extra_rows('workspace_nodes', ('id', 'payload'), [\n",
            "        models = list(getattr(scene, 'model_records', []) or [])\n"
            "        from prism.model_library.project_storage import validate_model_records\n"
            "        validate_model_records(models, scene.project_id)\n"
            "        self._sync_extra_rows('model_records', ('id', 'payload'), [\n"
            "            (model['id'], json.dumps(model, ensure_ascii=False, allow_nan=False))\n"
            "            for model in models])\n"
            "        self._sync_extra_rows('workspace_nodes', ('id', 'payload'), [\n",
            1,
        ),
    ]),
]

HELPER = '''

def _model_attachment_ids(records):
    """The project attachments the model domain owns.

    The originals of a model are stored in the project's attachment table.
    They are not pictures of the note, so the note's own cleanup must leave
    them alone.
    """
    owned = set()
    for record in records or ():
        if not isinstance(record, dict):
            continue
        for item in record.get('files', []) or ():
            if not isinstance(item, dict) or item.get('missing'):
                continue
            attachment_id = item.get('attachment_id')
            if attachment_id is not None:
                owned.add(int(attachment_id))
    return owned
'''


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


def add_helper(path):
    with io.open(path, 'r', encoding='utf-8', newline='') as handle:
        text = handle.read()
    if '_model_attachment_ids' in text and 'def _model_attachment_ids(' in text:
        return []
    newline = '\r\n' if '\r\n' in text else '\n'
    marker = newline.join([
        'def load_deferred_board_extras(scene, names=None, *, page_ids=None):',
    ])
    if marker not in text:
        return [(path, 'helper anchor not found')]
    helper = HELPER.replace('\n', newline)
    text = text.replace(marker, helper + newline + marker, 1)
    with io.open(path, 'w', encoding='utf-8', newline='') as handle:
        handle.write(text)
    return []


def main():
    failures = []
    for path, replacements in EDITS:
        failures.extend(apply(path, replacements))
    failures.extend(add_helper('prism/fileio/sql.py'))
    if failures:
        for path, reason in failures:
            print('FAILED', path, '::', reason)
        return 1
    print('sql.py model-domain integration applied')
    return 0


if __name__ == '__main__':
    sys.exit(main())
