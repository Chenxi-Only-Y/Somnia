"""OpenClaw Weixin 通道（腾讯官方 @tencent-weixin/openclaw-weixin）。

协议路径已在真机核实（2026-10-20）
--------------------------------
    POST /ilink/bot/msg/notifystart   订阅消息   → {"ret":0}
    POST /ilink/bot/getupdates        长轮询     → {"msgs":[],"sync_buf":...,"get_updates_buf":...}
    POST /ilink/bot/sendmessage       发消息
    POST /ilink/bot/sendtyping        正在输入   {"status":1|2}
    POST /ilink/bot/getconfig         会话参数

关键点
------
- 除 notifystart 外，机器人接口都要带 `ilink_user_id`（登录时拿到的微信 id），
  缺了会返回 {"ret":-2,"errmsg":"ilink_user_id required"}
- HTTP 200 不代表成功，必须看 ret / errcode
- ret == -14 表示票据失效，需要重新扫码
- sendmessage 必须带「当前入站消息」的 context_token 和一个唯一 client_id

未在真机验证的部分（消息体字段名），原始帧落盘 data/claw_raw.jsonl。
"""

import base64
import json
import queue
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASE = "https://ilinkai.weixin.qq.com"
APP_ID = "bot"
CLIENT_VERSION = "132102"
CHANNEL_VERSION = "2.4.6"
BOT_AGENT = "somnia/0.1.0 (python)"

# 已核实的接口路径
P_NOTIFYSTART = "/ilink/bot/msg/notifystart"
P_GETUPDATES = "/ilink/bot/getupdates"
P_SENDMESSAGE = "/ilink/bot/sendmessage"
P_SENDTYPING = "/ilink/bot/sendtyping"
P_GETCONFIG = "/ilink/bot/getconfig"

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
CRED_FILE = DATA_DIR / "claw_token.json"

# 推送帧里可能承载文本的字段名（按可能性排序）
TEXT_KEYS = ("text", "content", "msg", "message", "asr", "voice_text")
CONTAINER_KEYS = ("msg", "message", "data", "payload", "body", "content")
ID_KEYS = ("msg_id", "id", "client_id", "server_id", "msgid")
CTX_KEYS = ("context_token", "ctx_token", "context")
BUF_KEYS = ("get_updates_buf", "sync_buf", "buf", "cursor", "next_buf")
LIST_KEYS = ("msgs", "messages", "msg_list", "updates", "items")


class ClawError(RuntimeError):
    def __init__(self, ret, payload):
        self.ret = ret
        self.payload = payload
        super().__init__(f"claw ret={ret}: {json.dumps(payload, ensure_ascii=False)[:200]}")


class ClawAuthExpired(ClawError):
    """ret == -14：票据失效，需要重新扫码。"""


@dataclass
class Message:
    text: str
    msg_id: str = ""
    context_token: str = ""
    raw: dict = None


def _append_jsonl(name: str, obj) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(DATA_DIR / name, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except OSError:
        pass


def load_credentials(path: Path = None):
    """从扫码结果里读 (bot_token, ilink_user_id)。"""
    p = path or CRED_FILE
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", ""
    return d.get("bot_token", ""), d.get("ilink_user_id", "")


def random_uin() -> int:
    return random.randint(1, 2**31 - 1)


def build_headers(token: str, uin: int = None) -> dict:
    uin = uin if uin is not None else random_uin()
    return {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": base64.b64encode(str(uin).encode()).decode(),
        "iLink-App-Id": APP_ID,
        "iLink-App-ClientVersion": CLIENT_VERSION,
        "Authorization": f"Bearer {token}",
    }


def build_body(payload: dict) -> dict:
    body = dict(payload or {})
    body.setdefault("base_info", {
        "channel_version": CHANNEL_VERSION,
        "bot_agent": BOT_AGENT,
    })
    return body


def check(resp: dict) -> dict:
    if not isinstance(resp, dict):
        return {}
    ret = resp.get("ret", resp.get("errcode", 0))
    if ret in (0, None, "0"):
        return resp
    if int(ret) == -14:
        raise ClawAuthExpired(ret, resp)
    raise ClawError(ret, resp)


class UrllibTransport:
    """零依赖 HTTP。测试时可替换成假实现。"""

    def __init__(self, base: str = DEFAULT_BASE, timeout: int = 45):
        self.base = base.rstrip("/")
        self.timeout = timeout

    def post(self, path: str, body: dict, headers: dict, params: dict = None) -> dict:
        if not path.startswith("/"):
            path = "/" + path
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(build_body(body)).encode()
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            text = ""
            try:
                text = e.read().decode()[:300]
            except Exception:
                pass
            _append_jsonl("claw_raw.jsonl",
                          {"ts": time.time(), "path": path, "http": e.code, "body": text})
            raise ClawError(e.code, {"http_error": text})


class ClawClient:
    def __init__(self, token: str, transport=None, user_id: str = ""):
        self.token = token
        self.user_id = user_id
        self.transport = transport or UrllibTransport()

    def _post(self, path: str, body: dict) -> dict:
        body = dict(body or {})
        if self.user_id:
            body.setdefault("ilink_user_id", self.user_id)
        return check(self.transport.post(path, body, build_headers(self.token)))

    # ── 机器人接口 ────────────────────────────────────────

    def notifystart(self) -> dict:
        return self._post(P_NOTIFYSTART, {})

    def getupdates(self, buf: str = "", timeout_ms: int = 25000) -> dict:
        return self._post(P_GETUPDATES, {
            "get_updates_buf": buf or "",
            "longpolling_timeout_ms": timeout_ms,
        })

    def sendmessage(self, text: str, context_token: str, client_id: str = None,
                    to_user_id: str = None) -> dict:
        """发送一条文本。

        真机验证的载荷形状（2026-10-20）：出站消息和入站对称，
        文本放在 item_list[].text_item.text，而不是顶层的 text 字段。
        顶层放 text 会得到 {"ret":-2,"errmsg":"invalid arguments"}。
        """
        return self._post(P_SENDMESSAGE, {
            "msg": {
                "context_token": context_token,
                "client_id": client_id or uuid.uuid4().hex,
                "to_user_id": to_user_id or self.user_id,
                "message_type": 1,
                "item_list": [{
                    "type": 1,
                    "text_item": {"text": text},
                }],
            }
        })

    def sendtyping(self, status: int, context_token: str = "") -> dict:
        body = {"status": status}
        if context_token:
            body["context_token"] = context_token
        return self._post(P_SENDTYPING, body)

    def getconfig(self) -> dict:
        return self._post(P_GETCONFIG, {})


# ── 解析（容错优先）────────────────────────────────────────

def _first(d: dict, keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, (str, int)) and str(v).strip():
            return str(v)
    return ""


def _deep_text(obj, depth: int = 0) -> str:
    """在未知结构里找文本。

    只往已知容器键里递归 —— 否则 {"image":"x"} 里的文件名会被当成消息文本。
    """
    if depth > 2:
        return ""
    if isinstance(obj, str):
        return obj.strip()
    if isinstance(obj, dict):
        for k in TEXT_KEYS:
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for k in CONTAINER_KEYS:
            v = obj.get(k)
            if isinstance(v, (dict, list)):
                got = _deep_text(v, depth + 1)
                if got:
                    return got
        return ""
    if isinstance(obj, list):
        for v in obj:
            got = _deep_text(v, depth + 1)
            if got:
                return got
    return ""


def parse_updates(resp: dict):
    """返回 (messages, new_buf)。

    真机帧结构（2026-10-20 实测）：
        msgs[].item_list[].text_item.text   文本
        msgs[].item_list[].msg_id           消息 id
        msgs[].context_token                回复凭据
        msgs[].message_type == 1            文本类型
    认不出来就返回空，由调用方落盘。
    """
    if not isinstance(resp, dict):
        return [], ""

    buf = _first(resp, BUF_KEYS)

    items = []
    for k in LIST_KEYS:
        v = resp.get(k)
        if isinstance(v, list):
            items = v
            break

    msgs = []
    for item in items:
        m = _parse_one(item)
        if m:
            msgs.append(m)
    return msgs, buf


def _parse_one(item) -> "Message | None":
    if not isinstance(item, dict):
        return None

    # 文本：优先 item_list[].text_item.text
    text = ""
    item_list = item.get("item_list")
    if isinstance(item_list, list):
        for it in item_list:
            if not isinstance(it, dict):
                continue
            ti = it.get("text_item")
            if isinstance(ti, dict):
                t = ti.get("text")
                if isinstance(t, str) and t.strip():
                    text = t.strip()
                    break
    if not text:
        text = _deep_text(item)
    if not text:
        return None

    # 消息 id：优先条目内的 msg_id，退到 message_id / client_id
    msg_id = ""
    if isinstance(item_list, list):
        for it in item_list:
            if isinstance(it, dict):
                mid = _first(it, ("msg_id",))
                if mid:
                    msg_id = mid
                    break
    if not msg_id:
        msg_id = _first(item, ("message_id",) + ID_KEYS)

    return Message(
        text=text,
        msg_id=msg_id,
        context_token=_first(item, CTX_KEYS),
        raw=item,
    )


# ── 通道 ─────────────────────────────────────────────────

class ClawChannel:
    """长轮询放后台线程，主线程按 Channel 契约取消息。"""

    # sendmessage 必须带 context_token，没有入站消息就发不出去
    can_initiate = False

    def __init__(self, token: str, transport=None, alert_hook=None,
                 timeout_ms: int = 25000, user_id: str = ""):
        self.client = ClawClient(token, transport, user_id)
        self.alert = alert_hook or (lambda m: None)
        self.timeout_ms = timeout_ms
        self.inbox: "queue.Queue[Message]" = queue.Queue()
        self._buf = ""
        self._ctx = ""
        self._closed = False
        self._stop = False
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def _loop(self):
        try:
            self.client.notifystart()
        except Exception as e:
            self.alert(f"notifystart 失败：{e}")

        backoff = 1.0
        while not self._stop:
            try:
                resp = self.client.getupdates(self._buf, self.timeout_ms)
                msgs, buf = parse_updates(resp)
                if buf:
                    self._buf = buf
                for m in msgs:
                    if m.context_token:
                        self._ctx = m.context_token
                    self.inbox.put(m)
                if not msgs:
                    _append_jsonl("claw_raw.jsonl",
                                  {"ts": time.time(), "note": "no-msg", "resp": resp})
                backoff = 1.0
            except ClawAuthExpired as e:
                self.alert(f"票据失效，需要重新扫码：{e}")
                self._stop = True
            except Exception as e:
                self.alert(f"轮询异常：{type(e).__name__}: {e}")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    # ── Channel 契约 ──────────────────────────────────────

    def recv(self):
        if self._closed:
            return None
        return self._wrap(self.inbox.get())

    def recv_timeout(self, seconds: float):
        if self._closed:
            return None
        try:
            m = self.inbox.get(timeout=seconds)
        except queue.Empty:
            return None
        return self._wrap(m)

    def _wrap(self, m):
        from .base import Inbound
        if m is None:                 # 关闭哨兵：关闭是粘性的
            self._closed = True
            return None
        return Inbound(text=m.text, msg_id=m.msg_id or None)

    def send(self, text: str) -> None:
        if not self._ctx:
            raise RuntimeError("还没有 context_token：收到第一条消息前无法回复")
        self.client.sendmessage(text, self._ctx)

    def typing(self, on: bool) -> None:
        try:
            self.client.sendtyping(1 if on else 2, self._ctx)
        except Exception:
            pass

    def close(self):
        self._stop = True
        self._closed = True
        self.inbox.put(None)
