"""Make the legacy writer use the shared model-table definition."""
import io
import sys

PATH = 'prism/fileio/sql.py'
OLD = ("        self.ex('CREATE TABLE IF NOT EXISTS model_records ('\n"
       "                'id TEXT PRIMARY KEY, payload JSON NOT NULL)')\n")
NEW = ("        # The model domain owns its table definition; the writer only\n"
       "        # makes sure it exists (with the indexed projections).\n"
       "        from prism.model_library.project_storage import ensure_model_tables\n"
       "        ensure_model_tables(self.connection)\n")


def main():
    with io.open(PATH, 'r', encoding='utf-8', newline='') as handle:
        text = handle.read()
    newline = '\r\n' if '\r\n' in text else '\n'
    old = OLD.replace('\n', newline)
    new = NEW.replace('\n', newline)
    if new in text:
        print('already applied')
        return 0
    if text.count(old) != 1:
        print('FAILED: anchor found', text.count(old), 'times')
        return 1
    text = text.replace(old, new)
    with io.open(PATH, 'w', encoding='utf-8', newline='') as handle:
        handle.write(text)
    print('sql.py model table definition unified')
    return 0


if __name__ == '__main__':
    sys.exit(main())
