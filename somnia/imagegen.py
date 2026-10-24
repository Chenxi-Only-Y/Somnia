"""本地生图：她说"想给你看窗外"，库里没有就现场画一张。

走本机的 local-gen 服务（Z-Image-Turbo），不花钱、不联网。
出图约 25-45 秒，所以调用方必须放后台线程。
"""

import json
import random
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8100/v1/images/generations"

# 关键：要像"手机随手拍"，不是插画。这些词决定了它像不像她的生活。
STYLE = "，手机随手拍的，生活感，轻微噪点，构图随意，不像专业摄影，没有文字水印"


def build_prompt(desc: str) -> str:
    return (desc or "").strip() + STYLE


def generate(desc: str, url: str = DEFAULT_URL, width: int = 768,
             height: int = 1024, seed: int = None, timeout: int = 300) -> bytes:
    """生成一张图，返回 PNG 字节。失败抛异常。"""
    body = {
        "prompt": build_prompt(desc),
        "width": width, "height": height,
        "num_inference_steps": 8, "cfg_scale": 1.0,
        "seed": seed if seed is not None else random.randint(1, 10**6),
    }
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.loads(r.read().decode())
    img_url = payload["data"][0]["url"]
    with urllib.request.urlopen(img_url, timeout=120) as r:
        return r.read()


def health(url: str = DEFAULT_URL, timeout: int = 5) -> bool:
    base = url.rsplit("/v1/", 1)[0]
    try:
        with urllib.request.urlopen(base + "/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False
