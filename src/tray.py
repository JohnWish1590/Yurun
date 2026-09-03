"""语润（Yurun）托盘模块：系统托盘图标 + 菜单。
pystray 在后台线程运行；notify 也线程安全（内部投递）。
"""
import os
import sys
import threading
from pathlib import Path

import pystray
from PIL import Image, ImageDraw
from logger import get_logger, logs_dir

log = get_logger("yurun.tray")

# 在模块加载时解析图标路径（避免 @staticmethod 内 __file__ 作用域歧义）
def _resolve_tray_icon():
    try:
        if getattr(sys, "frozen", False):
            base = getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)
        else:
            base = Path(__file__).resolve().parent
        cands = [
            Path(base).parent / "assets" / "tray_16.png",
            Path(base) / "assets" / "tray_16.png",
            Path(base).parent / "assets" / "tray.ico",
            Path(base) / "assets" / "tray.ico",
            Path(base).parent / "assets" / "icon.ico",
            Path(base) / "assets" / "icon.ico",
        ]
        for c in cands:
            if c.exists():
                return str(c)
    except Exception:
        pass
    return None

ASSETS_TRAY_ICON = _resolve_tray_icon()


class Tray:
    def __init__(self, on_quit=None, on_open_settings=None, on_open_dictionary=None,
                 on_set_input_mode=None):
        self._icon = None
        self._on_quit = on_quit
        self._on_open_settings = on_open_settings
        self._on_open_dictionary = on_open_dictionary
        self._on_set_input_mode = on_set_input_mode
        self._lock = threading.Lock()

    @staticmethod
    def _icon_path():
        """返回专用 16×16 托盘图标路径（开发/打包两种模式）。"""
        return ASSETS_TRAY_ICON

    @staticmethod
    def _make_image(size=16):
        """优先加载专用 16×16 托盘图标；缺失时回退主图标。"""
        try:
            icon_path = Tray._icon_path()
            if icon_path:
                img = Image.open(icon_path).convert("RGBA")
                if img.size != (size, size):
                    img = img.resize((size, size), Image.LANCZOS)
                return img
        except Exception:
            pass
        return Image.new("RGBA", (size, size), (0, 0, 0, 0))

    def start(self, title="语润"):
        img = self._make_image()
        menu = pystray.Menu(
            # pystray 会传入 (icon, item) 两个参数；必须显式接收，不能用无参 lambda。
            pystray.MenuItem("打开设置", lambda _icon, _item: self._invoke_menu("打开设置", self._on_open_settings)),
            pystray.MenuItem("管理个人记忆", lambda _icon, _item: self._invoke_menu("管理个人记忆", self._on_open_dictionary)),
            pystray.MenuItem("打开日志目录", lambda _icon, _item: self._invoke_menu("打开日志目录", self._open_logs)),
            pystray.MenuItem(
                "快速输入",
                lambda _icon, _item: self._invoke_menu("快速输入", lambda: self._set_input_mode("direct")),
                checked=lambda item: self._mode_checked("direct"),
            ),
            pystray.MenuItem(
                "智能整理",
                lambda _icon, _item: self._invoke_menu("智能整理", lambda: self._set_input_mode("refine")),
                checked=lambda item: self._mode_checked("refine"),
                enabled=lambda item: self._refine_available(),
            ),
            pystray.MenuItem("退出", lambda _icon, _item: self._invoke_menu("退出", self._on_quit)),
        )
        # pystray 的 icon 参数必须是 PIL Image；传字符串路径会在 setup 线程
        # 抛 'str' object has no attribute 'save'（构造不报错、后台 setup 才崩，
        # 故 try/except 兜底无效）。img 已由 _make_image() 用 PIL 加载好。
        # 预览版使用独立名称，避免 Windows 把它与正式版的托盘实例混淆。
        icon_name = "yurun-pre" if os.environ.get("YURUN_PRE") == "1" else "yurun"
        self._icon = pystray.Icon(icon_name, img, title, menu)
        try:
            # Tk 需要占用主线程 mainloop；pystray 的 detached 入口会正确建立
            # Windows 消息循环，不再额外把阻塞的 run() 包进 daemon 线程。
            self._icon.run_detached()
            log.info("托盘图标已提交: name=%s title=%s", icon_name, title)
        except Exception as e:
            log.error("托盘图标启动失败: %s", e)

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
        except Exception as exc:
            log.exception("托盘菜单操作失败: %s", exc)

    def _invoke_menu(self, label, fn):
        log.info("托盘菜单点击: %s", label)
        self._safe(fn)

    def _mode_checked(self, mode):
        """菜单状态：输入模式始终互斥。"""
        try:
            from config import get_config
            return get_config().get("input_mode", "direct") == mode
        except Exception:
            return mode == "direct"

    def _refine_available(self):
        """智能整理依赖 API Key；快速输入始终可用。"""
        try:
            from config import get_config
            return bool(get_config().get("api_key"))
        except Exception:
            return False

    def _set_input_mode(self, mode):
        """设置输入模式并刷新互斥菜单。"""
        try:
            if self._on_set_input_mode:
                self._on_set_input_mode(mode)
        except Exception:
            pass
        # 刷新菜单勾选状态（pystray 在下次打开菜单时也会重读，但主动更新更即时）
        try:
            if self._icon is not None:
                self._icon.update_menu()
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
