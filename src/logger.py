"""语润（Yurun）日志模块：写 %APPDATA%\\Yurun\\logs\\yurun.log。
自动轮转：单文件不超过 1MB，保留最近 3 份。
"""
import logging
import logging.handlers
import os
from pathlib import Path

APP_NAME = "Yurun"


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