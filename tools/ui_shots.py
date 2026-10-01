"""给界面截图。改 UI 之前先看清它现在什么样。

用 QWidget.grab() 把自己渲染成图片，不需要真实显示器，也不会干扰
用户正在用的那个 Prism 实例。

用法：
    python tools/ui_shots.py                       # 只截空窗口
    python tools/ui_shots.py --project <路径>      # 顺带截打开工程后的样子
图片写到 tools/ui_shots/。
"""
import argparse
import os
import sys
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'windows')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 先解析再 import：Prism 的 CommandlineArgs 是单例，import 时就会读走
# sys.argv，并把它用不上的参数当成要打开的图片路径（见 baseline.py）。
_PARSER = argparse.ArgumentParser()
_PARSER.add_argument('--project', default='')
_PARSER.add_argument('--size', default='1400x900')
ARGS, _UNKNOWN = _PARSER.parse_known_args()
sys.argv = [sys.argv[0]]

from unittest.mock import patch                          # noqa: E402

from PyQt6 import QtCore, QtWidgets                      # noqa: E402

from prism.__main__ import PrismApplication, PrismMainWindow  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   'output', 'ui-shots')


def load_theme(app):
    """把主题装上，和 Prism 正常启动时一样。

    main() 里是 app.setStyleSheet(theme.qss)，直接构造 PrismMainWindow
    会跳过这一步。少了它截出来的是没有主题的界面 —— 那根本不是你
    看到的画面，拿它做判断会全部走偏。
    """
    assets = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          '..', 'prism', 'assets')
    qss_path = os.path.join(assets, 'theme.qss')
    if not os.path.exists(qss_path):
        print(f'  !! 找不到主题 {qss_path}', flush=True)
        return False
    with open(qss_path, 'r', encoding='utf-8') as handle:
        qss = handle.read()
    icons = os.path.join(assets, 'icons').replace(os.sep, '/')
    qss = qss.replace('url(assets/icons/', f'url({icons}/')
    app.setStyleSheet(qss)
    print(f'  主题已加载（{len(qss)} 字符）', flush=True)
    return True


def pump(app, seconds):
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        app.processEvents()
        time.sleep(0.01)


def shot(widget, name):
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, name)
    pixmap = widget.grab()
    ok = pixmap.save(path)
    print(f'  {"OK  " if ok else "失败"} {pixmap.width():4d}x{pixmap.height():4d}  '
          f'{name}', flush=True)
    return path


def main():
    width, height = (int(v) for v in ARGS.size.lower().split('x'))
    app = PrismApplication(['prism-shot'])
    load_theme(app)
    blocked = (
        patch.object(QtWidgets.QMessageBox, 'question',
                     return_value=QtWidgets.QMessageBox.StandardButton.No),
        patch.object(QtWidgets.QMessageBox, 'warning',
                     return_value=QtWidgets.QMessageBox.StandardButton.No),
        patch.object(QtWidgets.QMessageBox, 'information',
                     return_value=QtWidgets.QMessageBox.StandardButton.No),
        patch.object(QtWidgets.QMessageBox, 'critical',
                     return_value=QtWidgets.QMessageBox.StandardButton.No),
    )
    for item in blocked:
        item.start()

    window = PrismMainWindow(app)
    window.resize(width, height)
    window.show()
    pump(app, 1.5)
    print('空窗口：', flush=True)
    shot(window, '01-empty-window.png')

    # 把能单独截的面板也截下来
    for name, attr in (('02-detail-panel', 'detail_panel'),
                       ('03-category-panel', 'category_panel'),
                       ('04-color-filter', 'color_filter')):
        widget = getattr(window.view, '_' + attr, None) or \
            getattr(window, attr, None)
        if widget is not None and widget.isVisible():
            shot(widget, name + '.png')

    if ARGS.project and os.path.exists(ARGS.project):
        print('打开工程：', flush=True)
        view = window.view
        view.on_action_open_recent_file(ARGS.project)
        deadline = time.perf_counter() + 120
        while time.perf_counter() < deadline:
            pump(app, 0.5)
            if len([i for i in view.scene.items() if hasattr(i, 'save_id')]) >= 1000:
                break
        pump(app, 1.5)
        shot(window, '05-window-with-project.png')
        shot(window.view, '06-canvas-only.png')

    for item in blocked:
        item.stop()
    print(f'\n图片在 {OUT}', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
