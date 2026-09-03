"""一次语音输入的不可变上下文。

把每次按下热键时确定的信息放进独立对象，避免下一次录音覆盖上一句的
目标窗口、松键时间或 ASR 时间线。
"""
from dataclasses import dataclass, field
import threading
import time


@dataclass
class VoiceSession:
    """一次按下→说话→松开→输入的会话上下文。"""

    round_id: int
    target_hwnd: int | None
    stop_event: threading.Event
    config: dict
    # 高权限输入助手在热键按下时分配的会话标识；普通路径为 None。
    # 它只用于把最终文字送回同一次按键锁定的前台窗口，不保存任何文本。
    helper_session_id: str | None = None
    started_at: float = field(default_factory=time.perf_counter)
    keyup_at: float | None = None
    timeline: dict = field(default_factory=dict)

    def mark_keyup(self) -> float:
        """只记录一次松键时刻，供 TTFI/TTCI 计算。"""
        if self.keyup_at is None:
            self.keyup_at = time.perf_counter()
        return self.keyup_at
