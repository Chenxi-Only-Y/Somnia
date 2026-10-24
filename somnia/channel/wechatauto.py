"""微信 4.x 桌面通道（基于 wechatauto-replica）。

为什么不是原来的 wxauto 思路
----------------------------
微信 4.x 的聊天界面是**自绘**的，没有可访问性节点，所以：

- **读**：不碰界面，直接解密本地 SQLCipher 数据库（`xwechat_files/<wxid>/db_storage/`）
- **写**：驱动窗口 —— 热激活 UIA 树（往 Weixin.dll 内存写一个字节）+ OCR 定位 + 剪贴板粘贴

由此产生两条与 Claw 不同的硬事实：

1. **读不需要窗口**，窗口锁着也能收消息。
2. **写必须要窗口**，锁屏 / 最小化时发送会失败。

需要知道的风险
--------------
热激活是对 `Weixin.dll` 的一次内存写入（改一个字节），本质上是内存补丁，
比"纯辅助功能自动化"更接近外挂。加上驱动窗口本身，都属于违反服务协议的行为。
建议只用一次性小号。

依赖（惰性导入，测试时不需要装）
--------------------------------
    pip install wechatauto-replica     # 提供 wechatauto 包
"""

import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"

TEXT_TYPES = ("文本", "text")
MAX_TEXT = 2000

# 发送节奏：(抬窗后等待, 开会话后等待, 重试间隔)。测试可整体改成 0。
DEFAULT_PACE = (0.15, 0.05, 1.0)
# 发送后回查库的超时。测试可设 0 跳过等待。
DEFAULT_VERIFY_TIMEOUT = 2.5


@dataclass
class RawMsg:
    text: str
    key: str              # (shard, local_id) 组成的稳定键
    self_sent: bool
    ts: float = 0.0       # 消息时间（秒），用于重启后补回停机期间的消息


def _get(obj, *names, default=None):
    for n in names:
        if isinstance(obj, dict):
            if n in obj and obj[n] is not None:
                return obj[n]
        else:
            v = getattr(obj, n, None)
            if v is not None:
                return v
    return default


def msg_key(m) -> str:
    shard = str(_get(m, "shard", "db", default=""))
    lid = _get(m, "local_id", "id", "server_id", default="")
    return f"{shard}:{lid}"


def msg_text(m) -> str:
    return str(_get(m, "content", "text", "msg", default="") or "").strip()[:MAX_TEXT]


def is_text(m) -> bool:
    """真机字段：type 是中文类型名（'文本' / '图片' / '语音' …），不是整数。"""
    t = _get(m, "type", "local_type", default="文本")
    if isinstance(t, str):
        return t.strip() in TEXT_TYPES
    try:
        return int(t) == 1
    except (TypeError, ValueError):
        return False


def select_replay(msgs, since_ts: float, now: float = None,
                  limit: int = 8, age: float = 12 * 3600):
    """重启时该补回哪些消息：停机之后收到的、不太旧的、最近 limit 条。

    否则重启一次她就"断片"——你中途说的话她完全不知道。
    """
    if not since_ts:
        return []
    now = time.time() if now is None else now
    out = [m for m in msgs
           if not m.self_sent and m.ts > since_ts and 0 < now - m.ts < age]
    return out[-limit:] if limit else out


# 非文本消息：至少让她知道你发了什么，而不是当作没发生
KIND_PLACEHOLDER = {
    "动画表情": "（发来一个表情）",
    "表情": "（发来一个表情）",
    "图片": "（发来一张图片）",
    "语音": "（发来一条语音）",
    "视频": "（发来一段视频）",
    "文件": "（发来一个文件）",
    "位置": "（发来一个位置）",
    "名片": "（发来一张名片）",
    "链接": "（发来一个链接）",
}


def msg_kind(m) -> str:
    t = _get(m, "type", "local_type", default="文本")
    return t.strip() if isinstance(t, str) else "文本"


def parse_messages(rows, my_username: str = ""):
    """把库里的行转成 RawMsg 列表，并标出哪些是自己发的。

    图片和表情读不出内容（微信 4.x 加密），但**不能当没收到**——
    用一句占位代替，她才能自然地接一句"这什么表情"。
    """
    out = []
    for m in rows or []:
        kind = msg_kind(m)
        if is_text(m):
            text = msg_text(m)
        else:
            text = KIND_PLACEHOLDER.get(kind, "")
        if not text:
            continue
        sender = str(_get(m, "sender_username", "sender", "from", default="") or "")
        flag = _get(m, "is_sender", "from_self", "self", default=None)
        if flag is None:
            self_sent = bool(my_username) and sender == my_username
        else:
            self_sent = bool(flag)
        out.append(RawMsg(text=text, key=msg_key(m), self_sent=self_sent,
                          ts=_to_ts(_get(m, "create_time", "timestamp", "ts", default=0))))
    return out


def _to_ts(v) -> float:
    try:
        v = float(v)
        return v / 1000.0 if v > 1e11 else v      # 毫秒 → 秒
    except (TypeError, ValueError):
        return 0.0


def resolve_account(name: str, accounts) -> str:
    """把配置里的账号标识补全成库目录名。

    真机结论：`WeChatDB(account=...)` 要的是带后缀的目录名
    （`wxid_xxx_2a90`），传裸 wxid 会打开一个空库 —— 静默读到 0 条，
    比报错更坑。所以这里允许只写 wxid。
    """
    if not name:
        return ""
    rows = list(accounts or [])
    for a in rows:
        if a.get("account") == name:
            return name
    for a in rows:
        if a.get("wxid") == name:
            return a.get("account") or name
    for a in rows:
        if str(a.get("account", "")).startswith(name):
            return a.get("account")
    return name


class WechatAutoChannel:
    """Channel 契约实现：轮询本地库收消息，驱动窗口发消息。"""

    # 不依赖入站消息就能发 —— 所以可以主动开口
    can_initiate = True
    # 能发图片（表情包）
    can_send_image = True

    def __init__(self, target: str, db=None, sender=None, alert_hook=None,
                 poll_interval: float = 1.0, my_username: str = "", limit: int = 30,
                 pace: tuple = None, account: str = "", target_id: str = "",
                 since_ts: float = 0.0, replay_limit: int = 8,
                 replay_age: float = 12 * 3600):
        self.target = target                 # 显示名，给 GUI 搜索用
        # 真机结论：get_messages 只认 wxid，传显示名会返回 0 条（静默失败）
        self.target_id = target_id or target
        self.db = db
        self.sender = sender
        self.alert = alert_hook or (lambda m: None)
        self.poll_interval = poll_interval
        self.my_username = my_username
        self.limit = limit
        self.pace = pace or DEFAULT_PACE
        # 本机可能有多个微信账号；不钉死的话 WeChatDB 会自动挑"最近活跃"的那个，
        # 而窗口里登录的可能是另一个 —— 读和写就会错位。
        self.account = account
        self.verify = True          # 发送后回查库，确认账号没串
        self.verify_timeout = DEFAULT_VERIFY_TIMEOUT
        # 停机期间漏掉的消息：重启后补齐，别让她"断片"
        self.since_ts = float(since_ts or 0.0)
        self.replay_limit = int(replay_limit)
        self.replay_age = float(replay_age)
        self._account_warned = False
        self._send_fail_warned = False

        self.inbox: "queue.Queue[RawMsg]" = queue.Queue()
        self._seen = set()
        self._primed = False
        self._closed = False
        self._stop = False
        self._thread = None

    def _account_warn(self):
        if self._account_warned:
            return
        self._account_warned = True
        self.alert(
            "已发送，但在 " + (self.account or "当前账号") +
            " 的库里找不到这条消息 —— 桌面微信可能登录的是另一个号，"
            "消息以那个号发出去了"
        )

    # ── 启动 ──────────────────────────────────────────────

    def start(self):
        if self.db is None or self.sender is None:
            self._connect()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def _connect(self):
        from wechatauto import WeChatDB, WeChatGUI, list_accounts  # 惰性导入

        if self.db is None:
            account = self.account
            if account:
                try:
                    account = resolve_account(account, list_accounts())
                except Exception:
                    pass
            self.db = WeChatDB(account=account) if account else WeChatDB()

        self.sender = self.sender or WeChatGUI()
        if not self.my_username:
            try:
                self.my_username = (self.db.get_self_info() or {}).get("username", "")
            except Exception:
                self.my_username = ""
        if self.account and self.my_username and self.account not in (self.my_username, account):
            self.alert(f"账号不一致：配置的是 {self.account}，库里读到 {self.my_username}")

    # ── 收：轮询本地库 ────────────────────────────────────

    def _loop(self):
        backoff = 1.0
        while not self._stop:
            try:
                rows = self.db.get_messages(self.target_id, limit=self.limit)
                msgs = parse_messages(rows, self.my_username)

                if not self._primed:
                    # 首次轮询：老消息只建基线，但「停机期间收到的」要补回来，
                    # 否则重启一次她就不知道你中途说过话。
                    replay = select_replay(msgs, self.since_ts, limit=self.replay_limit,
                                           age=self.replay_age)
                    for m in msgs:
                        self._seen.add(m.key)
                    for m in replay:
                        self.inbox.put(m)
                    if replay:
                        self.alert(f"补回停机期间的 {len(replay)} 条消息")
                    self._primed = True
                    time.sleep(self.poll_interval)
                    continue

                for m in msgs:
                    if m.key in self._seen:
                        continue
                    self._seen.add(m.key)
                    if not m.self_sent:
                        self.inbox.put(m)

                if len(self._seen) > 5000:
                    self._seen = set(list(self._seen)[-2000:])

                backoff = 1.0
                time.sleep(self.poll_interval)
            except Exception as e:
                self.alert(f"读取异常：{type(e).__name__}: {e}")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    # ── 发：驱动窗口 ──────────────────────────────────────

    def _verify_in_db(self, text: str, timeout: float = None) -> bool:
        """发送后回查钉死的那个库，确认消息真的落在了这个账号下。

        为什么要查：账号是配置钉死的，但窗口登录的是哪个号程序查不到。
        如果窗口切到了另一个号，消息会以那个号发出，而这里读的是原账号的库
        —— 不查的话就是"以为发了，其实发给了别人"。
        """
        timeout = self.verify_timeout if timeout is None else timeout
        target = text.strip()[:MAX_TEXT]
        deadline = time.time() + timeout
        while True:
            try:
                rows = self.db.get_messages(self.target_id, limit=5)
            except Exception:
                return False
            for m in rows:
                if msg_text(m) == target:
                    return True
            if time.time() >= deadline:
                return False
            time.sleep(0.25)

    def _remember_said(self, text: str) -> None:
        self._said = getattr(self, "_said", [])
        self._said.append(((text or "").strip(), time.time()))
        self._said = self._said[-8:]

    def _recent_self_text(self, window: float = 20.0) -> str:
        """本进程最近 window 秒内发出去的那句（防重复用）。

        为什么不用读库判断：库里的落库时间会慢半拍，而且窗口设大了会误伤
        —— 她 40 秒前说过"嗯"，现在再说一次"嗯"是正常的，不该被吞掉。
        进程内记账两个都躲开了。
        """
        now = time.time()
        for text, ts in reversed(getattr(self, "_said", [])):
            if now - ts <= window:
                return text
        return ""

    VK_A, VK_V, VK_DELETE, VK_RETURN = 0x41, 0x56, 0x2E, 0x0D

    def _box_text(self, sender):
        """用 UIA 读出输入框里真实的文字。读不到返回 None。

        为什么不用库的 _input_box_has_text：那是屏幕取色，取样区从框顶往下
        200px，本机布局下会吃到下面的工具栏图标 —— 永远判"有字"，
        于是要么重复发，要么一直判发送失败。
        """
        try:
            uia = sender._get_uia()
            if uia is None:
                return None
            ctrl = uia._chat_input()
            if ctrl is None:
                return None
            return uia._read_edit_value(ctrl)
        except Exception:
            return None

    def _send_direct(self, sender, text: str):
        """自己发一条：粘贴 → 确认框里就是这句 → 回车 → 确认框空了。

        返回 True=确认发出，False=没发出去，None=读不到框（交给上层查库判断）。
        """
        box = getattr(sender, "_last_input_box", None)
        if not box:
            try:
                box = sender.get_input_box()
            except Exception:
                box = None
        if not box:
            return False
        try:
            sender._last_input_box = box
            sender.focus_input(box)
            sender.set_clipboard(text)
            sender._input.key(self.VK_A, ctrl=True)
            sender._input.key(self.VK_DELETE)
            sender._input.key(self.VK_V, ctrl=True)
            time.sleep(0.6)

            got = self._box_text(sender)
            if got is not None:
                norm = lambda t: "".join((t or "").split())
                if norm(text) not in norm(got):
                    # 粘贴没落地，补一次
                    sender.focus_input(box)
                    sender._input.key(self.VK_V, ctrl=True)
                    time.sleep(0.6)
                    got = self._box_text(sender)
                    if got is not None and norm(text) not in norm(got):
                        return False

            sender.focus_input(box)
            sender._input.key(self.VK_RETURN)
            time.sleep(1.0)
            after = self._box_text(sender)
            if after is None:
                return None                    # 读不到，让上层查库
            if after.strip() == "":
                return True
            sender._input.key(self.VK_RETURN)  # 再补一次回车
            time.sleep(1.0)
            after = self._box_text(sender)
            return True if (after is not None and after.strip() == "") else False
        except Exception:
            return False

    def _nudge_send(self, sender, text: str, tries: int = 2) -> bool:
        """文字已经在输入框里、只是回车没落地时，补按一次发送。

        为什么不重新 send_msg：它会把文字再粘一遍，于是要么发两遍、要么越积越多。
        这里只动"发送"这一步。库用 _pending_text 校验框里是不是这句话，
        每次 click_send 都会把它消费掉，所以要重新塞回去。
        """
        fn = getattr(sender, "click_send", None)
        if not fn:
            return False
        focus = getattr(sender, "focus_input", None)
        box = getattr(sender, "_last_input_box", None)
        if not box:
            # 库没缓存到输入框时，click_send 会直接判失败 —— 自己找一个
            getbox = getattr(sender, "get_input_box", None)
            if getbox:
                try:
                    box = getbox()
                except Exception:
                    box = None
                if box:
                    try:
                        sender._last_input_box = box
                    except Exception:
                        pass
        for _ in range(tries):
            try:
                if focus and box:
                    focus(box)                    # 回车没生效，多半是焦点跑了
                sender._pending_text = text
                if fn():
                    return True
            except Exception:
                pass
            time.sleep(0.8)
        return False

    def _do_send(self, text: str, attempts: int = 3):
        """发送。真机结论：UIA 调用是间歇性失败的，必须重试。

        实测两种错误都会自愈：
          COMError -2147220991 '事件无法调用任何订户'
          COMError -2147467259 '未指定的错误'
        每次重试前重新抬窗并重开会话，第 2 次通常就成功。

        但重试有个坑：上一次其实已经发出去了、只是没等到确认，重试就会
        发第二遍。所以①同一条刚发过就直接跳过 ②失败后先查库，落库了当成功。
        """
        sender = self.sender
        last = None
        after_window, _after_open, retry_gap = self.pace

        text = (text or "").strip()
        if self._recent_self_text() == text:
            self.alert("刚发过一模一样的一句，跳过这条")
            return None

        # 先走这套自己实现的发送（库的"有没有字"判定在本机是坏的）
        direct = self._send_direct(sender, text)
        if direct is True:
            self._remember_said(text)
            if self.verify and not self._verify_in_db(text):
                self._account_warn()
            return None
        if direct is None:
            # 回车已经按下去了，只是读不到输入框内容 —— 这种时候**不能**再走
            # 库的发送流程，否则就是第二遍。宁可这一条算"状态未知"，也不重复发。
            self._remember_said(text)
            if not (self.verify and self._verify_in_db(text, timeout=8)):
                self.alert("发出去了但没确认到（读不到输入框），没有重发，请看一眼")
            return None

        try:
            for i in range(attempts):
                try:
                    self._prep_window(after_window)

                    # 不要再自己 open_chat：send_msg(text, who) 内部会切会话。
                    # 实测重复切一次要多花 4.5 秒；会话已经开着时 send 只要 1.6 秒。
                    for name in ("send_msg", "SendMsg", "send"):
                        fn = getattr(sender, name, None)
                        if not fn:
                            continue
                        try:
                            resp = fn(text, self.target, verify=True)
                        except TypeError:
                            try:
                                resp = fn(text, self.target)
                            except TypeError:
                                resp = fn(text, who=self.target)
                        if getattr(resp, "is_success", None) is False:
                            raise RuntimeError(f"消息未被确认：{resp}")
                        self._remember_said(text)
                        if self.verify and not self._verify_in_db(text):
                            self._account_warn()
                        return resp

                    raise RuntimeError("发送对象没有 send_msg / SendMsg 方法")
                except Exception as e:
                    last = e
                    # 最先试：文字多半已经在框里了，只是回车没落地
                    if self._nudge_send(sender, text):
                        self._remember_said(text)
                        if self.verify and not self._verify_in_db(text, timeout=2):
                            self._account_warn()
                        return None
                    # 其次试：其实已经发出去了，只是没等到确认
                    if self.verify and self._verify_in_db(text, timeout=2):
                        self._remember_said(text)
                        return None
                    # 最后才整条重来
                    if i < attempts - 1:
                        time.sleep(retry_gap)

            raise last
        finally:
            self._release(sender)

    def _prep_window(self, wait: float):
        for prep in ("ensure_visible", "bring_to_front"):
            fn = getattr(self.sender, prep, None)
            if fn:
                try:
                    fn()
                except Exception:
                    pass
        if wait:
            time.sleep(wait)

    def _release(self, sender=None):
        release = getattr(sender or self.sender, "release_foreground", None)
        if release:
            try:
                release()
            except Exception:
                pass

    def send_image(self, path: str, attempts: int = 3):
        """发一张图（表情包）。和发文字同一套抬窗 + 重试。"""
        sender = self.sender
        after_window, _after_open, retry_gap = self.pace
        last = None
        try:
            for i in range(attempts):
                try:
                    self._prep_window(after_window)
                    fn = getattr(sender, "send_image", None)
                    if not fn:
                        raise RuntimeError("发送对象没有 send_image")
                    resp = fn(str(path), self.target)
                    if getattr(resp, "is_success", None) is False:
                        raise RuntimeError(f"图片未被确认：{resp}")
                    return resp
                except Exception as e:
                    last = e
                    if i < attempts - 1:
                        time.sleep(retry_gap)
            raise last
        finally:
            self._release(sender)

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
        if m is None:
            self._closed = True
            return None
        return Inbound(text=m.text, msg_id=f"wa_{m.key}")

    def send(self, text: str) -> None:
        try:
            self._do_send(text)
            self._send_fail_warned = False
        except Exception as e:
            if not self._send_fail_warned:
                # 锁屏 / 窗口不可用时，发送必然失败，且会一直失败
                self.alert(f"发送失败（窗口不可用？）：{type(e).__name__}: {e}")
                self._send_fail_warned = True
            raise

    def typing(self, on: bool) -> None:
        # 发送是"粘贴 + 回车"，没有独立的 typing 通道
        return None

    def close(self):
        self._stop = True
        self._closed = True
        self.inbox.put(None)
