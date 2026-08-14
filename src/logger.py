"""语润（Yurun）日志模块：写 %APPDATA%\\Yurun\\logs\\yurun.log。

特点：
- 自动轮转：单文件不超过 1MB，保留最近 3 份。
- 全局崩溃捕获：任何未捕获异常（主线程 / 子线程 / Tk 回调）都写入日志，
  方便用户把日志文件发回开发者反馈问题。
- 启动 banner：记录版本 / Python / 平台，便于定位。
"""
import logging
import logging.handlers
import os
import sys
import threading
import traceback
from pathlib import Path

APP_NAME = "Yurun"
YURUN_VERSION = "0.1.0"

# 全局 Tk root 引用（由 main 在创建后注册），用于捕获 Tk 回调异常
_tk_root = None


def logs_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    d = Path(base) / APP_NAME / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


_logger = None


def get_logger(name: str = "yurun") -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.DEBUG)
        fmt = logging.Formatter(
            "%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        try:
            handler = logging.handlers.RotatingFileHandler(
                logs_dir() / "yurun.log",
                maxBytes=1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            handler.setFormatter(fmt)
            logger.addHandler(handler)
        except Exception:
            pass
        try:
            console = logging.StreamHandler()
            console.setFormatter(fmt)
            logger.addHandler(console)
        except Exception:
            pass
    logger.propagate = False
    _logger = logger
    return logger


def install_crash_handler():
    """把任何未捕获异常都写进日志文件（主线程 / 子线程 / Tk 回调）。

    调用一次即可。必须在程序尽可能早的位置调用，
    这样后续模块 import 失败、子线程崩溃、Tk 回调报错都能落盘。
    """
    log = get_logger("yurun")

    def _dump(tag, exc_type, exc_value, exc_tb):
        try:
            tb = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
            log.error("===== 未捕获异常(%s) =====\n%s", tag, tb)
            log.error("===== 请将以上日志发给开发者反馈问题 =====")
        except Exception:
            pass

    # 主线程未捕获异常
    def _sys_excepthook(exc_type, exc_value, exc_tb):
        _dump("main", exc_type, exc_value, exc_tb)

    sys.excepthook = _sys_excepthook

    # 子线程未捕获异常（Python 3.8+）
    if hasattr(threading, "excepthook"):
        def _thread_excepthook(args):
            _dump("thread:%s" % getattr(args, "thread", None),
                  args.exc_type, args.exc_value, args.exc_traceback)
        threading.excepthook = _thread_excepthook

    # Tk 回调异常（在 main 创建 root 后由 register_tk_error 接管）
    log.debug("crash handler 已安装")


def register_tk_error(root):
    """设置 Tk 的回调异常处理器，让 Tk 内部错误也写进日志。

    必须在 Tk root 创建后调用一次。
    """
    global _tk_root
    _tk_root = root
    try:
        def _report(*args):
            if len(args) >= 3:
                _dump("tk", args[0], args[1], args[2])
            else:
                log = get_logger("yurun")
                log.error("Tk 回调异常: %r", args)
        root.report_callback_exception = _report
    except Exception:
        pass


def log_startup_banner():
    """启动时打印版本 / 环境信息到日志，便于反馈时定位。"""
    import platform
    log = get_logger("yurun")
    log.info("=" * 56)
    log.info("语润 Yurun v%s 启动", YURUN_VERSION)
    log.info("Python %s", sys.version.split()[0])
    log.info("平台: %s", platform.platform())
    log.info("日志目录: %s", logs_dir())
    log.info("=" * 56)
