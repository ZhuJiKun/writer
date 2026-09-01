"""模型配置的本地持久化：读写 config.json、预设模板、key 掩码。"""

import os

from json_store import lock_for, read_json, synchronized, write_json

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
_LOCK = lock_for(CONFIG_PATH)

PRESETS = {
    "custom": {"name": "自定义", "base_url": "", "model_hint": "模型名，如 my-model"},
    "openai": {"name": "OpenAI", "base_url": "https://api.openai.com/v1", "model_hint": "如 gpt-4o / gpt-4o-mini"},
    "deepseek": {"name": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "model_hint": "如 deepseek-chat / deepseek-reasoner"},
    "moonshot": {"name": "Kimi（Kimi Code 订阅）", "base_url": "https://api.kimi.com/coding/v1", "model_hint": "如 k3 / kimi-for-coding"},
    "qwen": {"name": "通义千问", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model_hint": "如 qwen-plus / qwen-max"},
    "glm": {"name": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4", "model_hint": "如 glm-4-plus / glm-4-air"},
}

SLOTS = {
    "generation": {"name": "正文生成", "desc": "旗舰模型，负责章节正文写作，质量优先"},
    "critic": {"name": "审校 Critic", "desc": "中档模型，负责一致性 / 逻辑 / 文风检查"},
    "extraction": {"name": "状态抽取 / 摘要", "desc": "便宜小模型，负责事实抽取与章节压缩，成本优先"},
}

DEFAULT_SLOT = {"preset": "custom", "base_url": "", "api_key": "", "model": "", "inherit": False}


def default_config():
    return {
        "slots": {
            "generation": dict(DEFAULT_SLOT),
            "critic": {**DEFAULT_SLOT, "inherit": True},
            "extraction": {**DEFAULT_SLOT, "inherit": True},
        }
    }


def load_config():
    cfg = read_json(CONFIG_PATH)
    if not isinstance(cfg, dict):
        return default_config()
    base = default_config()
    for slot, defaults in base["slots"].items():
        defaults.update(cfg.get("slots", {}).get(slot, {}))
    return base


@synchronized(_LOCK)
def save_slot(slot, data):
    """保存单个槽位；api_key 留空时保留旧值。"""
    cfg = load_config()
    old = cfg["slots"].get(slot, dict(DEFAULT_SLOT))
    merged = {**old, **data}
    if not data.get("api_key"):
        merged["api_key"] = old.get("api_key", "")
    cfg["slots"][slot] = merged
    write_json(CONFIG_PATH, cfg)
    return merged


def effective_slot(cfg, slot):
    """解析 inherit：跟随正文生成配置。"""
    s = cfg["slots"][slot]
    if slot != "generation" and s.get("inherit"):
        gen = cfg["slots"]["generation"]
        return {**gen, "inherit": True}
    return s


def mask_key(key):
    if not key:
        return ""
    if len(key) <= 8:
        return key[:2] + "***"
    return key[:4] + "***" + key[-4:]
