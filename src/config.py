"""
运行时配置：落在服务器 data/config.json

结构：
{
  "theme": "default",
  "notify": {
    "enabled": false,
    "bark": {"key": "", "server": "https://api.day.app", "group": "公众号发布"},
    "events": {"start": true, "success": true, "error": true}
  }
}
"""

import json
import os
import threading
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
CONFIG_PATH = DATA_DIR / "config.json"

DEFAULTS = {
    "theme": "default",
    "notify": {
        "enabled": False,
        "bark": {
            "key": "",
            "server": "https://api.day.app",
            "group": "公众号发布",
        },
        "events": {"start": True, "success": True, "error": True},
    },
}

_lock = threading.Lock()


def _deep_merge(base: dict, override: dict) -> dict:
    """用 override 覆盖 base，dict 递归合并"""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    """读配置，缺失的键用默认值补齐"""
    if not CONFIG_PATH.exists():
        return json.loads(json.dumps(DEFAULTS))
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return json.loads(json.dumps(DEFAULTS))
    return _deep_merge(DEFAULTS, raw)


def save_config(cfg: dict) -> dict:
    """原子写入配置文件"""
    merged = _deep_merge(DEFAULTS, cfg)
    with _lock:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(CONFIG_PATH)
    return merged


def get_theme() -> str:
    return load_config().get("theme") or "default"
