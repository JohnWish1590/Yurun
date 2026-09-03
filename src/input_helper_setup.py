"""安装或移除语润高权限输入助手的登录任务。

这是安装器调用的一次性管理程序。语润主程序本身始终按普通权限运行；
只有助手在登录后以高权限后台运行，用于向同样以高权限运行的软件输入文字。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

TASK_NAME = "Yurun Input Helper"


def _pythonw() -> Path:
    current = Path(sys.executable)
    candidate = current.with_name("pythonw.exe")
    return candidate if candidate.exists() else current


def _task_command() -> str:
    if getattr(sys, "frozen", False):
        helper = Path(sys.executable).with_name("YurunInputHelper.exe")
        return f'"{helper}"'
    launcher = Path(__file__).with_name("privileged_helper.py")
    return f'"{_pythonw()}" "{launcher}"'


def install() -> int:
    user_name = os.environ.get("USERNAME", "")
    if not user_name:
        print("无法确定当前 Windows 用户，未创建输入助手任务。")
        return 1
    command = [
        "schtasks", "/Create", "/TN", TASK_NAME, "/TR", _task_command(),
        "/SC", "ONLOGON", "/RL", "HIGHEST", "/IT", "/RU", user_name, "/F",
    ]
    created = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if created.returncode != 0:
        print(created.stderr or created.stdout or "无法创建后台输入助手")
        return created.returncode or 1
    launched = subprocess.run(["schtasks", "/Run", "/TN", TASK_NAME], capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
    if launched.returncode != 0:
        print(launched.stderr or launched.stdout or "任务已创建，但当前启动失败")
        return launched.returncode or 1
    print("输入助手已启用。日常直接启动语润即可，无需每次管理员运行。")
    return 0


def uninstall() -> int:
    deleted = subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True,
                             text=True, encoding="utf-8", errors="replace")
    if deleted.returncode != 0:
        print(deleted.stderr or deleted.stdout or "未找到输入助手任务")
        return deleted.returncode or 1
    print("输入助手已停用并移除。")
    return 0


if __name__ == "__main__":
    action = (sys.argv[1] if len(sys.argv) > 1 else "install").lower()
    raise SystemExit(uninstall() if action == "uninstall" else install())
