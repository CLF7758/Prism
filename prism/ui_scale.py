"""界面缩放档位（规范 §3.1）。

Qt 6 已经自己处理系统 DPI：Windows 设成 150% 时，一个 32 px 的控件仍然按 32 个
**逻辑像素**画，不会被再乘一次 —— 规范那句「不允许把 32 px 行高再手工乘一次系统
DPI」靠的就是这个。所以这里只负责**用户自己选的那一档**缩放。

缩放比例必须在 `QApplication` 建起来之前交给 Qt（`QT_SCALE_FACTOR` 环境变量），
所以界面上改这一档是**重启后生效**：菜单里如实这么写，不做"看起来能用、其实没反应"
的入口（红线的一条）。
"""
import logging
import os

logger = logging.getLogger(__name__)

#: 允许的档位（§3.1：100 / 125 / 150 / 200%）
SCALES = (100, 125, 150, 200)
DEFAULT_SCALE = 100
SETTINGS_KEY = 'Interface/scale'
ENV_VAR = 'QT_SCALE_FACTOR'


def scale_factor(percent):
    """档位 -> 交给 Qt 的因子；100% 返回 None（什么都不用设）。"""
    try:
        value = int(percent)
    except (TypeError, ValueError):
        return None
    if value not in SCALES or value == DEFAULT_SCALE:
        return None
    return str(value / 100)


def remembered_scale(settings=None):
    """设置里记着的档位；读不出来或不是档位里的值就当 100%。"""
    if settings is None:
        from prism.config import PrismSettings
        settings = PrismSettings()
    try:
        value = int(settings.value(SETTINGS_KEY, DEFAULT_SCALE, type=int))
    except (TypeError, ValueError):
        return DEFAULT_SCALE
    return value if value in SCALES else DEFAULT_SCALE


def remember_scale(percent, settings=None):
    """把档位记下来（下一次启动时生效）。返回是否被接受。"""
    if settings is None:
        from prism.config import PrismSettings
        settings = PrismSettings()
    try:
        value = int(percent)
    except (TypeError, ValueError):
        return False
    if value not in SCALES:
        return False
    settings.setValue(SETTINGS_KEY, value)
    settings.sync()
    return True


def apply_scale_environment(settings=None, environ=None):
    """把记着的档位交给 Qt —— **必须在 QApplication 之前调用**。

    返回实际设置的值（没设置就是 None）。已经有 `QT_SCALE_FACTOR` 时不动它：
    那是启动脚本或用户显式设的，优先级更高。
    """
    environ = os.environ if environ is None else environ
    factor = scale_factor(remembered_scale(settings))
    if factor is None:
        return None
    if environ.get(ENV_VAR):
        logger.debug('%s already set to %s, keeping it',
                     ENV_VAR, environ[ENV_VAR])
        return environ[ENV_VAR]
    environ[ENV_VAR] = factor
    logger.info('界面缩放 %s -> %s=%s',
                remembered_scale(settings), ENV_VAR, factor)
    return factor
