"""One shared WebEngine policy, and the one-time costs of a Qt start-up.

The text editors must not disable 3D globally, and the engine costs that a
process pays exactly once - Chromium's browser process and the platform font
engine - are paid before there is a window to freeze.
"""
import logging
import os

logger = logging.getLogger(__name__)


def configure_webengine():
    # Respect explicit user/test overrides; the production default leaves ANGLE
    # and Chromium's software fallback available. Do not disable both renderers.
    os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS',
                          '--disable-features=RendererCodeIntegrity')


def warm_text_layout(app=None):
    """Pay the font engine's one-time cost before there is a window.

    The first text widget of a process resolves the platform font engine
    (DirectWrite on Windows).  Measured on this machine: the first ``QLabel``
    costs **138.6 ms** in ``show()`` and a plain label 202.5 ms, while the
    second one costs 18.3 ms.  In the running application the same cost
    showed up as a **502.4 ms** stall that the trace attributed to
    ``prism/widgets/welcome_overlay.py:110`` - the first label of the window,
    built while the window was already on its way to the screen.

    A label that is shown before the window exists pays it while there is
    nothing on screen yet; the label is kept alive on the application so the
    font engine stays warm for the panels that follow.
    """
    try:
        from PyQt6 import QtCore, QtWidgets, sip
    except ImportError:                       # pragma: no cover - optional
        return None
    app = app or QtWidgets.QApplication.instance()
    if app is None or QtCore.QThread.currentThread() != app.thread():
        logger.debug('Text layout warm-up requires the GUI application thread')
        return None
    cached = app.__dict__.get('_text_layout_warmup')
    if cached is not None and not sip.isdeleted(cached):
        return cached
    try:
        label = QtWidgets.QLabel('Prism')
        # Polish the label without putting it on screen: the font engine is
        # resolved during the first layout, not during the construction.
        label.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        label.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        label.show()
    except Exception:                         # pragma: no cover - defensive
        logger.debug('Text layout warm-up failed; the first label will pay for it later',
                     exc_info=True)
        return None
    # Keep one label alive for the whole session, including repeated callers.
    app._text_layout_warmup = label
    label.hide()
    return label


def warm_web_engine(app=None):
    """Pay Qt Web Engine's one-time start-up cost before there is a window.

    The first ``QWebEngineView`` of a process initializes the browser
    profile and starts Chromium: measured on the real project this is
    199 ms of GUI-thread time (445 ms in the desktop session), and it cannot
    be split, because Qt widgets may only be built on the GUI thread.

    Inside the library pre-warm that cost landed in a visible window and
    read as a freeze.  Paying it here - while there is no window yet -
    keeps the cost, but not the freeze.  The view is kept alive on the
    application so the profile stays warm for the panels that follow.
    """
    try:
        from PyQt6 import QtCore, QtWidgets, QtWebEngineWidgets, sip
    except ImportError:                       # pragma: no cover - optional
        return None
    app = app or QtWidgets.QApplication.instance()
    if app is None or QtCore.QThread.currentThread() != app.thread():
        logger.debug('Web engine warm-up requires the GUI application thread')
        return None
    cached = app.__dict__.get('_webengine_warmup')
    if isinstance(cached, QtWebEngineWidgets.QWebEngineView) and not sip.isdeleted(cached):
        return cached
    try:
        view = QtWebEngineWidgets.QWebEngineView()
        # Activate the backing surface without flashing a temporary window or
        # taking focus from the user. The main window has not been built yet.
        view.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        view.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        # Touch the page explicitly: the profile, not the widget, is what
        # costs the half second.
        view.page()
        view.setHtml('<html><body></body></html>')
    except Exception:                         # pragma: no cover - defensive
        logger.debug('Web engine warm-up failed; the panels will pay for it later',
                     exc_info=True)
        return None
    # Keep one view alive for the whole session, including repeated callers.
    app._webengine_warmup = view
    view.show()
    view.hide()
    return view
