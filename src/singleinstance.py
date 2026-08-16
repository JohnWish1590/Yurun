"""单实例锁：启动时杀掉旧实例并接管，保证永远只有一个 Yurun 进程。

设计要点：
- 用 %APPDATA%/Yurun/yurun.pid 记录当前实例 PID。
- 启动时读旧 PID；校验它还活着且进程名属于 {yurun.exe, python.exe, pythonw.exe}
  才杀（防止 PID 被回收后误杀别的程序）。
- 杀完等 ~0.6s 让旧实例释放全局热键，再返回，让新实例接着 RegisterHotKey。
- 这样哪怕旧实例托盘图标没了(崩溃/没显示)，用户再双击一次也能把旧的收掉、
  新实例接管——永远不会被"旧僵尸占着热键、新的注册失败、没法用"卡死。
"""
import ctypes
import ctypes.wintypes as wt
import os
import time

from logger import get_logger
from config import app_data_dir

log = get_logger("yurun.singleinstance")

kernel32 = ctypes.windll.kernel32
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CloseHandle.restype = wt.BOOL
kernel32.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
kernel32.TerminateProcess.restype = wt.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wt.BOOL

PROCESS_QUERY_LIMITED_INFO = 0x1000
PROCESS_TERMINATE = 0x0001
_YURUN_PROCESS_NAMES = {"yurun.exe", "python.exe", "pythonw.exe"}


def _process_image_name(pid: int) -> str:
    """返回 pid 对应的可执行文件名(小写)；进程不存在或取不到返回 ''。"""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFO, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        kernel32.CloseHandle(h)


def _write_pid(pid_file):
    try:
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
    except Exception as e:
        log.warning("写 PID 文件失败: %s", e)


def kill_old_and_takeover() -> bool:
    """启动时杀掉旧实例(如有)并登记自己。返回是否杀了旧实例。"""
    pid_file = app_data_dir() / "yurun.pid"
    if not pid_file.exists():
        _write_pid(pid_file)
        return False
    try:
        old_pid = int(pid_file.read_text(encoding="utf-8").strip())
    except Exception:
        _write_pid(pid_file)
        return False
    if old_pid == os.getpid():
        return False
    name = _process_image_name(old_pid)
    if not name:
        # 旧进程已不在
        _write_pid(pid_file)
        return False
    if name not in _YURUN_PROCESS_NAMES:
        # PID 被别的程序回收了，不杀
        log.info("旧 PID %s 现属 %s，非 Yurun，跳过", old_pid, name)
        _write_pid(pid_file)
        return False
    h = kernel32.OpenProcess(PROCESS_TERMINATE, False, old_pid)
    if not h:
        _write_pid(pid_file)
        return False
    try:
        ok = bool(kernel32.TerminateProcess(h, 1))
    finally:
        kernel32.CloseHandle(h)
    if ok:
        log.info("已结束旧实例 PID=%s (%s)，新实例接管", old_pid, name)
        time.sleep(0.6)  # 等旧实例释放全局热键
    _write_pid(pid_file)
    return ok


# ---------- 按进程名枚举的兜底：清理无 PID 文件的旧 exe 僵尸 ----------
TH32CS_SNAPPROCESS = 0x00000002


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", wt.WCHAR * 260),
    ]


kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
kernel32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32FirstW.restype = wt.BOOL
kernel32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.restype = wt.BOOL


def kill_other_yurun_exe() -> int:
    """枚举进程，杀掉所有名为 yurun.exe 的进程(除自己)。

    兜底用：旧版本 exe(如 9288 僵尸)没写过 PID 文件，PID 文件法找不到它；
    这里按进程名枚举，确保把任何旧 exe 僵尸都收掉。dev 模式(python.exe 跑
    main.py)由 PID 文件法处理；本函数只针对打包后的 yurun.exe。
    """
    me = os.getpid()
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap:
        return 0
    killed = 0
    pe = PROCESSENTRY32W()
    pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    try:
        if not kernel32.Process32FirstW(snap, ctypes.byref(pe)):
            return 0
        while True:
            name = pe.szExeFile.lower()
            pid = pe.th32ProcessID
            if name == "yurun.exe" and pid != me:
                h = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
                if h:
                    try:
                        if kernel32.TerminateProcess(h, 1):
                            killed += 1
                            log.info("已结束旧 Yurun.exe PID=%s", pid)
                    finally:
                        kernel32.CloseHandle(h)
            if not kernel32.Process32NextW(snap, ctypes.byref(pe)):
                break
    finally:
        kernel32.CloseHandle(snap)
    if killed:
        time.sleep(0.6)  # 等旧实例释放全局热键
    return killed

