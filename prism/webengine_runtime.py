"""One shared WebEngine policy: text editors must not disable 3D globally."""
import os


def configure_webengine():
    # Respect explicit user/test overrides; the production default leaves ANGLE
    # and Chromium's software fallback available. Do not disable both renderers.
    os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS',
                          '--disable-features=RendererCodeIntegrity')
