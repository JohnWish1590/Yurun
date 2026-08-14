"""语润（Yurun）托盘模块：系统托盘图标 + 菜单。
pystray 在后台线程运行；notify 也线程安全（内部投递）。
"""
import os
import threading

import pystray
from PIL import Image, ImageDraw
from logger import logs_dir


class Tray:
    def __init__(self, on_quit=None, on_open_settings=None):
        self._icon = None
        self._on_quit = on_quit
        self._on_open_settings = on_open_settings
        self._lock = threading.Lock()

    @staticmethod
    def _make_image(size=64):
        """精致麦克风图标：胶囊头 + 支架 + U 形底座，亮色描边、干净不花哨。"""
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        cx = size // 2
        # 麦克风头（圆角竖胶囊）
        head_w = size * 0.34
        head_top = size * 0.16
        head_bot = size * 0.52
        d.rounded_rectangle(
            [cx - head_w / 2, head_top, cx + head_w / 2, head_bot],
            radius=head_w / 2, fill=(65, 105, 225, 255),
            outline=(173, 200, 255, 255), width=max(2, size // 32))
        # 支架（竖直杆）
        stem_w = max(3, size * 0.07)
        d.rectangle([cx - stem_w / 2, head_bot - 2, cx + stem_w / 2, size * 0.70],
                    fill=(173, 200, 255, 255))
        # 底座弧（U 形支架）
        bw = size * 0.30
        d.arc([cx - bw, size * 0.62, cx + bw, size * 0.62 + bw],
              start=20, end=160, fill=(173, 200, 255, 255), width=max(3, size // 22))
        return img

    def start(self, title="语润 Yurun"):
        img = self._make_image()
        menu = pystray.Menu(
            pystray.MenuItem("打开设置", lambda: self._safe(self._on_open_settings)),
            pystray.MenuItem("打开日志目录", lambda: self._safe(self._open_logs)),
            pystray.MenuItem("退出", lambda: self._safe(self._on_quit)),
        )
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

    def notify(self, message: str, title: str = "语润 Yurun"):
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
