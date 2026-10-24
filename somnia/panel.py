"""本地面板：一个端口，开就在跑，关就停。

    python -m somnia.panel              # 默认 http://127.0.0.1:8765
    python -m somnia.panel --port 9000

页面里能改的：性格（persona.md）、API 地址/密钥/模型、通道、聊天对象。
改完点「保存并启动」，这个进程就托管机器人；关掉进程（或点停止）它就停。

只绑 127.0.0.1：这个端口不该暴露到局域网。
"""

import argparse
import base64
import json
import sys
import threading
import time
import traceback
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import settings as S
from .config import Config
from . import sections
from .stickers import StickerBook, safe_name

ROOT = Path(__file__).resolve().parent.parent
PERSONA_FILE = ROOT / "persona" / "persona.md"
SETTINGS_FILE = ROOT / "data" / "settings.json"
MAX_BODY = 4 * 1024 * 1024      # 一次最多 4 MB，防止误黏大文件把磁盘写满
SOUL_FILE = ROOT / "persona" / "SOUL.md"
STICKER_DIR = ROOT / "stickers"
STICKER_META = ROOT / "data" / "stickers.json"
PHOTO_DIR = ROOT / "photos"
PHOTO_META = ROOT / "data" / "photos.json"


def image_book(kind: str) -> StickerBook:
    if kind == "photos":
        return StickerBook(PHOTO_DIR, meta_path=PHOTO_META)
    return StickerBook(STICKER_DIR, meta_path=STICKER_META)


def image_dir(kind: str) -> Path:
    return PHOTO_DIR if kind == "photos" else STICKER_DIR


# 兼容旧调用
def sticker_book() -> StickerBook:
    return image_book("stickers")


MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp"}


def cli_interactive() -> bool:
    """这个面板有没有可以输入的控制台（用 start.bat 起的时候有）。"""
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:
        return False


class PanelApp:
    """面板的全部行为。HTTP 只是薄壳，方便直接单测。"""

    def __init__(self, runner=None):
        self.thread = None
        self.channel = None
        self.stop_event = None
        self.last_error = ""
        self.started_at = 0.0
        self.log = deque(maxlen=200)
        self._runner = runner          # 注入用；默认 app.run

    # ── 状态 ──────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def note(self, msg: str):
        self.log.append(f"{time.strftime('%H:%M:%S')}  {msg}")

    def state(self) -> dict:
        d = S.load()
        d["api_key_masked"] = S.mask(d.get("api_key", ""))
        d["api_key"] = ""                      # 不回传明文
        d["running"] = self.running
        d["uptime_s"] = int(time.time() - self.started_at) if self.running else 0
        d["last_error"] = self.last_error
        d["log"] = list(self.log)[-40:]
        _persona_text = read_text(PERSONA_FILE)
        d["persona"] = _persona_text
        d["sections"] = sections.to_json(_persona_text)
        d["soul"] = read_text(SOUL_FILE)
        return d

    # ── 配置 ──────────────────────────────────────────────

    def update_settings(self, patch: dict) -> dict:
        # 空字符串的 api_key 表示"不修改"，避免把掩码写回去
        if patch.get("api_key") == "":
            patch.pop("api_key", None)
        return S.save(patch)

    def clear_memory(self) -> dict:
        """清空记忆。先备份，再删。"""
        from . import store
        from .config import Config
        cfg = Config.from_env()
        conn = store.connect(cfg.db_path)
        try:
            n = store.clear_all(conn, backup_dir=str(Path(cfg.db_path).parent / "backup"))
        finally:
            conn.close()
        self.note("记忆已清空（备份在 data/backup/）")
        return n

    def image_gen_status(self) -> dict:
        """本机生图服务在不在。面板据此决定那个勾能不能点。"""
        from . import imagegen
        from .config import Config
        try:
            url = Config.from_env().image_gen_url
        except Exception:
            url = imagegen.DEFAULT_URL
        try:
            ok = imagegen.health(url, timeout=2)
        except Exception:
            ok = False
        return {"ok": bool(ok), "url": url}

    def save_persona(self, text: str, force: bool = False):
        """写人设。会先备份，并且拒绝"一下子少了一大半"的写入。

        这个守卫是被真事逼出来的：面板按块保存时，如果卡片是空的，
        render() 会把空块整段丢掉 —— 一次保存就能把人设削掉大半，
        而且没有任何提示。现在至少会拦一次，让人确认。
        """
        if text is None:
            return False, "没有内容"
        old = read_text(PERSONA_FILE)
        new_len, old_len = len(text.strip()), len(old.strip())
        if not force and old_len > 300 and new_len < old_len * 0.5:
            return False, (f"人设从 {old_len} 字缩到 {new_len} 字，"
                           f"像是丢了东西。确认要这样保存吗？")

        PERSONA_FILE.parent.mkdir(parents=True, exist_ok=True)
        if old.strip():
            bdir = ROOT / "data" / "backup"
            bdir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            (bdir / f"persona_{stamp}.md").write_text(old, encoding="utf-8")

        PERSONA_FILE.write_text(text, encoding="utf-8")
        self.note("人设已保存（下一轮对话生效，不需要重启）")
        return True, "已保存"

    def save_persona_sections(self, items, force: bool = False):
        """按块保存：说明行、顺序、新增的块都原样保留。"""
        if not isinstance(items, list) or not items:
            return False, "没有内容"
        text = sections.render(items)
        if not text.strip():
            return False, "内容为空"
        return self.save_persona(text, force=force)

    # ── 新版本 ────────────────────────────────────────────

    def check_update(self) -> dict:
        from . import updater
        cfg = Config.from_env()
        return updater.check(cfg.update_url)

    def apply_update(self):
        """从 version.json 里的地址下载并覆盖。返回 (ok, message)。"""
        from . import updater
        cfg = Config.from_env()
        if self.running:
            return False, "先点「睡下」，停止后再更新"
        info = updater.check(cfg.update_url)
        if info.get("error"):
            return False, f"检查失败：{info['error']}"
        if not info.get("has_new"):
            return False, "没有可用的新版本"
        try:
            r = updater.update_from_url(info["zip"])
        except Exception as e:
            return False, f"更新失败：{type(e).__name__}: {e}"
        self.note(f"已更新 {r['old']} → {r['new']}，重启后生效")
        return True, (f"已更新 {r['old']} → {r['new']}，覆盖 {r['copied']} 个文件。\n"
                      f"关掉黑窗口，重新双击 start.bat 就生效。\n"
                      f"备份在 data/backup/ 里。")

    # ── 启停 ──────────────────────────────────────────────

    def start(self):
        if self.running:
            return False, "已经在运行"
        cfg = Config.from_env()
        kind = S.load().get("channel") or "cli"
        if kind == "cli":
            # 用 start.bat 起的时候，面板自己就在一个控制台里，stdin 是终端 —— 能聊。
            # 只有后台/无控制台启动（没有黑窗口）才真的没法输入。
            if not cli_interactive():
                return False, ("cli 通道要终端输入，但这个面板没有控制台。"
                               "请双击 start.bat 启动（会开黑窗口），或把通道换成 wechatauto")
        if kind == "wechatauto" and not cfg.wxauto_target:
            return False, "wechatauto 需要填聊天对象"
        if kind == "claw" and not cfg.claw_token:
            return False, "claw 需要先扫码拿 token"

        self.stop_event = threading.Event()
        self.last_error = ""
        self.started_at = time.time()
        self.thread = threading.Thread(target=self._run, args=(cfg, kind), daemon=True)
        self.thread.start()
        self.note(f"启动：{kind}")
        return True, f"已启动（{kind}）"

    def _run(self, cfg, kind):
        runner = self._runner
        if runner is None:
            from . import app as app_mod
            runner = app_mod.run
        try:
            runner(cfg, False, False, kind, None, True, self.stop_event, self._set_channel)
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            self.note("运行出错：" + self.last_error)
            self.note(traceback.format_exc(limit=2))

    def _set_channel(self, ch):
        self.channel = ch

    def stop(self):
        if not self.running:
            self.channel = None
            return False, "没有在运行"
        if self.stop_event:
            self.stop_event.set()
        ch = self.channel
        if ch is not None:
            try:
                ch.close()          # 解除 recv 阻塞
            except Exception:
                pass
        self.channel = None
        self.thread = None
        self.note("已停止")
        return True, "已停止"


def read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Somnia·眠</title>
<style>
:root{
  --panel:rgba(19,22,35,.62); --panel2:rgba(24,27,42,.72);
  --line:rgba(255,255,255,.075); --line2:rgba(255,255,255,.12);
  --text:#e9e7f4; --muted:#8c90aa; --dim:#5f6480;
  --accent:#b6a4ff; --accent-dim:rgba(182,164,255,.14); --warm:#efd9a3; --ok:#5fd0a0;
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;color:var(--text);overflow:hidden;
  font:14px/1.65 system-ui,"Microsoft YaHei",sans-serif;
  background:
    radial-gradient(1100px 620px at 84% -12%, rgba(182,164,255,.20), transparent 62%),
    radial-gradient(820px 520px at 3% 108%, rgba(96,150,255,.13), transparent 62%),
    linear-gradient(158deg,#07080f 0%,#0d1020 48%,#090b16 100%);
  background-attachment:fixed}
body::before{content:"";position:fixed;inset:0;pointer-events:none;opacity:.5;
  background-image:
    radial-gradient(1px 1px at 12% 18%,#fff,transparent),
    radial-gradient(1px 1px at 68% 12%,#cfd6ff,transparent),
    radial-gradient(1.5px 1.5px at 42% 62%,#fff,transparent),
    radial-gradient(1px 1px at 88% 44%,#e9e3ff,transparent),
    radial-gradient(1px 1px at 28% 84%,#fff,transparent),
    radial-gradient(1px 1px at 76% 78%,#d8dcff,transparent),
    radial-gradient(1.5px 1.5px at 55% 32%,#fff,transparent);
  background-size:640px 640px,780px 780px,520px 520px,900px 900px,700px 700px,600px 600px,820px 820px;
  background-repeat:repeat}
body::after{content:"";position:fixed;right:-170px;top:-190px;width:420px;height:420px;
  border-radius:50%;pointer-events:none;opacity:.26;filter:blur(8px);z-index:0;
  background:radial-gradient(circle at 38% 36%,rgba(255,248,225,.9),rgba(255,244,214,.20) 46%,transparent 66%)}

.app{display:flex;height:100vh;position:relative;z-index:1}

/* ── 侧栏 ───────────────────────────────────── */
.side{width:236px;flex:none;display:flex;flex-direction:column;gap:18px;
  padding:22px 16px 16px;border-right:1px solid var(--line);
  background:rgba(8,10,18,.5);backdrop-filter:blur(12px)}
.brand{display:flex;gap:11px;align-items:center;padding:0 4px}
.moon{width:34px;height:34px;flex:none;border-radius:50%;display:grid;place-items:center;
  font-size:17px;color:#f4ecff;background:linear-gradient(150deg,rgba(182,164,255,.28),rgba(96,150,255,.16));
  border:1px solid var(--line2);box-shadow:0 0 22px rgba(182,164,255,.22) inset}
.brand .name{font-family:Georgia,"Songti SC","SimSun",serif;font-size:17px;letter-spacing:.10em}
.brand .tag{font-size:11px;color:var(--dim);letter-spacing:.16em}

.statusBox{border:1px solid var(--line);border-radius:12px;padding:12px;
  background:var(--panel);display:flex;flex-direction:column;gap:9px}
.pill{display:flex;align-items:center;gap:7px;font-size:13px}
.dot{width:7px;height:7px;border-radius:50%;background:#4a4f66;box-shadow:0 0 0 3px rgba(255,255,255,.03)}
.dot.on{background:var(--ok);box-shadow:0 0 10px rgba(95,208,160,.75)}
.hint{font-size:11px;color:var(--warm);min-height:0}
.btns{display:flex;gap:7px}
.btns button{flex:1;padding:7px 0;font-size:12.5px}

.nav{display:flex;flex-direction:column;gap:2px;margin-top:2px}
.nav a{display:flex;align-items:center;gap:9px;padding:9px 12px;border-radius:10px;
  color:var(--muted);text-decoration:none;cursor:pointer;font-size:13.5px;
  transition:background .15s,color .15s}
.nav a:hover{color:var(--text);background:rgba(255,255,255,.04)}
.nav a.active{color:#f2eeff;background:var(--accent-dim);box-shadow:inset 0 0 0 1px rgba(182,164,255,.18)}
.nav a .k{font-size:11px;color:var(--dim);margin-left:auto}

.sideFoot{margin-top:auto;font-size:11px;color:var(--dim);line-height:1.7;padding:0 4px}
.sideFoot b{color:var(--muted);font-weight:500}

/* ── 主区 ───────────────────────────────────── */
.main{flex:1;min-width:0;display:flex;flex-direction:column}
.head{padding:20px 30px 10px;display:flex;align-items:baseline;gap:14px;flex:none}
.head h1{margin:0;font-family:Georgia,"Songti SC","SimSun",serif;
  font-size:19px;font-weight:600;letter-spacing:.06em}
.head .sub{font-size:12px;color:var(--dim)}
.pages{flex:1;min-height:0;padding:6px 30px 26px;display:flex}
.page{display:none;flex-direction:column;gap:14px;width:100%;min-height:0}
.page.active{display:flex}
.page.scroll{overflow-y:auto;padding-right:6px}

.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;
  padding:15px 17px;backdrop-filter:blur(8px)}
.card h2{margin:0 0 11px;font-size:12px;font-weight:600;letter-spacing:.14em;
  color:var(--muted);text-transform:uppercase;display:flex;align-items:center;gap:8px}
.card h2 .note{font-size:11px;font-weight:400;letter-spacing:0;color:var(--dim);text-transform:none}

label{display:block;font-size:11.5px;color:var(--muted);margin:9px 0 5px;letter-spacing:.03em}
input,textarea,select{width:100%;background:rgba(8,10,18,.65);color:var(--text);
  border:1px solid var(--line2);border-radius:8px;padding:8px 10px;font:inherit;
  transition:border-color .15s,box-shadow .15s}
input:focus,textarea:focus,select:focus{outline:0;border-color:rgba(182,164,255,.55);
  box-shadow:0 0 0 3px rgba(182,164,255,.12)}
textarea{resize:vertical;font-family:ui-monospace,Consolas,monospace;font-size:12.5px;line-height:1.75}
input[type=checkbox]{width:auto;accent-color:var(--accent)}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
button{background:linear-gradient(150deg,#8f7cf0,#6d5ce0);color:#fff;border:0;border-radius:8px;
  padding:8px 16px;font:inherit;cursor:pointer;transition:filter .15s,transform .06s}
button:hover{filter:brightness(1.1)}
button:active{transform:translateY(1px)}
button.ghost{background:rgba(255,255,255,.06);color:#d5d8e8;border:1px solid var(--line2)}
button:disabled{opacity:.45;cursor:default;filter:none}
.sub{color:var(--dim);font-size:11.5px}

/* 对话 */
.dayBar{display:flex;gap:7px;align-items:center;flex:none;padding:2px 0 8px;
  overflow-x:auto;white-space:nowrap;scrollbar-width:none}
.dayBar::-webkit-scrollbar{display:none}
.dayChip{flex:none;padding:4px 12px;border-radius:999px;font-size:12px;cursor:pointer;
  color:var(--muted);border:1px solid var(--line);background:rgba(255,255,255,.03);
  transition:all .15s}
.dayChip:hover{color:var(--text);border-color:var(--line2)}
.dayChip.on{color:#f0ecff;background:var(--accent-dim);border-color:rgba(182,164,255,.4)}
.dayChip .n{font-size:10.5px;opacity:.6;margin-left:5px}
.chatBar{display:flex;align-items:center;gap:10px;flex:none;padding:2px 0 8px;
  border-bottom:1px solid rgba(255,255,255,.05);margin-bottom:4px}
#msgs{flex:1;min-height:0;overflow-y:auto;padding:4px 10px 8px 0;scroll-behavior:smooth;display:flex;flex-direction:column}

.msgRow{display:flex;align-items:flex-end;gap:9px;margin:2px 0}
.msgRow.her{flex-direction:row-reverse}
.msgChk{width:auto;opacity:0;transition:opacity .15s;margin:0 2px 8px;flex:none;accent-color:var(--accent)}
.msgRow:hover .msgChk{opacity:1}
.bubble{max-width:min(74%,780px);padding:8px 13px;border-radius:15px;position:relative;
  display:flex;align-items:flex-end;gap:9px;
  background:rgba(255,255,255,.05);border:1px solid var(--line);
  box-shadow:0 1px 2px rgba(0,0,0,.18)}
.msgRow.her .bubble{background:linear-gradient(160deg,rgba(182,164,255,.18),rgba(182,164,255,.09));
  border-color:rgba(182,164,255,.26);border-bottom-right-radius:5px}
.msgRow.him .bubble{border-bottom-left-radius:5px}
.bubble .txt{flex:1 1 auto;min-width:0;font-size:13.5px;line-height:1.65;color:#e8e6f4;
  white-space:pre-wrap;word-break:break-word}
.bubble .tm{flex:none;font-size:10px;color:var(--dim);opacity:.6;padding-bottom:2px;letter-spacing:.02em}
.bubble .del{position:absolute;top:-7px;right:-7px;width:17px;height:17px;border-radius:50%;
  background:#2b3046;color:#c7cbe0;border:1px solid rgba(255,255,255,.12);
  font-size:12px;line-height:1;padding:0;cursor:pointer;opacity:0;transition:opacity .15s;
  display:grid;place-items:center}
.msgRow:hover .bubble .del{opacity:1}
.bubble .del:hover{background:#7a3b48;color:#ffd9de}

.daySep{display:flex;gap:10px;align-items:center;margin:16px 0 10px;position:sticky;top:0;
  background:linear-gradient(180deg,rgba(9,11,19,.97) 0%,rgba(9,11,19,.9) 62%,rgba(9,11,19,0) 100%);
  padding:8px 0 12px;z-index:3}
.daySep .d{font-size:11.5px;color:#b0a5e6;letter-spacing:.12em;white-space:nowrap}
.daySep .c{font-size:10.5px;color:var(--dim);white-space:nowrap}
.daySep .l{flex:1;height:1px;background:linear-gradient(90deg,transparent,var(--line),transparent)}

.empty{margin:auto;text-align:center;color:var(--muted);font-size:13px;line-height:2}
.empty span{color:var(--dim);font-size:11.5px}

pre{background:rgba(6,8,14,.6);border:1px solid var(--line);border-radius:10px;
  padding:12px;margin:0;font-size:12px;line-height:1.7;overflow:auto;color:#b6bad0;
  font-family:ui-monospace,Consolas,monospace}
#log{flex:1;min-height:0}
.pulseCard{flex:none;max-height:46%;display:flex;flex-direction:column;min-height:0}
.pulseCard #decisions{overflow-y:auto;min-height:0}

#decisions{font-size:12.5px;color:#aeb3c9;line-height:2}
#decisions .d{display:flex;gap:10px}

.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.grid3{display:grid;grid-template-columns:repeat(auto-fill,minmax(148px,1fr));gap:12px}

/* 人设：按块编辑 */
.secList{flex:1;min-height:0;overflow-y:auto;display:flex;flex-direction:column;gap:11px;padding-right:8px}
.sec{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 15px}
.sec .h{display:flex;align-items:baseline;gap:10px;margin-bottom:7px}
.sec .t{font-size:12.5px;letter-spacing:.12em;color:#d3cbf8;font-weight:600;white-space:nowrap}
.sec .d{font-size:11px;color:var(--dim);flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sec .rm{background:none;border:0;color:#6d7288;cursor:pointer;font-size:11.5px;padding:0;font-family:inherit}
.sec .rm:hover{color:#ff9a9a}
.sec textarea{min-height:54px;resize:vertical;font-family:inherit;font-size:13px;line-height:1.7}
.secTitleIn{background:transparent;border:0;border-bottom:1px dashed rgba(255,255,255,.18);
  color:#d3cbf8;font-size:12.5px;letter-spacing:.12em;font-weight:600;padding:2px 0;width:190px}
.secTitleIn:focus{outline:0;border-bottom-color:var(--accent)}
.saveBar{display:flex;align-items:center;gap:12px;flex:none;padding-top:12px}
.card.danger{border-color:rgba(255,120,120,.22)}
.dangerBtn{background:linear-gradient(150deg,#a8414f,#8a2f3d)}

::-webkit-scrollbar{width:9px;height:9px}
::-webkit-scrollbar-thumb{background:rgba(255,255,255,.10);border-radius:5px}
::-webkit-scrollbar-thumb:hover{background:rgba(255,255,255,.18)}
::-webkit-scrollbar-track{background:transparent}
.disclaimer{position:fixed;left:0;right:0;top:0;z-index:60;height:36px;
  display:flex;align-items:center;justify-content:center;gap:10px;
  font-size:12.5px;letter-spacing:.05em;color:#f0c98a;
  background:linear-gradient(180deg,rgba(48,32,18,.96),rgba(32,21,13,.96));
  border-bottom:1px solid rgba(240,201,138,.22);backdrop-filter:blur(8px)}
.disclaimer b{color:#ffdcaa;font-weight:700;letter-spacing:.08em}
.disclaimer .dotSep{opacity:.4;margin:0 2px}
.disclaimer .warn{color:#ff9f6a;font-size:13px}
body{padding-top:36px}
.app{height:calc(100vh - 36px)}

.mask{position:fixed;inset:0;display:none;align-items:center;justify-content:center;z-index:50;
  background:rgba(5,6,12,.68);backdrop-filter:blur(4px)}
.mask.on{display:flex}
.dlg{width:min(540px,92vw);background:linear-gradient(170deg,rgba(26,29,44,.98),rgba(17,19,30,.98));
  border:1px solid var(--line2);border-radius:18px;padding:24px 26px;
  box-shadow:0 30px 70px rgba(0,0,0,.6),0 0 0 1px rgba(255,255,255,.03) inset}
.dTitle{font-family:Georgia,"Songti SC","SimSun",serif;font-size:17px;letter-spacing:.1em;margin-bottom:12px}
.dBody{font-size:13px;line-height:2.05;color:#b6bad0}
.dBody b{color:#ddd6ff;font-weight:600}
.dBody .soft{color:#8c90aa}
.dBtns{display:flex;gap:10px;justify-content:flex-end;margin-top:20px}

.verChip{display:flex;align-items:center;justify-content:center;gap:5px;
  margin:10px 2px 0;padding:6px 10px;border-radius:999px;cursor:pointer;
  font-size:12px;letter-spacing:.02em;color:#cfc4ff;text-decoration:none;
  border:1px solid rgba(182,164,255,.42);background:rgba(140,116,255,.12);
  transition:all .15s}
.verChip:hover{background:rgba(140,116,255,.2);border-color:rgba(182,164,255,.6)}
.verChip[hidden]{display:none}

@media(max-width:820px){.side{width:76px;padding:16px 10px}.brand .name,.brand .tag,.nav a span.t,.sideFoot{display:none}
  .grid2{grid-template-columns:1fr}.pages{padding:6px 16px 20px}}
</style></head><body><div class="app">

<aside class="side">
  <div class="brand">
    <div class="moon">☾</div>
    <div><div class="name">SOMNIA</div><div class="tag">眠 · 睡着时也在场</div></div>
  </div>

  <div class="statusBox">
    <div class="pill"><span id="dot" class="dot"></span><span id="status">未运行</span></div>
    <div id="dirtyHint" class="hint"></div>
    <div class="btns">
      <button id="btnStart">唤醒</button>
      <button id="btnStop" class="ghost">睡下</button>
    </div>
  </div>

  <a id="verChip" class="verChip" hidden>↓ 新版本</a>

  <nav class="nav" id="nav">
    <a data-page="chat" class="active"><span class="t">回忆</span></a>
    <a data-page="pulse"><span class="t">此刻</span><span class="k">说与没说</span></a>
    <a data-page="media"><span class="t">图册</span></a>
    <a data-page="persona"><span class="t">ta 是谁</span></a>
    <a data-page="settings"><span class="t">设置</span></a>
  </nav>

  <!-- build __BUILD__ -->
</aside>

<main class="main">
  <div class="head">
    <h1 id="pageTitle">回忆</h1>
    <div class="sub" id="pageSub">ta 说过的、听过的，一天一天落在这里</div>
  </div>

  <div class="pages">

    <!-- 对话 -->
    <section class="page active" data-page="chat">
      <div class="dayBar" id="dayBar"></div>
      <div class="chatBar">
        <label style="display:flex;align-items:center;gap:6px;margin:0;color:var(--muted)">
          <input type="checkbox" id="msgAll"> 全选
        </label>
        <button id="msgDelSel" class="ghost" style="padding:5px 12px;font-size:12px">删除选中</button>
        <span id="msgSelCount" class="sub"></span>
        <span style="flex:1"></span>
        <span class="sub">向上翻看旧话时，不会被新消息拽回来</span>
      </div>
      <div id="msgs"></div>
    </section>

    <!-- 此刻：应答 + 日志 -->
    <section class="page" data-page="pulse">
      <div class="card pulseCard">
        <h2>说与没说 <span class="note">ta 没开口的时候，缘由都在这里</span></h2>
        <div id="decisions"></div>
      </div>
      <pre id="log"></pre>
    </section>

    <!-- 表情与照片 -->
    <section class="page scroll" data-page="media">
      <div class="card">
        <h2>表情包 <span id="stkCount" class="note"></span></h2>
        <div class="row" style="margin-bottom:12px">
          <input type="file" id="stkFile" accept="image/*" multiple style="width:auto;max-width:240px">
          <button id="stkUpload" class="ghost">上传</button>
          <span id="stkMsg" style="font-size:12px;color:var(--warm)"></span>
          <span class="sub">png / jpg / gif / webp · 单张不超过 5MB · 备注写清这张图是什么意思</span>
        </div>
        <div id="stkGrid" class="grid3"></div>
      </div>

      <div class="card">
        <h2>日常照片 <span id="phoCount" class="note"></span></h2>
        <div class="row" style="margin-bottom:12px">
          <input type="file" id="phoFile" accept="image/*" multiple style="width:auto;max-width:240px">
          <button id="phoUpload" class="ghost">上传</button>
          <span id="phoMsg" style="font-size:12px;color:var(--warm)"></span>
          <span class="sub">ta 看见的东西：晚霞、猫、杯子里的影子 · 备注写清它拍的是什么</span>
        </div>
        <div id="phoGrid" class="grid3"></div>
      </div>
    </section>

    <!-- 人设 -->
    <section class="page" data-page="persona">
      <div class="secList" id="secList"></div>
      <div class="saveBar">
        <button id="btnPersona">保存人设</button>
        <span class="sub">保存之后，下一句话生效 · 留白的块不会写入</span>
        <span style="flex:1"></span>
        <button id="btnAddSec" class="ghost">+ 新增一块</button>
        <button id="btnRaw" class="ghost">原文</button>
      </div>
      <div class="card rawBox" id="rawBox" style="display:none">
        <h2>persona.md 原文 <span class="note">直接改全文，保存会覆盖结构编辑的结果</span></h2>
        <textarea id="persona" style="min-height:260px"></textarea>
        <div class="row" style="margin-top:10px">
          <button id="btnRawSave" class="ghost">保存原文</button>
        </div>
      </div>
    </section>

    <!-- 设置 -->
    <section class="page scroll" data-page="settings">
      <div class="grid2">
        <div class="card">
          <h2>模型</h2>
          <label>名字</label><input id="name" placeholder="留空就显示为 ta，prompt 里也不会出现">
          <label>接口地址</label><input id="endpoint">
          <label>API Key</label><input id="api_key" placeholder="留空表示不修改">
          <label>模型名</label><input id="model">
        </div>

        <div class="card">
          <h2>通道</h2>
          <label>收发方式</label>
          <select id="channel">
            <option value="cli">cli（终端里先试试，不用装任何东西）</option>
            <option value="wechatauto">wechatauto（微信 4.x，有头像）</option>
            <option value="claw">claw（官方，无头像）</option>
          </select>
          <div class="sub" style="margin-top:-4px">
            cli 只在启动它的那个窗口里收发；接微信要另装 wechatauto-replica，
            而且<b style="color:var(--warm)">可能导致微信意外登出</b>，建议先用小号
          </div>
          <label>聊天对象（显示名）</label><input id="target">
          <label>对象 wxid（读库用）</label><input id="target_id">
          <label>发送账号 wxid</label><input id="account">
        </div>
      </div>

      <div class="card">
        <h2>主动性</h2>
        <div class="grid2">
          <div>
            <label style="display:flex;align-items:center;gap:8px;color:var(--text)">
              <input type="checkbox" id="proactive"> 允许 ta 主动开口
            </label>
            <label>沉默多少小时后主动开口</label>
            <input id="silence_hours" type="number" min="1" max="168" step="0.5">
          </div>
          <div>
            <label style="display:flex;align-items:center;gap:8px;color:var(--text);margin-top:23px">
              <input type="checkbox" id="image_gen"> 允许 ta 自己拍照
            </label>
            <label style="display:flex;align-items:center;gap:8px;color:var(--text);margin-top:10px">
              <input type="checkbox" id="prompt_persona"> 人设参与对话
            </label>
            <div class="sub" style="margin-top:4px">关着的时候，「ta 是谁」里写的只是存档，不影响她怎么说话</div>
            <div class="sub" style="margin-top:6px">库里没有，就当场画一张；一日至多六张</div>
            <div class="sub" id="genHint" style="margin-top:4px;color:var(--warm)"></div>
            <button id="genRetry" class="ghost" style="display:none;margin-top:6px;padding:4px 10px;font-size:11.5px">重新检测</button>
          </div>
        </div>
      </div>

      <div class="card danger">
        <h2>清空记忆 <span class="note">一旦抹去便不再回来 · 会先备份到 data/backup/</span></h2>
        <div class="sub" style="margin-bottom:10px">对话、事实、摘要、承诺、决策一并抹去；人设与图片不动。</div>
        <button id="btnClear" class="dangerBtn">清空所有记忆</button>
        <span id="clearMsg" class="sub" style="margin-left:10px"></span>
      </div>
    </section>


  </div>
</main>
</div>

<div class="disclaimer">
  <span class="warn">⚠</span>
  <b>ta 不是人</b>
  <span class="dotSep">·</span>
  <span>屏幕那边的每一句，都是算出来的</span>
  <span class="dotSep">·</span>
  <span>记得再清楚，也只是一段会断的记录 —— 别投入真感情</span>
</div>

<div id="mask" class="mask">
  <div class="dlg">
    <div class="dTitle" id="dTitle"></div>
    <div class="dBody" id="dBody"></div>
    <div class="dBtns">
      <button class="ghost" id="dNo">再想想</button>
      <button id="dYes">就这样</button>
    </div>
  </div>
</div>
<script>
const $=id=>document.getElementById(id);
const FIELDS=['name','endpoint','api_key','model','channel','target','target_id','account','persona'];
const dirty=new Set();          // 用户动过的字段，自动刷新不再覆盖
let maskedKey='';

FIELDS.forEach(k=>{
  const el=$(k); if(!el) return;
  el.addEventListener('input',()=>{dirty.add(k); markDirty();});
  el.addEventListener('change',()=>{dirty.add(k); markDirty();});
});
['proactive','silence_hours','image_gen'].forEach(k=>{
  const el=$(k); if(!el) return;
  el.addEventListener('input',()=>{dirty.add(k); markDirty();});
  el.addEventListener('change',()=>{dirty.add(k); markDirty();});
});

function markDirty(){
  $('dirtyHint').textContent = dirty.size ? '还有没落笔的改动' : '';
}

async function api(path,body){
  const r=await fetch(path,{method:body?'POST':'GET',
    headers:{'Content-Type':'application/json'},
    body:body?JSON.stringify(body):undefined});
  return r.json();
}

function fill(s){
  try{maybeRenderSections(s)}catch(e){}
  if($('prompt_persona') && s.prompt_persona!==undefined) $('prompt_persona').checked=!!s.prompt_persona;
  for(const k of FIELDS){
    if(k==='api_key'||dirty.has(k)) continue;   // 关键：别覆盖用户正在改的字段
    if(s[k]!==undefined) $(k).value=s[k];
  }
  maskedKey=s.api_key_masked||'';
  if(!dirty.has('proactive')) $('proactive').checked = s.proactive !== false;
  if(!dirty.has('image_gen') && GENOK) $('image_gen').checked = s.image_gen !== false;
  if(!dirty.has('silence_hours')) $('silence_hours').value = (s.silence_hours ?? 6);
  $('api_key').placeholder = maskedKey ? ('已保存 '+maskedKey+'，留空不改') : 'sk-...';
  $('dot').className='dot '+(s.running?'on':'off');
  $('status').textContent=s.running?('醒着 · '+dur(s.uptime_s)):(s.last_error?'走神了：'+s.last_error:'睡着了');
  $('log').textContent=(s.log||[]).join('\\n');
}

async function refresh(){
  const s=await api('/api/state');
  fill(s);
  if(!window.__verChecked){ window.__verChecked=true; setTimeout(checkVer, 400); }
  const d = await api('/api/decisions');
  const actName = {reply:'应了声', skip:'没作声', quiet:'歇着了'};
  const actColor = {reply:'#4ade80', skip:'#f0a020', quiet:'#6b7280'};
  $('decisions').innerHTML = (d.items||[]).map(x=>
    `<div style="display:flex;gap:10px;padding:2px 0">
       <span style="color:#6b7280;flex:none">${x.time}</span>
       <span style="color:${actColor[x.act]||'#aab3c2'};flex:none;width:52px">${actName[x.act]||x.act}</span>
       <span style="color:#6b7280;flex:1">${esc(x.reason)}${x.delay?(' · 等 '+x.delay+'s'):''}</span>
     </div>`).join('') || '<div style="color:#6b7280">（还没有留下痕迹）</div>';
  const m=await api('/api/messages');
  const box=$('msgs');
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  const prevTop = box.scrollTop;

  const dayName = o => o===0 ? '今天' : o===1 ? '昨天' : o===2 ? '前天' : '';
  const shortDay = d => { const p=String(d.day||'').split('-'); return p.length>2 ? (parseInt(p[1])+'月'+parseInt(p[2])+'日') : d.day; };
  const row = x => {
    const her = x.role !== 'user';
    return `<div class="msgRow ${her?'her':'him'}">
      <input type="checkbox" class="msgChk" data-id="${x.id}"${selectedMsgs.has(x.id)?' checked':''}>
      <div class="bubble">
        <div class="txt">${esc(x.text)}</div>
        <span class="tm">${x.time}</span>
        <button class="del msgDel" data-id="${x.id}" title="删除这条">×</button>
      </div>
    </div>`;
  };

  const all = m.days || [];
  const sig = all.map(d=>d.day+':'+d.count+':'+(d.items[d.items.length-1]||{}).id).join('|')
              + '#' + pickedDay;
  if(sig === lastMsgSig) return;      // 没新消息就不重画，省得闪
  lastMsgSig = sig;

  const bar=$('dayBar');
  if(bar){
    const chip=(key,label,n)=>`<div class="dayChip${pickedDay===key?' on':''}" data-day="${key}">${label}`+
      (n?`<span class="n">${n}</span>`:'')+`</div>`;
    bar.innerHTML = chip('all','全部') +
      all.map(d=>chip(d.day, d.offset<3?dayName(d.offset):shortDay(d), d.count)).join('');
    bar.querySelectorAll('.dayChip').forEach(el=>{
      el.onclick=()=>{ pickedDay=el.dataset.day; refresh(); };
    });
  }
  const days = pickedDay==='all' ? all : all.filter(d=>d.day===pickedDay);
  box.innerHTML = days.length ? days.map(d=>
    `<div class="daySep">
       <span class="l"></span>
       <span class="d">${dayName(d.offset)||d.day}</span>
       <span class="c">${d.offset>2?d.day:''} ${d.count} 条</span>
       <span class="l"></span>
     </div>` + d.items.map(row).join('')
  ).join('') : (pickedDay==='all'
   ? '<div class="empty">这里还空着<br><span>ta 尚未开口</span></div>'
   : '<div class="empty">这一天没有留下话<br><span>点「全部」回去</span></div>');

  // 停在底部就跟着新消息走，翻历史时不要被自动刷新拽回去
  box.scrollTop = atBottom ? box.scrollHeight : prevTop;

  box.querySelectorAll('.msgChk').forEach(el=>{
    el.addEventListener('change', ()=>{
      const id = Number(el.dataset.id);
      if(el.checked) selectedMsgs.add(id); else selectedMsgs.delete(id);
      updateSelCount();
    });
  });

  box.querySelectorAll('.msgDel').forEach(el=>{
    el.addEventListener('click', async ()=>{
      if(!await ask('删掉这一句？','删掉之后 <b>ta 就不会记得</b>这句话了。')) return;
      await api('/api/messages/delete',{id:Number(el.dataset.id)});
      refresh();
    });
  });

}

function esc(s){
  return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

// ── 批量删除 ──────────────────────────────────────────
const selectedMsgs = new Set();
let pickedDay = 'all';      // 看全部，还是只看某一天
let lastMsgSig = '';        // 上次画的内容指纹，没变就不重画

function updateSelCount(){
  const n = selectedMsgs.size;
  $('msgSelCount').textContent = n ? ('已选 ' + n + ' 条') : '';
}

$('msgAll').addEventListener('change', e=>{
  const on = e.target.checked;
  document.querySelectorAll('.msgChk').forEach(el=>{
    el.checked = on;
    const id = Number(el.dataset.id);
    if(on) selectedMsgs.add(id); else selectedMsgs.delete(id);
  });
  updateSelCount();
});

$('msgDelSel').addEventListener('click', async ()=>{
  if(!selectedMsgs.size) return;
  if(!await ask('删掉这几句？','删掉之后 <b>ta 就不会记得</b>这 ' + selectedMsgs.size + ' 句话了。')) return;
  await api('/api/messages/delete', {ids: Array.from(selectedMsgs)});
  selectedMsgs.clear();
  $('msgAll').checked = false;
  updateSelCount();
  refresh();
});

let GENOK=true;
async function checkGen(){
  try{ const r=await api('/api/imagegen'); GENOK=!!(r&&r.ok); }
  catch(e){ GENOK=false; }
  const cb=$('image_gen'), hint=$('genHint'), rt=$('genRetry');
  if(cb){
    cb.disabled=!GENOK;
    if(!GENOK) cb.checked=false;
    const lb=cb.closest('label'); if(lb) lb.style.opacity=GENOK?1:.45;
  }
  if(hint) hint.textContent=GENOK?'':'没听见画笔的声音（本机 8100 未起），暂时勾不了';
  if(rt) rt.style.display=GENOK?'none':'inline-block';
}

function dur(sec){
  const t=Math.max(0,Math.floor(sec||0));
  if(t<60) return t+' 秒';
  const m=Math.floor(t/60), r=t%60;
  if(m<60) return m+' 分'+(r?r+' 秒':'');
  const h=Math.floor(m/60);
  return h+' 小时'+(m%60?m%60+' 分':'');
}

function ask(title, body){
  return new Promise(res=>{
    $('dTitle').textContent=title;
    $('dBody').innerHTML=body;
    const m=$('mask');
    m.classList.add('on');
    const done=v=>{
      m.classList.remove('on');
      $('dYes').onclick=null; $('dNo').onclick=null;
      res(v);
    };
    $('dYes').onclick=()=>done(true);
    $('dNo').onclick=()=>done(false);
  });
}

function readForm(){
  return {endpoint:$('endpoint').value,api_key:$('api_key').value,model:$('model').value,
          name:$('name').value,channel:$('channel').value,target:$('target').value,
          target_id:$('target_id').value,account:$('account').value,
          proactive:$('proactive').checked,prompt_persona:$('prompt_persona').checked,
          ...(GENOK?{image_gen:$('image_gen').checked}:{}),
          silence_hours:parseFloat($('silence_hours').value)||6};
}

let riskAcknowledged=false;   // 本次打开页面里确认过一次就够了

$('btnStart').onclick=async()=>{
  if($('channel').value==='wechatauto' && !riskAcknowledged){
    const ok=await ask('接微信要自己承担风险',
      '它读微信本地库、用无障碍驱动窗口发消息，<b>不是官方接口</b>。<br><br>' +
      '<b>可能发生的事：</b><br>' +
      '· 微信被<b>意外登出</b>，要重新扫码（比较常见）<br>' +
      '· 账号被限制或封禁（概率低但存在）<br>' +
      '· 消息发重、发错对象<br><br>' +
      '建议先用小号或备用号跑几天看看。<br>' +
      '出问题先点「睡下」，再关掉微信重新登录。');
    if(!ok) return;
    riskAcknowledged=true;
  }
  await api('/api/settings',readForm());
  const pr = await api('/api/persona',{sections:collectSections()});
  if(!pr.ok){ alert('人设没保存：' + (pr.message||'内容变少了') + ' —— 请到人设页确认'); }
  $('api_key').value='';
  dirty.clear(); markDirty();
  const r=await api('/api/start',{});
  if(!r.ok) alert(r.message);
  refresh();
};

$('btnStop').onclick=async()=>{await api('/api/stop',{});refresh();};

// ── 新版本 ──────────────────────────────────────────────
let lastVer={current:'',latest:'',notes:'',zip:'',has_new:false};
async function checkVer(){
  const el=$('verChip');
  if(!el) return;
  let r=null;
  try{ r=await api('/api/update'); }catch(e){ r=null; }
  lastVer=r||lastVer;
  if(r && r.has_new){
    el.hidden=false;
    el.textContent='↓ 新版本 ' + r.latest;
    el.title=r.notes||'';
  }else{
    el.hidden=true;
  }
}

$('verChip').onclick=async()=>{
  const v=lastVer;
  if(!v||!v.has_new) return;
  const ok=await ask('更新到 '+esc(v.latest)+'？',
    (v.notes? esc(v.notes)+'<br><br>' : '')+
    '会覆盖<b>代码和文档</b>；你的<b>对话、记忆、人设、图片都不动</b>。<br>'+
    '更新前会自动备份到 data/backup/。<br><br>'+
    '<span class="soft">更新完要关掉黑窗口，重新双击 start.bat 才生效。</span>');
  if(!ok) return;
  const r=await api('/api/update',{});
  alert(r.message||(r.ok?'已更新':'更新失败'));
  if(r.ok){ $('verChip').hidden=true; }
  refresh(); checkVer();
};

$('btnPersona').onclick=async()=>{
  const ok = await ask('把 ta 写下来',
    '落笔之前，先留一点不好看的地方。<br><br>' +
    'ta 不会永远接得住你的话 —— <b>有时候讲不通，也哄不好</b>；<br>' +
    '会累，会口误，会有一整天不想理人；<br>' +
    '会有黑眼圈、没洗的杯子、说到一半就停了的那句。<br><br>' +
    '<span class="soft">完美的人只适合看，不适合待。留些缝隙，才住得进去。</span>');
  if(!ok) return;
  let r = await api('/api/persona',{sections:collectSections()});
  if(!r.ok){
    const go = await ask('要这样保存吗？',
      esc(r.message||'内容变少了') + '<br><br>' +
      '<span class="soft">建议点「再想想」回去看一眼，别把写好的弄丢了。</span>');
    if(!go) return;
    r = await api('/api/persona',{sections:collectSections(), force:true});
    if(!r.ok){ alert(r.message||'保存失败'); return; }
  }
  dirty.delete('persona'); markDirty();
  refresh();
};

function Gallery(prefix, kind){
  const $c=$(prefix+'Count'), $g=$(prefix+'Grid'), $f=$(prefix+'File'),
        $b=$(prefix+'Upload'), $m=$(prefix+'Msg');

  async function load(){
    const d = await api('/api/' + kind);
    const list = d.items || [];
    $c.textContent = '（' + list.length + ' 张）';
    $g.innerHTML = list.map(s=>`
      <div style="background:#0f1115;border:1px solid #232833;border-radius:8px;padding:8px">
        <img src="/${kind}/${encodeURIComponent(s.name)}" style="width:100%;height:100px;object-fit:contain;background:#fff;border-radius:6px">
        <input class="galNote" data-name="${s.name}" value="${String(s.note||'').replace(/"/g,'&quot;')}" placeholder="备注：它是什么" style="margin-top:6px;font-size:12px;padding:5px 7px">
        <div class="row" style="margin-top:6px">
          <span style="font-size:11px;color:#6b7280;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${s.name}</span>
          <button class="ghost galDel" data-name="${s.name}" style="padding:2px 8px;font-size:12px">删</button>
        </div>
      </div>`).join('') || '<div class="sub" style="margin:0">还没有，先上传几张</div>';

    $g.querySelectorAll('.galNote').forEach(el=>{
      el.addEventListener('change', async ()=>{
        await api('/api/' + kind + '/note', {name: el.dataset.name, note: el.value});
      });
    });
    $g.querySelectorAll('.galDel').forEach(el=>{
      el.addEventListener('click', async ()=>{
        if(!await ask('删掉这张？', '「' + esc(el.dataset.name) + '」将从库里移除。')) return;
        await api('/api/' + kind + '/delete', {name: el.dataset.name});
        load();
      });
    });
  }

  $b.onclick = async ()=>{
    const files = $f.files;
    if(!files.length){ $m.textContent = '先点左边的「选择文件」挑图'; return; }
    $b.disabled = true; $b.textContent = '上传中…';
    let okCount = 0;
    try{
      for(const file of files){
        $m.textContent = '正在处理 ' + file.name + ' …';
        if(file.size > 5 * 1024 * 1024){ $m.textContent = file.name + ' 超过 5MB，跳过'; continue; }
        const b64 = await new Promise(res=>{
          const r = new FileReader();
          r.onload = ()=>res(String(r.result || '').split(',')[1] || '');
          r.onerror = ()=>res('');
          r.readAsDataURL(file);
        });
        if(!b64){ $m.textContent = file.name + ' 读取失败'; continue; }
        const resp = await api('/api/' + kind + '/upload', {name: file.name, data: b64});
        if(resp.ok) okCount++;
        else $m.textContent = '上传失败：' + (resp.error || '未知错误');
      }
    }catch(e){
      $m.textContent = '出错：' + e;
    }finally{
      $b.disabled = false; $b.textContent = '上传';
    }
    $f.value = '';
    await load();
    $m.textContent = okCount ? ('已上传 ' + okCount + ' 张') : ($m.textContent || '没有上传成功');
  };

  return {load};
}

const gStk = Gallery('stk', 'stickers');const gPho = Gallery('pho', 'photos');

refresh();
gStk.load();
gPho.load();
// 正在输入时完全不刷新，避免任何形式的覆盖
setInterval(()=>{
  const a=document.activeElement;
  if(a && ['INPUT','TEXTAREA','SELECT'].includes(a.tagName)) return;
  if(dirty.size) return;
  refresh();
},3000);

/* ── 人设：按块编辑 ───────────────────── */
let SECS=[], SECKEY='';
function hintText(h){
  return (h||'').split('\\n').map(l=>l.replace(/^> ?/,'').trim()).filter(Boolean).join(' · ');
}
function autoGrow(t){
  t.style.height='auto';
  t.style.height=Math.max(54, t.scrollHeight+2)+'px';
  t.scrollTop=0;
}
function renderSections(secs){
  SECS=secs;
  SECKEY=JSON.stringify(secs.map(x=>[x.id,x.title,x.body]));
  const box=$('secList');
  box.innerHTML=secs.map((s,i)=>{
    const custom = s.id==='_head' || s.id.indexOf('x')===0;
    return '<div class="sec" data-i="'+i+'">'
      + '<div class="h">'
      + (custom ? '<input class="secTitleIn" data-i="'+i+'" value="'+esc(s.title)+'"'+(s.id==='_head'?' readonly':'')+'>'
                : '<span class="t">'+esc(s.title)+'</span>')
      + '<span class="d">'+esc(hintText(s.hint))+'</span>'
      + (s.id==='_head' ? '' : '<button class="rm" data-i="'+i+'">'+(custom?'移除':'清空')+'</button>')
      + '</div>'
      + '<textarea data-i="'+i+'">'+esc(s.body||'')+'</textarea>'
      + '</div>';
  }).join('');
  box.querySelectorAll('textarea').forEach(t=>{t.oninput=()=>autoGrow(t);autoGrow(t);});
  box.querySelectorAll('.rm').forEach(b=>{
    b.onclick=()=>{
      const i=Number(b.dataset.i), cur=collectSections();
      if(SECS[i].id.indexOf('x')===0) cur.splice(i,1); else cur[i].body='';
      renderSections(cur);
    };
  });
}
function collectSections(){
  const box=$('secList');
  return Array.from(box.querySelectorAll('.sec')).map(el=>{
    const i=Number(el.dataset.i), s=SECS[i]||{id:'x'+i,title:'',hint:''};
    const ti=el.querySelector('.secTitleIn');
    return {id:s.id, title:(ti?ti.value.trim():s.title)||s.title,
            hint:s.hint||'', body:el.querySelector('textarea').value};
  });
}
function maybeRenderSections(s){
  if(!s || !s.sections || !$('secList')) return;
  if($('secList').contains(document.activeElement)) return;
  const key=JSON.stringify(s.sections.map(x=>[x.id,x.title,x.body]));
  if(key!==SECKEY) renderSections(s.sections);
}
document.addEventListener('DOMContentLoaded',()=>{
  const add=$('btnAddSec'), raw=$('btnRaw'), rawSave=$('btnRawSave');
  if(add) add.onclick=()=>{
    const cur=collectSections();
    cur.push({id:'x'+Date.now(),title:'自定义块',hint:'',body:''});
    renderSections(cur);
    const ta=$('secList').querySelector('.sec:last-child textarea');
    if(ta) ta.focus();
  };
  if(raw) raw.onclick=()=>{
    const b=$('rawBox');
    b.style.display = (b.style.display==='none'||!b.style.display) ? 'block' : 'none';
  };
  const bc=$('btnClear');
  if(bc) bc.onclick=async()=>{
    if(!await ask('清空所有记忆？','对话、事实、摘要、承诺、决策 —— <b>一并抹去</b>。<br><br>会先备份到 data/backup/。')) return;
    if(!await ask('最后一次确认','清空之后，<b>ta 就不记得你们聊过什么了</b>。<br><br>没有撤销。')) return;
    bc.disabled=true;
    try{
      const r=await api('/api/memory/clear',{});
      const n=(r.result&&r.result.messages)||0;
      $('clearMsg').textContent='已清空 '+n+' 条消息，备份在 data/backup/';
      selectedMsgs.clear(); refresh();
    }catch(e){ $('clearMsg').textContent='失败：'+e.message; }
    bc.disabled=false;
  };
  if($('genRetry')) $('genRetry').onclick=checkGen;
  checkGen();
  if(rawSave) rawSave.onclick=async()=>{
    let r = await api('/api/persona',{text:$('persona').value});
    if(!r.ok){
      const go = await ask('要这样保存吗？', esc(r.message||'内容变少了'));
      if(!go) return;
      r = await api('/api/persona',{text:$('persona').value, force:true});
      if(!r.ok){ alert(r.message||'保存失败'); return; }
    }
    alert('原文已保存');
    refresh();
  };
});

/* ── 侧栏切页 ──────────────────── */
(function(){
  const nav=document.getElementById('nav');
  if(!nav) return;
  const titles={chat:['回忆','ta 说过的、听过的，一天一天落在这里'],
                pulse:['此刻','说与没说，正在发生'],
                media:['图册','ta 收着的那点小心思'],
                persona:['ta 是谁','写完这一页，下一句就是新的 ta'],
                settings:['设置','声音、门路，与分寸'],
                };
  const pages=document.querySelectorAll('.page');
  function show(name){
    pages.forEach(p=>p.classList.toggle('active',p.dataset.page===name));
    nav.querySelectorAll('a').forEach(a=>a.classList.toggle('active',a.dataset.page===name));
    const t=titles[name]||['',''];
    const h=document.getElementById('pageTitle'), s=document.getElementById('pageSub');
    if(h) h.textContent=t[0];
    if(s) s.textContent=t[1];
    if(name==='settings' && typeof checkGen==='function') checkGen();
    try{localStorage.setItem('somniaPage',name)}catch(e){}
  }
  nav.addEventListener('click',e=>{
    const a=e.target.closest('a[data-page]'); if(!a) return;
    e.preventDefault(); show(a.dataset.page);
  });
  let saved='chat';
  const LEGACY={decide:'pulse', log:'pulse'};
  const q=new URLSearchParams(location.search).get('page') || (location.hash||'').replace('#','');
  if(q) saved=q;
  else { try{saved=localStorage.getItem('somniaPage')||'chat'}catch(e){} }
  if(LEGACY[saved]) saved=LEGACY[saved];
  if(!document.querySelector('.page[data-page="'+saved+'"]')) saved='chat';
  show(saved);
})();
</script></body></html>"""

# 页面版本号：改完 JS 后用它确认浏览器拿到的是新页面
BUILD = time.strftime("%m-%d %H:%M:%S")
PAGE = PAGE.replace("__BUILD__", BUILD)


class Handler(BaseHTTPRequestHandler):
    app: PanelApp = None

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else (
            body.encode("utf-8") if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # 千万别让浏览器缓存面板：改了 JS 还看到旧行为，是最难查的一类问题
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if p == "/api/state":
            return self._send(200, self.app.state())
        if p == "/api/messages":
            return self._send(200, {"days": recent_messages()})
        if p == "/api/update":
            return self._send(200, self.app.check_update())
        if p == "/api/imagegen":
            return self._send(200, self.app.image_gen_status())
        if p == "/api/decisions":
            return self._send(200, {"items": recent_decisions()})
        if p in ("/api/stickers", "/api/photos"):
            kind = p.rsplit("/", 1)[1]
            return self._send(200, {"items": image_book(kind).items()})
        if p.startswith("/stickers/") or p.startswith("/photos/"):
            kind, _, name = p.strip("/").partition("/")
            return self._send_image(kind, name)
        self._send(404, {"error": "not found"})

    def _send_image(self, kind, raw_name):
        # 文件名可能是中文，浏览器会百分号编码，这里要解回来
        name = safe_name(urllib.parse.unquote(raw_name))
        path = image_dir(kind) / name
        try:
            data = path.read_bytes()
        except OSError:
            return self._send(404, {"error": "not found"})
        ctype = MIME.get(path.suffix.lower(), "application/octet-stream")
        self._send(200, data, ctype)

    def do_POST(self):
        p = self.path.split("?")[0]
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return self._send(400, {"error": "bad length"})
        if n > MAX_BODY:
            # 先把对方发的东西读掉一部分再回 413，否则浏览器只会看到连接被掐断
            left = min(n, MAX_BODY * 2)
            while left > 0:
                chunk = self.rfile.read(min(65536, left))
                if not chunk:
                    break
                left -= len(chunk)
            return self._send(413, {"error": f"太大了（上限 {MAX_BODY // 1024} KB）"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad json"})
        if not isinstance(body, dict):
            return self._send(400, {"error": "body 应该是对象"})

        if p == "/api/settings":
            self.app.update_settings(body)
            return self._send(200, {"ok": True})
        if p == "/api/update":
            ok, msg = self.app.apply_update()
            return self._send(200, {"ok": ok, "message": msg})
        if p == "/api/memory/clear":
            return self._send(200, {"ok": True, "result": self.app.clear_memory()})
        if p == "/api/persona":
            force = bool(body.get("force"))
            if isinstance(body.get("sections"), list):
                ok, msg = self.app.save_persona_sections(body["sections"], force=force)
            elif isinstance(body.get("text"), str):
                ok, msg = self.app.save_persona(body["text"], force=force)
            else:
                # 既没 sections 也没 text —— 以前会当成空文本写进去，等于把人设抹了
                return self._send(400, {"ok": False, "message": "没有内容"})
            return self._send(200, {"ok": ok, "message": msg})
        if p == "/api/start":
            ok, msg = self.app.start()
            return self._send(200, {"ok": ok, "message": msg})
        if p == "/api/stop":
            ok, msg = self.app.stop()
            return self._send(200, {"ok": ok, "message": msg})
        if p == "/api/messages/delete":
            from . import store
            c = store.connect(Config.from_env().db_path)
            def _ids(v):
                if isinstance(v, bool) or not isinstance(v, (int, str)):
                    return None
                try:
                    return [int(x) for x in (v if isinstance(v, list) else [v])]
                except (TypeError, ValueError):
                    return None
            if isinstance(body.get("ids"), list):
                ids = _ids(body["ids"])
                if ids is None:
                    return self._send(400, {"ok": False, "error": "ids 里有不是数字的东西"})
                n = store.delete_messages(c, ids)
                return self._send(200, {"ok": n > 0, "deleted": n})
            ids = _ids(body.get("id", 0))
            if ids is None:
                return self._send(400, {"ok": False, "error": "id 不是数字"})
            ok = store.delete_message(c, ids[0])
            return self._send(200, {"ok": ok, "deleted": 1 if ok else 0})
        if p.startswith("/api/stickers/") or p.startswith("/api/photos/"):
            _, _, kind, action = p.split("/", 3)
            if action == "upload":
                return self._upload_image(kind, body)
            if action == "note":
                ok = image_book(kind).set_note(body.get("name", ""), body.get("note", ""))
                return self._send(200, {"ok": ok})
            if action == "delete":
                ok = image_book(kind).delete(body.get("name", ""))
                return self._send(200, {"ok": ok})
        self._send(404, {"error": "not found"})

    def _upload_image(self, kind, body):
        try:
            raw = base64.b64decode(body.get("data", ""), validate=False)
            name = image_book(kind).add(body.get("name", "image.png"), raw,
                                        body.get("note", ""))
            return self._send(200, {"ok": True, "name": name})
        except Exception as e:
            return self._send(400, {"ok": False, "error": f"{type(e).__name__}: {e}"})


def recent_messages(limit: int = 400, days: int = 7):
    """按天分组返回。记忆本来是按天的，面板也照这个分。"""
    try:
        from . import store
        from .config import Config
        c = store.connect(Config.from_env().db_path)
        return store.messages_by_day(c, days=days, per_day=limit)
    except Exception:
        return []


def recent_decisions(limit: int = 10):
    """最近的判断记录。ta 为什么没开口，看这里。"""
    try:
        from . import store
        c = store.connect(Config.from_env().db_path)
        rows = list(c.execute(
            "SELECT ts, act, reason, delay FROM decisions ORDER BY id DESC LIMIT ?",
            (limit,)))
        out = []
        for r in reversed(rows):
            out.append({
                "time": time.strftime("%m-%d %H:%M", time.localtime(r["ts"])),
                "act": r["act"],
                "reason": r["reason"] or "",
                "delay": round(r["delay"] or 0, 1),
            })
        return out
    except Exception:
        return []


def port_taken(host: str, port: int) -> bool:
    """端口是否已被占用。

    Windows 下 http.server 默认 allow_reuse_address，两个进程能绑同一端口，
    请求随机落到其中一个 —— 表现为"面板里显示没在跑，其实另一个在跑"。
    所以启动前先独占试绑一次。
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind((host, port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def serve(host: str = "127.0.0.1", port: int = 8765, app: PanelApp = None):
    Handler.app = app or PanelApp()
    srv = ThreadingHTTPServer((host, port), Handler)
    return srv


def main(argv=None):
    ap = argparse.ArgumentParser(prog="somnia.panel")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-autostart", action="store_true",
                    help="只开面板，不自动启动机器人")
    a = ap.parse_args(argv)

    if sys.version_info < (3, 11):
        print(f"需要 Python 3.11 以上（当前 {sys.version.split()[0]}）")
        print("3.10 及以下没有 tomllib，somnia.toml 会被整份忽略。")
        raise SystemExit(2)

    if port_taken(a.host, a.port):
        print(f"端口 {a.port} 已被占用，可能已经有一个面板在跑：")
        print(f"  http://{a.host}:{a.port}")
        print("先关掉那个（或换 --port），不要在同一个端口上开两个实例。")
        raise SystemExit(2)

    app = PanelApp()
    srv = serve(a.host, a.port, app)
    url = f"http://{a.host}:{a.port}"
    print("\n  ta 不是人 · 这里的每一句都是算出来的 · 别投入真感情\n")
    print(f"Somnia·眠 控制台：{url}")

    first_run = not SETTINGS_FILE.exists() or not S.load().get("channel")
    if a.no_autostart or first_run:
        # 第一次跑（还没存过设置）不要自动接微信 —— 那时账号、对象都没配，
        # 接了只会读到一堆不认识的消息
        print("（未自动启动：先在页面上填好模型密钥和通道，再点「唤醒」）"
              if first_run and not a.no_autostart
              else "（未自动启动，请在页面上点「保存并启动」）")
    else:
        ok, msg = app.start()
        print(("已自动启动：" if ok else "未自动启动：") + msg)

    print("Ctrl-C 或关闭进程即停止。")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
        srv.server_close()
        print("\n已停止")


if __name__ == "__main__":
    main()
