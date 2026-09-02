"""AI 聊天助手的会话历史持久化：config/chat_history.json。

只有一条全局会话，支持追加与清空；落盘历史设上限，超出截掉最旧消息。
"""

import os
from datetime import datetime

from stores.json_store import lock_for, read_json, synchronized, write_json
from stores.paths import CONFIG_DIR

HISTORY_PATH = os.path.join(CONFIG_DIR, "chat_history.json")
_LOCK = lock_for(HISTORY_PATH)

# 落盘历史上限（条）；发送给 LLM 的上下文窗口条数
MAX_STORED = 200
CONTEXT_TAIL = 40


def load_history(tail=0):
    """读取历史消息列表 [{role, content, time}]；tail>0 时只取最近 tail 条。"""
    data = read_json(HISTORY_PATH)
    msgs = data.get("messages", []) if isinstance(data, dict) else []
    msgs = [m for m in msgs if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
    return msgs[-tail:] if tail > 0 else msgs


@synchronized(_LOCK)
def append_turn(user_msg, assistant_msg):
    """锁内追加一轮对话并落盘，超出上限截掉最旧。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msgs = load_history()
    msgs.append({"role": "user", "content": user_msg, "time": now})
    msgs.append({"role": "assistant", "content": assistant_msg, "time": now})
    write_json(HISTORY_PATH, {"messages": msgs[-MAX_STORED:]})


@synchronized(_LOCK)
def clear_history():
    write_json(HISTORY_PATH, {"messages": []})
