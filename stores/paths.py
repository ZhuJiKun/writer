"""项目路径常量：数据目录唯一定义处。

所有 *_store.py 的数据文件路径都基于 CONFIG_DIR（<项目根>/config/）。
"""

import os

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT_DIR, "config")
