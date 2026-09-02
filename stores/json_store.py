"""JSON 落盘公共助手：每文件一把 RLock、原子写（tmp + os.replace）、损坏文件留底。

各 *_store.py 的 load/save 统一走这里，解决：
- 后台写作线程与 Flask 请求线程并发写同一 JSON 文件导致的丢更新/截断
- 写入中途进程被杀导致的文件损坏
- 损坏后静默返回空库（数据无声消失）—— 改为先把坏文件改名留底
"""

import functools
import json
import os
import threading
from datetime import datetime

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def lock_for(path):
    """每个文件路径对应一把可重入锁（mutator 内部嵌套调用 load/save 不会死锁）。"""
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(path, threading.RLock())


def synchronized(lock):
    """装饰器：把「读-改-写」整段包进锁内，保证单次 mutator 调用是原子的。"""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with lock:
                return fn(*args, **kwargs)
        return wrapper
    return deco


def read_json(path):
    """读取 JSON 文件。文件不存在/读取失败返回 None；
    JSON 损坏时把坏文件改名为 <文件>.corrupt-<时间戳> 留底后返回 None。"""
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        try:
            os.replace(path, "%s.corrupt-%s" % (path, datetime.now().strftime("%Y%m%d%H%M%S")))
        except OSError:
            pass
        return None
    except OSError:
        return None


def write_json(path, data):
    """原子写：先写临时文件再 os.replace，避免半截文件。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
