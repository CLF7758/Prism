"""Read-only preview and count verification against a local project."""
import sys, tempfile, time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PyQt6 import QtWidgets
from prism.__main__ import PrismApplication, PrismMainWindow
from prism.config import PrismSettings
from prism.fileio.sql import SQLiteIO
from tools.preview_native_ui import pump

def main():
    root = Path(__file__).resolve().parents[1]
    # Optional argument; auto-discovery only searches the Desktop root.
    project = Path(sys.argv[1]) if len(sys.argv) > 1 else next((root.parent).glob('*.prism'))
    sys.argv = [sys.argv[0]]
    with tempfile.TemporaryDirectory(prefix='prism-readonly-preview-', ignore_cleanup_errors=True) as settings, patch.object(PrismSettings, 'get_settings_dir', return_value=settings), patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.StandardButton.Yes):
        app = PrismApplication(['preview'])
        qss = (root / 'prism/assets/theme.qss').read_text(encoding='utf8').replace('url(assets/icons/', 'url(' + (root / 'prism/assets/icons').as_posix() + '/')
        app.setStyleSheet(qss)
        w = PrismMainWindow(app)
        io = SQLiteIO(str(project), w.view.scene, readonly=True)
        start = time.monotonic()
        io.read()
        w.view.scene.add_queued_items()
        w.view.category_panel.force_rebuild()
        w._reload_workspace_pages()
        w.resize(1680, 960)
        w.show()
        w.asset_browser.open('all')
        pump(app, 4)
        catalog = w.view.category_panel.catalog
        counts = {'total': catalog.total_count(), 'untagged': catalog.untagged_count(), 'browser_rows': w.asset_browser.model.rowCount(), 'seconds': round(time.monotonic()-start, 2)}
        out = root / 'output/ui-screenshots'
        out.mkdir(parents=True, exist_ok=True)
        w.grab().save(str(out / 'eagle-real-project.png'))
        print(counts, flush=True)
        assert counts['total'] == counts['browser_rows']
        w.view.undo_stack.setClean()
        w.close()
        pump(app, .2)
        io.connection.close()

if __name__ == '__main__':
    main()
