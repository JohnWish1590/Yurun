"""最小验证：确认 Tk 可以在不声明 DPI Aware 模式下，
通过子类化 WndProc 拦截 WM_DPICHANGED(0x02E0) 并还原 tk scaling，
且不崩、窗口正常显示。

仅验证机制，不碰语润源码。
"""
import ctypes
import ctypes.wintypes as wt
import tkinter as tk

GWL_WNDPROC = -4
WM_DPICHANGED = 0x02E0

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# 不声明 SetProcessDpiAwareness，回到 v1.0 模式
root = tk.Tk()
root.title("DPI verify")
root.geometry("320x120")

label = tk.Label(root, text="orig scaling: ?", font=("Microsoft YaHei", 14))
label.pack(padx=20, pady=20)


def get_scaling():
    return float(root.tk.call('tk', 'scaling'))


orig = get_scaling()
label.config(text="orig scaling: %.4f" % orig)
print("[verify] orig scaling =", orig)

# ---- 子类化 WndProc ----
hwnd = wt.HWND(int(root.wm_frame(), 16))
old_wndproc = ctypes.c_longlong(user32.GetWindowLongPtrW(hwnd, GWL_WNDPROC))

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM)

hit = {"count": 0}


def new_wndproc(h, msg, wp, lp):
    if msg == WM_DPICHANGED:
        hit["count"] += 1
        cur = get_scaling()
        if abs(cur - orig) > 1e-3:
            root.tk.call('tk', 'scaling', orig)
            print("[verify] WM_DPICHANGED 收到, scaling %.4f -> %.4f (restored)" % (cur, orig))
        else:
            print("[verify] WM_DPICHANGED 收到, scaling 未漂, 无需还原")
        return 0  # 我们处理了，阻止 Tk 默认重算
    # 其余消息交给原 WndProc
    return user32.CallWindowProcW(old_wndproc, h, msg, wp, lp)


proc_ref = WNDPROC(new_wndproc)  # 必须保活
user32.SetWindowLongPtrW(hwnd, GWL_WNDPROC, ctypes.cast(proc_ref, ctypes.c_longlong))
print("[verify] WndProc 子类化完成, hwnd=", hwnd)

# 模拟：手动把 scaling 改大，再调还原逻辑（验证 _restore 本身有效）
root.tk.call('tk', 'scaling', orig * 1.5)
print("[verify] 模拟漂移到 %.4f" % get_scaling())
root.tk.call('tk', 'scaling', orig)
print("[verify] 手动还原后 %.4f (应=orig)" % get_scaling())

# 每 1.5s 周期兜底（同 _watch_dpi_drift）
def tick():
    cur = get_scaling()
    if abs(cur - orig) > 1e-3:
        root.tk.call('tk', 'scaling', orig)
        print("[verify] tick 还原 %.4f -> %.4f" % (cur, orig))
    root.after(1500, tick)


root.after(1500, tick)


def on_close():
    # 还原 WndProc 再销毁（避免野指针）
    try:
        user32.SetWindowLongPtrW(hwnd, GWL_WNDPROC, old_wndproc)
    except Exception:
        pass
    root.destroy()


root.protocol("WM_DELETE_WINDOW", on_close)
print("[verify] 窗口已显示，关闭即退出。若期间不崩、关闭正常，则机制可行。")
root.mainloop()
print("[verify] mainloop 结束, WM_DPICHANGED 命中次数:", hit["count"])
