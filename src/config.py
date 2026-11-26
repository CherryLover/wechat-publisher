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

from . import crypto

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
CONFIG_PATH = DATA_DIR / "config.json"

DEFAULTS = {
    "allow_registration": True,      # 是否开放新用户注册（管理员可在后台开关）
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
    # AI 配图：文章 → 1 封面 + 3 插图
    "ai_images": {
        "enabled": False,            # 全局开关，关了就完全不碰模型
        "base_url": "",              # OpenAI 兼容地址，如 https://xxx/v1
        "api_key": "",
        "text_model": "",            # 文生文：读文章出 4 条提示词
        "image_model": "",           # 生图模型（responses 方式下填承担生图工具的模型）
        "image_api": "auto",         # auto | images | responses
        "style_mode": "auto",        # auto：模型按文章自由设计风格 | fixed：固定下面那串
        "style": "",                 # fixed 模式的风格描述
        "write_to_notion": True,     # 生成的图回写 Notion（封面 + 插图块）
        "skip_if_has_images": False, # 文章正文已有配图时跳过 AI 规划与生图
        "skip_min_images": 2,        # 触发跳过的最少图片数量（默认 2 张）
        "max_width": 1200,           # 出图后压缩宽度
        "cover_size": "1536x1024",
        "illustration_size": "1536x1024",
    },
    # 固定密码登录：默认关闭，仅 Pocket ID 登录后可在设置页开启
    "password_login": {
        "enabled": False,
        "password_hash": "",
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
    """读配置，缺失的键用默认值补齐（自动解密平台 AI API Key）"""
    if not CONFIG_PATH.exists():
        return json.loads(json.dumps(DEFAULTS))
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return json.loads(json.dumps(DEFAULTS))
    merged = _deep_merge(DEFAULTS, raw)
    ai = merged.get("ai_images")
    if isinstance(ai, dict) and ai.get("api_key"):
        ai["api_key"] = crypto.decrypt_secret(str(ai["api_key"]))
    return merged


def save_config(cfg: dict) -> dict:
    """原子写入配置文件（落盘前加密平台 AI API Key）"""
    merged = _deep_merge(DEFAULTS, cfg)
    to_disk = json.loads(json.dumps(merged))
    ai_disk = to_disk.get("ai_images")
    if isinstance(ai_disk, dict) and ai_disk.get("api_key"):
        ai_disk["api_key"] = crypto.encrypt_secret(str(ai_disk["api_key"]))
    with _lock:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(to_disk, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(CONFIG_PATH)
    return merged


def get_theme() -> str:
    return load_config().get("theme") or "default"
