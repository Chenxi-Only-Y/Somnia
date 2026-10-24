"""本地设置：面板写、程序读。

落盘位置 `data/settings.json`，只放"需要用户改的东西"：
性格、API、通道、对象。人设正文仍在 `persona/*.md`，这里只存路径与开关。
"""

import json
import sys
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
FILE = DATA_DIR / "settings.json"

DEFAULTS = {
    "name": "",
    "endpoint": "https://api.deepseek.com/chat/completions",
    "api_key": "",
    "model": "deepseek-flash",
    "channel": "cli",          # cli | claw | wechatauto
    "target": "",              # 微信：显示名（给 GUI 搜索）
    "target_id": "",           # 微信：wxid（读库只认这个）
    "account": "",             # 微信：用哪个账号发（wxid）
    "proactive": True,         # 允许她主动发消息
    "silence_hours": 6.0,      # 沉默多少小时后主动开口
    "image_gen": True,         # 允许她现场生成照片
    "prompt_persona": False,   # 人设正文要不要发进 prompt
}


def _warn(msg: str) -> None:
    try:
        print(f"[设置] {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


def load() -> dict:
    out = dict(DEFAULTS)
    try:
        raw = json.loads(FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return out
    except (OSError, json.JSONDecodeError) as e:
        # 手改坏了别静悄悄地退回默认值 —— 那样只会表现为"改了没反应"
        _warn(f"data/settings.json 读不了（{type(e).__name__}），这次全部用默认值")
        return out
    if not isinstance(raw, dict):
        _warn("data/settings.json 应该是一个对象，已忽略")
        return out
    for k in DEFAULTS:
        if k in raw and raw[k] is not None:
            out[k] = raw[k]
    return out


def save(patch: dict) -> dict:
    cur = load()
    for k, v in (patch or {}).items():
        if k in DEFAULTS and v is not None:
            cur[k] = v
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    return cur


def mask(key: str) -> str:
    if not key:
        return ""
    return key[:6] + "…" + key[-4:] if len(key) > 12 else "…"
