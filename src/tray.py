"""语润（Yurun）托盘模块：系统托盘图标 + 菜单。
pystray 在后台线程运行；notify 也线程安全（内部投递）。
"""
import os
import sys
import threading
from pathlib import Path

import pystray
from PIL import Image, ImageDraw
from logger import logs_dir

# 在模块加载时解析图标路径（避免 @staticmethod 内 __file__ 作用域歧义）
def _resolve_icon():
    try:
        if getattr(sys, "frozen", False):
            base = getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)
        else:
            base = Path(__file__).resolve().parent
        cands = [
            Path(base).parent / "assets" / "icon.ico",
            Path(base) / "assets" / "icon.ico",
        ]
        for c in cands:
            if c.exists():
                return str(c)
    except Exception:
        pass
    return None

ASSETS_ICON = _resolve_icon()


class Tray:
    def __init__(self, on_quit=None, on_open_settings=None):
        self._icon = None
        self._on_quit = on_quit
        self._on_open_settings = on_open_settings
        self._lock = threading.Lock()

    @staticmethod
    def _icon_path():
        """返回 favicon.ico 的绝对路径（开发/打包两种模式）。"""
        return ASSETS_ICON

    @staticmethod
    def _make_image(size=64):
        """加载项目图标 assets/icon.ico（favicon，含多尺寸 + 透明）。"""
        try:
            ico = Tray._icon_path()
            if ico:
                img = Image.open(ico).convert("RGBA").resize((size, size), Image.LANCZOS)
                return img
        except Exception:
            pass
        return Image.new("RGBA", (size, size), (0, 0, 0, 0))

    def start(self, title="语润"):
        img = self._make_image()
        menu = pystray.Menu(
            pystray.MenuItem("打开设置", lambda: self._safe(self._on_open_settings)),
            pystray.MenuItem("打开日志目录", lambda: self._safe(self._open_logs)),
            pystray.MenuItem("退出", lambda: self._safe(self._on_quit)),
        )
        # pystray 的 icon 参数必须是 PIL Image；传字符串路径会在 setup 线程
        # 抛 'str' object has no attribute 'save'（构造不报错、后台 setup 才崩，
        # 故 try/except 兜底无效）。img 已由 _make_image() 用 PIL 加载好。
        self._icon = pystray.Icon("yurun", img, title, menu)
        try:
            self._icon.run()
        except Exception as e:
            import sys
            sys.stderr.write(f"tray error: {e}\n")

    def _open_logs(self):
        """打开日志目录，方便用户把 yurun.log 发给开发者反馈问题。"""
        try:
            os.startfile(str(logs_dir()))
        except Exception:
            pass

    def _safe(self, fn):
        try:
            if fn:
                fn()
        except Exception:
            pass

    def notify(self, message: str, title: str = "语润"):
        """显示通知气泡（可选，程序主要用迷你浮窗）。"""
        def _do():
            try:
                if self._icon:
                    self._icon.notify(message, title)
            except Exception:
                pass
        threading.Thread(target=_do, daemon=True).start()

    def stop(self):
        try:
            if self._icon:
                self._icon.stop()
        except Exception:
            pass


# 全局单例引用（main 里设置）
_tray_instance = None
def get_tray():
    return _tray_instance
