#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性脚本：让模型看一遍表情包，生成 2-4 字的备注。"""

import base64
import json
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from somnia.config import Config          # noqa: E402
from somnia.stickers import StickerBook   # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CFG = Config.from_env()
BOOK = StickerBook(ROOT / "stickers", meta_path=ROOT / "data" / "stickers.json")

PROMPT = ("这是微信表情包。用一个词（2-4 个汉字）概括它表达的情绪或动作，"
          "只输出这个词，不要标点、不要解释。")


def label(path: Path, retries: int = 2) -> str:
    b64 = base64.b64encode(path.read_bytes()).decode()
    mime = "image/gif" if path.suffix.lower() == ".gif" else "image/png"
    body = {
        "model": CFG.model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ]}],
        "max_tokens": 1500,
        "temperature": 0.3,
    }
    for _ in range(retries):
        try:
            req = urllib.request.Request(
                CFG.endpoint, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + CFG.api_key}, method="POST")
            with urllib.request.urlopen(req, timeout=90) as r:
                d = json.loads(r.read().decode())
                text = (d["choices"][0]["message"].get("content") or "").strip()
            text = text.strip("「」\"'。.!！?？ \n")
            if text:
                return text[:4]
        except Exception:
            pass
    return ""


def work(p: Path):
    note = label(p)
    if note:
        BOOK.set_note(p.name, note)
    return p.name, note


def main():
    todo = [p for p in BOOK.files() if not BOOK._meta().get(p.name)]
    print(f"待标注 {len(todo)} 张", flush=True)
    done = 0
    with ThreadPoolExecutor(max_workers=5) as pool:
        for name, note in pool.map(work, todo):
            done += 1
            print(f"[{done}/{len(todo)}] {name} -> {note or '(失败)'}", flush=True)
    print("完成", flush=True)


if __name__ == "__main__":
    main()
