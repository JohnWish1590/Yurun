"""Small Windows clipboard snapshot/restore helper.

The correction flow temporarily uses the system clipboard to copy a selection
and paste the replacement.  Tk's clipboard API only preserves text, so this
module snapshots the raw global-memory formats that Windows exposes and puts
them back after the transaction.  Formats that use non-global handles (for
example some delayed-rendered bitmap formats) are skipped safely.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
from dataclasses import dataclass

from logger import get_logger

log = get_logger("yurun.clipboard_transaction")

GMEM_MOVEABLE = 0x0002
MAX_FORMAT_BYTES = 64 * 1024 * 1024

# Only these clipboard formats are documented as HGLOBAL-backed text formats.
# Other formats (CF_BITMAP, CF_ENHMETAFILE, delayed-rendered and application
# private formats) may return handles that are not safe to pass through
# GlobalSize/GlobalLock/GlobalAlloc.  Treating those handles as raw bytes can
# corrupt the process heap during a restore, which is much worse than skipping
# automatic correction for a non-text clipboard.
CF_TEXT = 1
CF_DIB = 8
CF_OEMTEXT = 7
CF_UNICODETEXT = 13
CF_LOCALE = 16
CF_DIBV5 = 17
SAFE_FORMATS = frozenset({CF_TEXT, CF_OEMTEXT, CF_UNICODETEXT, CF_LOCALE,
                          CF_DIB, CF_DIBV5})

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wintypes.BOOL
user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
user32.EnumClipboardFormats.restype = wintypes.UINT
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE

kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalSize.restype = ctypes.c_size_t
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.restype = wintypes.HGLOBAL


@dataclass(frozen=True)
class ClipboardSnapshot:
    formats: tuple[tuple[int, bytes], ...]


def capture_clipboard() -> ClipboardSnapshot | None:
    """Capture currently available global-memory clipboard formats."""
    if not user32.OpenClipboard(None):
        log.debug("无法打开剪贴板进行备份")
        return None
    formats: list[tuple[int, bytes]] = []
    try:
        fmt = 0
        while True:
            fmt = int(user32.EnumClipboardFormats(fmt))
            if not fmt:
                break
            if fmt not in SAFE_FORMATS:
                continue
            handle = user32.GetClipboardData(fmt)
            if not handle:
                continue
            size = int(kernel32.GlobalSize(handle))
            if size <= 0 or size > MAX_FORMAT_BYTES:
                continue
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                continue
            try:
                formats.append((fmt, ctypes.string_at(pointer, size)))
            finally:
                kernel32.GlobalUnlock(handle)
        # If the clipboard only contains non-text/private formats, cancel the
        # correction transaction instead of emptying it and risking data loss.
        return ClipboardSnapshot(tuple(formats)) if formats else None
    except Exception as exc:
        log.warning("剪贴板格式备份失败: %s", exc)
        return None
    finally:
        user32.CloseClipboard()


def restore_clipboard(snapshot: ClipboardSnapshot) -> bool:
    """Restore a previously captured snapshot, including non-text formats."""
    if not isinstance(snapshot, ClipboardSnapshot):
        return False
    if not user32.OpenClipboard(None):
        log.debug("无法打开剪贴板进行还原")
        return False
    allocated: list[wintypes.HGLOBAL] = []
    try:
        if not user32.EmptyClipboard():
            return False
        for fmt, data in snapshot.formats:
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not handle:
                continue
            allocated.append(handle)
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                continue
            try:
                ctypes.memmove(pointer, data, len(data))
            finally:
                kernel32.GlobalUnlock(handle)
            if user32.SetClipboardData(fmt, handle):
                # Windows owns a successful SetClipboardData handle.
                allocated.remove(handle)
        return True
    except Exception as exc:
        log.warning("剪贴板格式还原失败: %s", exc)
        return False
    finally:
        for handle in allocated:
            kernel32.GlobalFree(handle)
        user32.CloseClipboard()
