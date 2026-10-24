"""所有可调参数。改 somnia.toml，不改代码。

优先级：环境变量 > somnia.toml > 这里的默认值

这里的数字就是"像不像一个人"的全部来源。
先用先验跑，之后用你自己的真实聊天记录校准。
"""

import os
from dataclasses import dataclass, replace

import sys as _sys


def _warn(msg: str) -> None:
    """配置有问题时提醒一句。不抛异常 —— 配置错了也不该让程序起不来。"""
    try:
        print(f"[配置] {msg}", file=_sys.stderr, flush=True)
    except Exception:
        pass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB = BASE_DIR / "data" / "somnia.db"
DEFAULT_TOML = BASE_DIR / "somnia.toml"
DEFAULT_PERSONA_DIR = BASE_DIR / "persona"


@dataclass(frozen=True)
class Config:
    # ── 身份 ──────────────────────────────────────────────
    name: str = ""              # 角色名。留空就是"ta"，prompt 里也不会出现
    # 人设正文要不要发进 prompt。默认不发 —— 人设归控制台管
    prompt_persona: bool = False
    # 新版本检查地址（version.json）。留空 = 面板不显示「新版本」
    update_url: str = ""

    # ── 位置 ──────────────────────────────────────────────
    persona_dir: str = str(DEFAULT_PERSONA_DIR)
    db_path: str = str(DEFAULT_DB)

    # ── 模型 ──────────────────────────────────────────────
    api_key: str = ""
    model: str = "deepseek-flash"
    endpoint: str = "https://api.deepseek.com/chat/completions"
    temperature: float = 1.15

    # ── 通道 ──────────────────────────────────────────────
    debounce: float = 2.5          # 连发合并窗口（秒）
    claw_base: str = "https://ilinkai.weixin.qq.com"
    claw_token: str = ""           # 扫码登录后得到，见 脚本/claw_login.py
    claw_user_id: str = ""         # 登录返回的 ilink_user_id，机器人接口必填
    wxauto_target: str = ""        # 微信 4.x 通道：要挂的联系人显示名（给 GUI 用）
    wxauto_target_id: str = ""     # 同一个人的 wxid（读库只认这个）
    wxauto_username: str = ""      # 本机 wxid，用于分辨哪些消息是自己发的
    wxauto_account: str = ""       # 多账号时钉死读哪个库（wxid），不填则自动挑

    # ── 行为（拟真的真正来源）─────────────────────────────
    reply_rate: float = 0.88        # 基础回复率
    reply_rate_step: float = 0.03   # 连续回一次，概率再加这么多（聊得顺就更顺）
    reply_rate_max: float = 0.98    # 上限，永远不是 100%
    quiet_hours: tuple = (2, 7)     # 这个区间内闭嘴
    wake_chance: float = 0.10        # 睡觉时被吵醒的概率（第一条消息）
    wake_chance_step: float = 0.20   # 每回一次，概率再加这么多（越醒越难睡回去）
    wake_chance_max: float = 0.90    # 上限
    wake_max_per_night: int = 0      # 0 = 不限制
    delay_median: float = 5.0       # 回复延迟中位数（秒）
    delay_sigma: float = 0.45       # 越大越长尾
    delay_max: float = 20.0         # 延迟硬上限，防止长尾拖成半分钟
    seg_choices: tuple = (1, 1, 1, 2, 2, 2, 3)   # 连发条数分布
    seg_gap: tuple = (1.2, 2.4)     # 条间隔，防折叠

    # ── 记忆 ──────────────────────────────────────────────
    recent_n: int = 400             # 细节层一次最多读多少条（安全阀，不是压缩线）
    recent_days: int = 3            # 细节层：几天之内留原文
    hot_days: int = 3               # 超过这个天数，原文搬进档案、只留印象
    impression_days: int = 7        # 印象层读最近几天
    summary_keep_days: int = 30     # 印象在库里留多久，更早的只剩档案
    recall_enabled: bool = True     # 说到旧事/追问时，去档案里翻
    recall_limit: int = 3           # 一次最多翻出几段
    recall_k: int = 0               # L3 读多少条；0 = 不限制，能记多少读多少
    summary_days: int = 3           # L2 读几天
    fact_half_life_days: float = 30.0   # 事实记忆半衰期
    compress_batch: int = 200       # 旧机制，已停用（保留字段兼容）
    compress_enabled: bool = False  # 400 条压缩已取消，改用分层记忆
    chronicle_keep: int = 6         # 旧机制，已停用

    # ── 补回复 ────────────────────────────────────────────
    catchup_enabled: bool = True
    catchup_delay_s: int = 240      # 没回的消息，多久之后补一句
    catchup_max_age_s: int = 3 * 3600   # 超过这么久就不补了

    # ── 主动性 ────────────────────────────────────────────
    proactive: bool = True          # 允许她主动发消息
    silence_hours: float = 2.0      # 你沉默多久她开始想说话（有概率）
    proactive_guarantee_hours: float = 6.0   # 到这个点必发
    # 概率从 silence_hours 的 base 线性升到 guarantee 的 100%：
    #   2h → 2%   3h → 26%   4h → 51%   5h → 75%   6h → 100%
    proactive_base_chance: float = 0.02
    wake_cooldown_h: float = 5.5    # 两次唤醒最小间隔（小时）
    outbox_threshold: float = 0.6   # 念头够不够格说出来
    proactive_max_per_day: int = 2  # 一天最多主动说几次
    promise_keep_rate: float = 0.9  # 承诺九成按时兑现
    promise_delay_h: float = 2.5    # 剩下那一成，往后拖多久才想起来
    # 晚间问候：一整天没聊，到点她主动来问一句（不受概率控制）
    evening_checkin: bool = True
    evening_window: tuple = (20, 22)
    scheduler_interval_min: int = 10  # 后台检查间隔（分钟）

    # ── 文风 ──────────────────────────────────────────────
    max_len: int = 26               # 单条上限

    # ── 表情包 ────────────────────────────────────────────
    sticker_enabled: bool = True
    sticker_prob: float = 0.18      # 离线模式下随机补一张的概率
    sticker_offer_prob: float = 0.35  # 有多少比例的回合把表情清单给她挑
    sticker_dir: str = str(BASE_DIR / "stickers")

    # ── 日常照片 ──────────────────────────────────────────
    photo_enabled: bool = True
    photo_offer_prob: float = 0.12   # 有多少比例的回合让她能发照片
    photo_dir: str = str(BASE_DIR / "photos")
    image_gen_enabled: bool = True   # 库里没有时，允许她现场生成一张
    image_gen_url: str = "http://127.0.0.1:8100/v1/images/generations"
    image_gen_cooldown_min: int = 0    # 0 = 不冷却（频率靠 offer_prob 和日上限控）
    image_gen_max_per_day: int = 6
    banned: tuple = (
        "听起来", "我理解", "抱抱你", "你辛苦了", "希望能帮到",
        "作为一", "首先", "其次", "总之", "记得照顾好自己",
    )

    ENV_MAP = {
        "name": "SOMNIA_NAME",
        "update_url": "SOMNIA_UPDATE_URL",
        "db_path": "SOMNIA_DB",
        "persona_dir": "SOMNIA_PERSONA_DIR",
        "api_key": "DEEPSEEK_API_KEY",
        "model": "DEEPSEEK_MODEL",
        "claw_token": "SOMNIA_CLAW_TOKEN",
        "claw_user_id": "SOMNIA_CLAW_USER_ID",
        "wxauto_target": "SOMNIA_WA_TARGET",
        "wxauto_target_id": "SOMNIA_WA_TARGET_ID",
        "wxauto_username": "SOMNIA_WA_USERNAME",
        "wxauto_account": "SOMNIA_WA_ACCOUNT",
        "claw_base": "SOMNIA_CLAW_BASE",
    }

    def with_env(self) -> "Config":
        """环境变量最后叠加 —— 它优先级最高。

        以前是把 env 先塞进去、再叠 toml，结果 toml 一覆盖，env 等于没用。
        文档写的是 env > 面板 > toml > 默认，现在才对上。
        """
        upd = {k: os.environ[v] for k, v in self.ENV_MAP.items() if os.environ.get(v)}
        return replace(self, **upd) if upd else self

    @classmethod
    def from_env(cls) -> "Config":
        # 顺序很重要：默认 → toml → 面板 → 环境变量
        return cls().with_file(DEFAULT_TOML).with_settings().with_env()

    def with_settings(self) -> "Config":
        """面板写的 data/settings.json 覆盖默认值（但不覆盖已填的环境变量）。"""
        try:
            from . import settings as s
            d = s.load()
        except Exception:
            return self
        return replace(
            self,
            name=d.get("name") or self.name,
            endpoint=d.get("endpoint") or self.endpoint,
            api_key=d.get("api_key") or self.api_key,
            model=d.get("model") or self.model,
            wxauto_target=d.get("target") or self.wxauto_target,
            wxauto_target_id=d.get("target_id") or self.wxauto_target_id,
            wxauto_account=d.get("account") or self.wxauto_account,
            proactive=bool(d.get("proactive", self.proactive)),
            silence_hours=float(d.get("silence_hours") or self.silence_hours),
            image_gen_enabled=bool(d.get("image_gen", self.image_gen_enabled)),
            prompt_persona=bool(d.get("prompt_persona", self.prompt_persona)),
        )

    def with_file(self, path) -> "Config":
        """读 somnia.toml。缺字段就保留默认，写错了也不崩。"""
        try:
            import tomllib
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except FileNotFoundError:
            return self
        except ImportError:
            _warn("这个 Python 没有 tomllib（需要 3.11 以上），somnia.toml 整份被忽略了")
            return self
        except OSError:
            return self
        except Exception as e:
            _warn(f"somnia.toml 解析失败（{type(e).__name__}: {e}），这次全部用默认值")
            return self

        def pick(section, key, cast=None):
            raw = data.get(section, {}).get(key)
            if raw is None:
                return None
            try:
                return cast(raw) if cast else raw
            except Exception:
                _warn(f"{section}.{key} = {raw!r} 不是合法值，已忽略（用默认）")
                return None

        updates = {}
        table = {
            "identity": {"name": str, "persona_dir": str,
                          "prompt_persona": bool},
            "behavior": {
                "reply_rate": float, "reply_rate_step": float,
                "reply_rate_max": float,
                "quiet_hours": tuple, "delay_median": float,
                "wake_chance": float, "wake_chance_step": float,
                "wake_chance_max": float, "wake_max_per_night": int,
                "delay_sigma": float, "delay_max": float,
                "seg_choices": tuple, "seg_gap": tuple,
                "debounce": float, "max_len": int, "banned": tuple,
                "sticker_enabled": bool, "sticker_prob": float,
                "sticker_offer_prob": float, "sticker_dir": str,
                "photo_enabled": bool, "photo_offer_prob": float, "photo_dir": str,
                "image_gen_enabled": bool, "image_gen_url": str,
                "image_gen_cooldown_min": int, "image_gen_max_per_day": int,
            },
            "memory": {
                "recent_n": int, "recent_days": int, "recall_k": int,
                "summary_days": int, "fact_half_life_days": float,
                "compress_batch": int, "compress_enabled": bool,
                "chronicle_keep": int, "hot_days": int,
                "impression_days": int, "summary_keep_days": int,
                "recall_enabled": bool, "recall_limit": int,
            },
            "proactive": {
                "silence_hours": float, "wake_cooldown_h": float,
                "proactive_guarantee_hours": float, "proactive_base_chance": float,
                "outbox_threshold": float, "scheduler_interval_min": int,
                "proactive_max_per_day": int,
                "promise_keep_rate": float, "promise_delay_h": float,
                "evening_checkin": bool, "evening_window": tuple,
                "catchup_enabled": bool, "catchup_delay_s": int,
                "catchup_max_age_s": int,
            },
            "model": {"model": str, "temperature": float},
            "update": {"update_url": str},
        }
        for section, fields in table.items():
            for key, cast in fields.items():
                v = pick(section, key, cast)
                if v is not None:
                    updates[key] = v

        return replace(self, **updates) if updates else self
