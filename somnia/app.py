"""主循环：把 Channel / Brain / Memory / Composer 串起来。

一次消息的时序：
    recv → 去重 → 记忆 → 决策 → (延迟/正在输入) → 取记忆 → 生成 → Lint → 分段发送
"""

import argparse
import os
import random
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

# Windows 控制台默认不是 UTF-8，中文会变乱码
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

from . import (store, brain, memory, composer, scheduler, stickers, imagegen,
               promises, aging)
from .config import Config
from .gateway import Gateway
from .persona import load as load_persona
from .channel.cli import CliChannel


def make_alert(cfg: Config):
    """静默死亡是最伤体验的失败模式，所以要能喊出来。"""
    from pathlib import Path

    log = Path(cfg.db_path).parent / "alert.log"

    def alert(msg: str):
        line = f"{datetime.now().isoformat(timespec='seconds')}  {msg}"
        print(f"  [告警] {msg}", flush=True)
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        hook = os.environ.get("SOMNIA_ALERT_WEBHOOK")
        if hook:
            try:
                req = urllib.request.Request(
                    hook, data=msg.encode("utf-8"),
                    headers={"Content-Type": "text/plain; charset=utf-8"})
                urllib.request.urlopen(req, timeout=5).read()
            except Exception:
                pass

    return alert


def build_channel(cfg: Config, kind: str, alert, since_ts: float = 0.0):
    if kind == "cli":
        return CliChannel(cfg.name or "ta")

    if kind == "claw":
        from .channel.claw import ClawChannel, UrllibTransport, load_credentials
        token, user_id = cfg.claw_token, cfg.claw_user_id
        if not token:
            token, from_file = load_credentials()
            user_id = user_id or from_file
        if not token:
            sys.exit("缺 bot_token：先跑 `python ..\\脚本\\claw_login.py` 扫码，"
                     "或设置环境变量 SOMNIA_CLAW_TOKEN")
        return ClawChannel(
            token,
            transport=UrllibTransport(cfg.claw_base),
            alert_hook=alert,
            user_id=user_id,
        ).start()

    if kind == "wechatauto":
        from .channel.wechatauto import WechatAutoChannel
        target = cfg.wxauto_target or os.environ.get("SOMNIA_WA_TARGET", "")
        if not target:
            sys.exit("缺联系方式：设置 SOMNIA_WA_TARGET 为要挂的联系人备注名")
        return WechatAutoChannel(
            target,
            alert_hook=alert,
            my_username=cfg.wxauto_username,
            account=cfg.wxauto_account,
            target_id=cfg.wxauto_target_id,
            since_ts=since_ts,
        ).start()

    sys.exit(f"未知通道：{kind}")


def _wake_loop(cfg: Config, llm, alert, ch=None, send_lock=None, stop_event=None):
    """后台唤醒线程。独立连接——sqlite 连接不能跨线程共用。

    如果通道能"主动开口"（wechatauto 可以，claw 不行），
    念头够格就直接发出去，而不是只存着等下轮。
    """
    conn = store.connect(cfg.db_path)
    can_initiate = bool(getattr(ch, "can_initiate", False)) if ch else False
    can_initiate = can_initiate and bool(getattr(cfg, "proactive", True))
    lock = send_lock or threading.Lock()

    interval = max(60, cfg.scheduler_interval_min * 60)
    while True:
        # 睡下之后这条线程必须退出，否则她还会自己冒出来说话 —— 那就等于"睡着了还被叫醒"
        if stop_event is not None:
            if stop_event.wait(interval):
                return
        else:
            time.sleep(interval)
        if stop_event is not None and stop_event.is_set():
            return
        try:
            store.purge_expired(conn)       # 到期的记忆自己忘掉
            r = scheduler.tick(store, conn, cfg, datetime.now(), llm,
                               persona=load_persona(cfg.persona_dir))
            if not r:
                continue
            thought, score, reason, oid = r
            print(f"  〔念头·{reason}〕{thought}  score={score}", flush=True)

            if not can_initiate or score < cfg.outbox_threshold:
                continue

            if stop_event is not None and stop_event.is_set():
                return                      # 正要开口时被按下"睡下"
            with lock:
                try:
                    ch.typing(True)
                    ch.send(thought)
                    store.add_message(conn, "assistant", thought)
                    store.outbox_deliver(conn, [oid])
                    print(f"  〔主动说了〕{thought}", flush=True)
                except Exception as e:
                    alert(f"主动发送失败：{e}")
        except Exception as e:
            alert(f"调度器异常：{e}")


def _fire_sticker(cfg: Config, ch, path, send_lock, alert, box_busy, tag="表情",
                  delete_after=False):
    """表情/照片走后台线程：发图实测要 30 秒，不能卡住下一条消息。

    delete_after=True 用于照片：发过就删，避免下次又发同一张。
    """
    box = None
    try:
        with send_lock:
            ch.send_image(path)
        c2 = store.connect(cfg.db_path)
        try:
            store.add_message(c2, "assistant", f"[{tag}] {path.name}")
        finally:
            c2.close()
        print(f"  〔{tag}〕{path.name}", flush=True)
        if delete_after:
            from .stickers import StickerBook
            box = StickerBook(cfg.photo_dir)
            if box.delete(Path(path).name):
                print(f"  〔已用掉〕{Path(path).name}", flush=True)
    except Exception as e:
        alert(f"{tag}发送失败：{e}")
    finally:
        box_busy.clear()


def _fire_generated(cfg: Config, ch, photo_box, desc, send_lock, alert, box_busy):
    """库里没有这张 → 现场生成一张，存进库里再发。

    生成 25-45 秒 + 发送 30 秒，全程后台，绝不阻塞主循环。
    """
    try:
        data = imagegen.generate(desc, cfg.image_gen_url)
        name = f"生成_{time.strftime('%m%d_%H%M%S')}.png"
        path = photo_box.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        photo_box.set_note(name, (desc or "")[:40])

        with send_lock:
            ch.send_image(path)

        c2 = store.connect(cfg.db_path)
        try:
            store.add_message(c2, "assistant", f"[照片] {name}")
        finally:
            c2.close()
        print(f"  〔生成并发送〕{desc} → {name}", flush=True)
        # 发过就删：她要发下一张就得重新生成，不会反复贴同一张
        photo_box.delete(name)
        print(f"  〔已用掉〕{name}", flush=True)
    except Exception as e:
        alert(f"生成照片失败：{type(e).__name__}: {e}")
    finally:
        box_busy.clear()


REPROACH_WORDS = ("怎么不找我", "怎么才回", "不想理我", "是不是忘了我",
                  "都不找我", "才想起我", "等你半天")


def _extra_note(cfg, d, conn) -> str:
    """一次生成额外的指令：被吵醒 / 今天已经抱怨过就别重复。"""
    parts = []
    note = _wake_note(d)
    if note:
        parts.append(note)
    if store.count_said_today(conn, REPROACH_WORDS) >= 1:
        parts.append("\n今天已经抱怨过一次他没找你了。这次别再提，正常说话。")
    return "".join(parts)


def _wake_note(d) -> str:
    """被吵醒时的额外指令。她该是迷糊的，不是精神饱满地聊。"""
    if getattr(d, "reason", "") != "woken":
        return ""
    return ("\n\n你是被这条消息吵醒的，脑子还没转过来。"
            "回得很短（一两个字也行），可以说明天再聊，别装热情。")


def _is_question(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return t.endswith(("?", "？", "吗", "呢", "么")) or "？" in t or "?" in t


def _generated_today(photo_box) -> int:
    """今天已经现场生成过几张（文件名带日期，不用另开表）。"""
    prefix = "生成_" + time.strftime("%m%d")
    try:
        return sum(1 for _ in photo_box.dir.glob(prefix + "*.png"))
    except OSError:
        return 0


def needs_catchup(lu, la, now: float, delay: float, max_age: float) -> bool:
    """他最后一条是否还没被回应，且已经过了该补的时间。

    补一句的前提是"她这边最后一条不是她自己的"。
    """
    if not lu:
        return False
    if la and la >= lu:
        return False
    age = now - lu
    return delay <= age <= max_age


def maybe_catchup(cfg, conn, llm, ch, send_lock, alert=None, now=None,
                  fast: bool = False) -> bool:
    """尝试补一句。返回是否真的补了。抽出来是为了能测。"""
    now = time.time() if now is None else now
    lu = store.last_user_ts(conn)
    la = store.last_assistant_ts(conn)
    if not needs_catchup(lu, la, now, cfg.catchup_delay_s, cfg.catchup_max_age_s):
        return False
    if scheduler.in_quiet(cfg, datetime.now()):
        return False

    text, _ = store.last_user_message(conn)
    if not text:
        return False

    history, facts, summaries, recall = memory.context(store, conn, cfg, user_text=text)
    persona = load_persona(cfg.persona_dir)
    segs, _st, _ph = composer.generate_turn(
        cfg, history, facts, text, 3, summaries=summaries,
        llm=llm, persona=persona)
    if not segs:
        return False
    # 生成这段时间里主循环可能已经回了，别补重复的
    if (store.last_assistant_ts(conn) or 0) >= lu:
        return False

    with send_lock:
        for i, s in enumerate(segs):
            if i and not fast:
                time.sleep(random.uniform(*cfg.seg_gap))
            ch.send(s)
            store.add_message(conn, "assistant", s)
    # 补回复也是一次回复，记进决策表，面板才对得上
    store.log_decision(conn, None, "reply", 0, len(segs), "catchup")
    print(f"  〔补回〕{'|'.join(segs)}", flush=True)
    return True


def _catchup_loop(cfg: Config, llm, ch, alert, send_lock, stop_event):
    """当时没回，过几分钟自己补一句。

    发完就不满足条件了（她这边会多一条 assistant 消息），所以不会重复。
    """
    conn = store.connect(cfg.db_path)
    while True:
        time.sleep(20)
        if stop_event is not None and stop_event.is_set():
            return
        try:
            if not cfg.catchup_enabled:
                continue
            maybe_catchup(cfg, conn, llm, ch, send_lock, alert)
        except Exception as e:
            alert(f"补回复失败：{type(e).__name__}: {e}")


def _age_async(cfg: Config, llm, alert):
    """把三天前的原文搬进档案，留下印象。每轮聊完跑一次，很轻。"""
    try:
        conn = store.connect(cfg.db_path)
        try:
            n = aging.roll(store, conn, cfg, llm)
        finally:
            conn.close()
        if n.get("archived"):
            print(f"  〔归档〕{n['archived']} 段进了档案，留了 {n['summaries']} 条印象",
                  flush=True)
    except Exception as e:
        alert(f"记忆归档失败：{type(e).__name__}: {e}")


def _compress_async(cfg: Config, llm, alert):
    """窗口满了就把最老的 200 条压成一段概述。压完这批，下一批才会轮上来。"""
    try:
        if not getattr(cfg, "compress_enabled", True):
            return
        conn = store.connect(cfg.db_path)
        try:
            n = memory.compress(store, conn, llm,
                                keep=cfg.recent_n,
                                batch=getattr(cfg, "compress_batch", 200))
        finally:
            conn.close()
        if n:
            print(f"  〔压缩〕最早的 {n} 条压成了一段概述", flush=True)
    except Exception as e:
        alert(f"压缩记忆失败：{type(e).__name__}: {e}")


def _remember_async(cfg: Config, llm, user_text: str, alert):
    """后台把"值得记的事"抽成事实。规则匹配太窄，交给模型判断。"""
    try:
        conn = store.connect(cfg.db_path)
        try:
            facts = memory.remember_facts(store, conn, user_text, llm)
        finally:
            conn.close()
        if facts:
            print(f"  〔记住〕{'；'.join(facts)}", flush=True)
    except Exception as e:
        alert(f"记事实失败：{type(e).__name__}: {e}")


def run(cfg: Config, fast: bool, offline: bool, kind: str = "cli", window=None,
        scheduler_on: bool = False, stop_event=None, on_channel=None):
    conn = store.connect(cfg.db_path)
    store.purge_expired(conn)               # 启动时先清一遍过期记忆
    try:
        _aged = aging.roll(store, conn, cfg, None)
        if _aged.get('archived'):
            print(f"  〔归档〕启动时收进 {_aged['archived']} 段", flush=True)
    except Exception:
        pass
    alert = make_alert(cfg)
    # 把"她最后记得的时间"传下去：通道重启时据此补回停机期间的消息
    ch = build_channel(cfg, kind, alert, since_ts=store.since_ts(conn))
    if on_channel:
        on_channel(ch)

    if kind == "wechatauto":
        print("  ⚠ 微信真机通道：非官方操作。可能被意外登出（重新扫码即可），极端情况会封号。"
              "建议先用小号。\n")
    print(f"—— {cfg.name or 'ta'} ——（{kind}，Ctrl-C 退出）\n")

    llm = (lambda p: composer.call_model(cfg, p)) if (cfg.api_key and not offline) else None

    # 启动时补做过去几天的摘要（今天还没过完，不做）
    try:
        done = memory.catch_up(store, conn, cfg, llm)
        if done:
            print(f"  补做摘要：{', '.join(done)}\n")
    except Exception as e:
        alert(f"摘要补做失败：{e}")

    send_lock = threading.Lock()

    if cfg.catchup_enabled:
        threading.Thread(target=_catchup_loop,
                         args=(cfg, llm, ch, alert, send_lock, stop_event),
                         daemon=True).start()

    if scheduler_on:
        threading.Thread(target=_wake_loop,
                         args=(cfg, llm, alert, ch, send_lock, stop_event),
                         daemon=True).start()
        print(f"  调度器已启动（每 {cfg.scheduler_interval_min} 分钟检查）\n")

    # 合并窗口：--window 显式指定；否则 --fast 置零，正常用配置值
    win = cfg.debounce if window is None else window
    if fast and window is None:
        win = 0.0
    gw = Gateway(conn, store, window=win)
    box = stickers.StickerBook(cfg.sticker_dir)
    photo_box = stickers.StickerBook(cfg.photo_dir)
    box_busy = threading.Event()
    can_stickers = bool(getattr(ch, "can_send_image", False)) and len(box) > 0
    can_photos = bool(getattr(ch, "can_send_image", False))   # 库空也能现拍
    last_gen = [0.0]        # 上次"现场生成"的时间，用来限频

    while True:
        if stop_event is not None and stop_event.is_set():
            break
        batch = gw.collect(ch)
        if batch is None:
            break
        if not batch:
            continue

        stored = 0
        for t, mid in zip(batch.texts, batch.msg_ids):
            if store.add_message(conn, "user", t, mid):
                stored += 1
                # 记忆不依赖这一轮有没有回话——她可以不回，但会记住
                memory.maybe_remember(store, conn, t)
        if not stored:
            # 全是删过的或重复的消息 —— 别对已经忘掉的话再回应一遍
            print("  〔删过的消息，跳过〕\n")
            continue

        # 他开口了，说明她已经跟他讲上了，"晚上找你"这类不用再补
        store.cancel_promises(conn)

        user_text = batch.text
        vid = ",".join(m for m in batch.msg_ids if m) or None

        now = datetime.now()
        # 连着不回、或者对方在问问题 —— 这两种情况都该接。
        # 夜里：最近 4 小时醒过几次，醒得越多越容易再醒。
        d = brain.plan(cfg, now,
                       last_act=store.last_decision_act(conn),
                       asking=_is_question(user_text),
                       woken_tonight=store.decisions_with_reason_since(
                           conn, "woken", time.time() - 4 * 3600),
                       streak=store.reply_streak(conn))
        store.log_decision(conn, vid, d.act, d.delay, d.segments, d.reason)

        if d.act == "quiet":
            print(f"  〔{cfg.name or 'ta'} 应该睡了〕\n")
            continue
        if d.act == "skip":
            print("  〔已读未回〕\n")
            continue

        history, facts, summaries, recall = memory.context(store, conn, cfg, user_text=user_text)

        # 每轮读一次人设：按 mtime 缓存，改 md 立刻生效
        persona = load_persona(cfg.persona_dir)

        # 先取不标记，等真的发出去了再标记，避免生成失败把念头吞掉
        thoughts = store.outbox_take(conn, cfg.outbox_threshold)
        pending_txt = scheduler.pending_block([t for _, t in thoughts])

        # 先生成，再把剩余延迟补齐 —— 模型耗时算在"正在输入"里，
        # 而不是加在延迟之后（串行会让感知耗时翻倍）。
        t0 = time.monotonic()
        wanted = None
        used_model = not (offline or not cfg.api_key)

        # 频率由代码控制：只在部分回合把清单给她。
        # 写进 prompt 不管用——模型会模仿样例，每句都配。
        offer_stickers = (can_stickers and cfg.sticker_enabled
                          and random.random() < cfg.sticker_offer_prob)
        offer_photos = (can_photos and cfg.photo_enabled
                        and random.random() < cfg.photo_offer_prob)
        menu = box.menu_text() if offer_stickers else ""
        pmenu = photo_box.menu_text(limit=8, slot=stickers.slot_of_hour(datetime.now().hour)) if offer_photos else ""
        if offer_photos and not pmenu:
            pmenu = "（库里暂时没有照片，直接写你眼前看到的东西）"

        wanted_photo = None
        if not used_model:
            segs = composer.offline(cfg, d.segments)
        else:
            try:
                segs, wanted, wanted_photo = composer.generate_turn(
                    cfg, history, facts, user_text, d.segments,
                    summaries=summaries, pending=pending_txt,
                    llm=llm, persona=persona,
                    sticker_menu=menu, photo_menu=pmenu,
                    extra=_extra_note(cfg, d, conn), recall=recall,
                )
                # 她在复读自己 —— 再生成一次，明确要求换说法
                if segs and composer.repeats(segs, history):
                    print("  〔复读了，重来〕", flush=True)
                    segs, wanted, wanted_photo = composer.generate_turn(
                        cfg, history, facts, user_text, d.segments,
                        summaries=summaries, pending=pending_txt,
                        llm=llm, persona=persona,
                        sticker_menu=menu, photo_menu=pmenu,
                        recall=recall,
                        extra=_extra_note(cfg, d, conn) +
                        "\n\n注意：你刚才那段在重复你自己说过的内容。"
                        "换成新的说法，说点没说过的事。",
                    )
            except Exception as e:
                alert(f"生成失败：{e}")
                segs = []

        if not segs:
            print("  〔没回〕\n")
            continue

        remain = d.delay - (time.monotonic() - t0)
        if not fast and remain > 0.3:
            ch.typing(True)
            step = remain / 6
            for _ in range(6):
                time.sleep(step)
                if hasattr(ch, "typing_dot"):
                    ch.typing_dot()
            if hasattr(ch, "typing_end"):
                ch.typing_end()

        for i, s in enumerate(segs):
            if i and not fast:
                time.sleep(random.uniform(*cfg.seg_gap))
            try:
                with send_lock:          # 和调度器共用一把锁，避免同时驱动窗口
                    ch.send(s)
            except NotImplementedError as e:
                print(f"  〔发送未实现〕{e}")
                break
            except Exception as e:
                alert(f"发送异常：{e}")
                break
            store.add_message(conn, "assistant", s)

        store.outbox_deliver(conn, [i for i, _ in thoughts])

        # 她要是答应了什么（"晚上找你""晚点跟你说"），记下来，到点必须兑现
        for s in segs:
            got = promises.first(s)
            if got:
                store.add_promise(conn, got[0], got[1])
                print(f"  〔答应〕{got[0]}", flush=True)
                break

        # 表情 / 照片：由她自己决定发不发、发哪张。
        # 没模型时（离线）退化成随机，否则完全听她的。
        sticker = None
        if can_stickers:
            if wanted:
                sticker = box.find(wanted) or box.pick()
            elif not used_model and random.random() < cfg.sticker_prob:
                sticker = box.pick()

        if sticker and not box_busy.is_set():
            box_busy.set()
            threading.Thread(
                target=_fire_sticker,
                args=(cfg, ch, sticker, send_lock, alert, box_busy, "表情"),
                daemon=True,
            ).start()

        photo = photo_box.find(wanted_photo) if wanted_photo else None
        if photo is not None:
            # 只认她自己的话。用"你说了外卖"来放行是错的：
            # 她可能刚说自己吃完了红烧肉，再发一张外卖照片，前后矛盾。
            note = photo_box.note_of(photo)
            said = " ".join(segs)
            if not stickers.matches_text(note, said, threshold=0.12):
                print(f"  〔照片不搭，跳过〕{note}", flush=True)
                photo = None

        gen_ready = (cfg.image_gen_enabled and can_photos
                     and (cfg.image_gen_cooldown_min <= 0
                          or (time.time() - last_gen[0]) > cfg.image_gen_cooldown_min * 60)
                     and _generated_today(photo_box) < cfg.image_gen_max_per_day)

        if photo and not box_busy.is_set():
            box_busy.set()
            threading.Thread(
                target=_fire_sticker,
                args=(cfg, ch, photo, send_lock, alert, box_busy, "照片", True),
                daemon=True,
            ).start()
        elif wanted_photo and gen_ready and not box_busy.is_set():
            # 她说想给你看某个东西，但库里没有 —— 现场生成一张。
            # 生成同样要过一致性检查：说了"我在写代码"却要拍猫，也不发。
            said = " ".join(segs)
            if not stickers.matches_text(wanted_photo, said, threshold=0.12):
                print(f"  〔照片不搭，跳过〕{wanted_photo}", flush=True)
            else:
                last_gen[0] = time.time()
                box_busy.set()
                threading.Thread(
                    target=_fire_generated,
                    args=(cfg, ch, photo_box, wanted_photo, send_lock, alert, box_busy),
                    daemon=True,
                ).start()

        # 事后抽事实：不阻塞回复，也不占她的"说话"时间
        if used_model and len(user_text) >= 6:
            threading.Thread(target=_remember_async,
                             args=(cfg, llm, user_text, alert), daemon=True).start()

        # 三天前的原文搬进档案，只留印象
        threading.Thread(target=_age_async, args=(cfg, llm, alert),
                         daemon=True).start()

        print()


def _show_prompt(cfg: Config, text: str):
    """打印最终送进模型的 prompt。调人设时用这个，不用真聊。"""
    conn = store.connect(cfg.db_path)
    history, facts, summaries, recall = memory.context(store, conn, cfg, user_text=text)
    p = load_persona(cfg.persona_dir)
    print(f"# 人设来源：{p.source}")
    print(f"# 事实 {len(facts)} 条 / 摘要 {len(summaries)} 天 / 近期 {len(history)} 条")
    print(f"# 名字：{cfg.name or '（没取名字）'}\n")
    print(composer.build_prompt(cfg, history, facts, text, 2, summaries, "", p))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="somnia")
    ap.add_argument("--fast", action="store_true", help="跳过延迟")
    ap.add_argument("--offline", action="store_true", help="不调模型")
    ap.add_argument("--channel", default="cli", choices=["cli", "claw", "wechatauto"])
    ap.add_argument("--token", default="", help="Claw bot_token（也可用 SOMNIA_CLAW_TOKEN）")
    ap.add_argument("--window", type=float, default=None,
                    help="连发合并窗口秒数，覆盖配置")
    ap.add_argument("--reset", action="store_true",
                    help="清空数据库后启动（开发期用）")
    ap.add_argument("--scheduler", action="store_true",
                    help="启动后台唤醒（主动性）")
    ap.add_argument("--show-prompt", metavar="TEXT", default=None,
                    help="打印最终 prompt 后退出（调人设用）")
    a = ap.parse_args(argv)

    if a.token:
        os.environ["SOMNIA_CLAW_TOKEN"] = a.token

    if a.reset:
        from pathlib import Path
        p = Path(Config.from_env().db_path)
        for f in (p, Path(str(p) + "-wal"), Path(str(p) + "-shm")):
            try:
                f.unlink()
            except FileNotFoundError:
                pass
        print(f"已清空 {p}\n")

    if a.show_prompt is not None:
        _show_prompt(Config.from_env(), a.show_prompt)
        return

    try:
        run(Config.from_env(), a.fast, a.offline, a.channel, a.window, a.scheduler)
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()


