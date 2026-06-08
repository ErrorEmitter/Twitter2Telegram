#!/usr/bin/env python3
"""twitter2telegram —— 监控 X 博主新推，翻译成中文，转发到 Telegram 群。

用法：
  python3 main.py --check      # 检查配置与三方连通性（不发消息，建议先跑这个）
  python3 main.py --chat-id    # 列出 bot 见过的会话 id，用来填 telegram.chat_id
  python3 main.py --test       # 端到端自检：往群里发一条测试消息并演示翻译 edit
  python3 main.py --once       # 只跑一轮（调试用）
  python3 main.py              # 常驻轮询（生产，配合 systemd）
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from dataclasses import replace
from pathlib import Path

import publisher
import source
import translator
from config import ConfigError, load_config
from store import Store, tweet_from_row
from util import ApiError, http_json

log = logging.getLogger("t2t")
HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.toml"


# ---------- 轮询主逻辑 ----------

def run_cycle(cfg, store: Store) -> None:
    """一轮：抓新推 → 发原文占位；再跑填充 pass（先翻译后解读，edit 占位）。"""
    for username in cfg.twitter.usernames:
        _poll_user(cfg, store, username)
    _enhance_pending(cfg, store)


# 时间窗重叠：每轮多回看这么多秒，兜住推文 API 索引延迟造成的漏推（seen() 去重，不会重发）
_OVERLAP_SEC = 180
# 首次 backfill 回看窗口：往前查这么久来取最近 N 条
_BACKFILL_LOOKBACK_SEC = 7 * 86400


def _cache_path(cfg, username: str) -> Path:
    return cfg.state_db.parent / f"last_{username}.json"


def _poll_user(cfg, store: Store, username: str) -> None:
    now = int(time.time())
    last = store.get_last_checked(username)
    first_run = last is None

    # 首次且不补发：只建时间基线、不抓取
    if first_run and cfg.twitter.backfill_on_start <= 0:
        store.set_last_checked(username, now)
        log.info("@%s 首次：建立时间基线，仅转发此后的新推", username)
        return

    if first_run:
        since, max_pages = now - _BACKFILL_LOOKBACK_SEC, 1  # 最新一页就够
    else:
        since, max_pages = int(last) - _OVERLAP_SEC, 3      # 查 [上次-重叠, 现在]

    tweets = _fetch_or_alert(cfg, store, username, since, now, max_pages)
    if tweets is None:
        return  # 抓取失败：已按需提示一次，不重试，下轮再来

    if first_run:
        new = list(reversed(tweets))[-cfg.twitter.backfill_on_start:]  # 旧→新，取最新 keep 条
        log.info("@%s 首次：补发最近 %d 条", username, len(new))
    else:
        new = [t for t in reversed(tweets) if not store.seen(t.id)]  # 旧→新，按时间顺序发
        cap = cfg.twitter.max_per_poll
        if cap > 0 and len(new) > cap:
            log.warning("@%s 窗口内 %d 条新推超过上限 %d，丢弃最旧 %d 条", username, len(new), cap, len(new) - cap)
            new = new[-cap:]
        if new:
            log.info("@%s 发现 %d 条新推", username, len(new))

    all_ok = True
    for t in new:
        if not _post_placeholders_one(cfg, store, t):
            all_ok = False  # 发送失败：先不推进游标，下轮重试（已成功的靠 seen() 去重）
    if all_ok:
        store.set_last_checked(username, now)


def _fetch_or_alert(cfg, store: Store, username: str, since_ts: int, until_ts: int, max_pages: int):
    """抓取时间窗。失败时只在首次故障向群提示一次（不重试）；成功且此前故障过则发"已恢复"。"""
    try:
        tweets = source.fetch_window(cfg.twitter, username, since_ts, until_ts, max_pages=max_pages)
    except ApiError as exc:
        log.warning("抓取 @%s 失败（不重试，下轮再试）：%s", username, exc)
        if not store.get_alerted(username):  # 还没提示过才提示，避免重复刷屏
            try:
                publisher.send_plain(
                    cfg.telegram,
                    f"⚠️ 监控异常：暂时无法获取 @{username} 的推文，将在下次轮询自动重试，"
                    f"恢复后会通知（期间不重复提示）。",
                )
            except ApiError:
                pass
            store.set_alerted(username, True)
        return None
    # 成功：若此前提示过故障，发一次"已恢复"并清旗标
    if store.get_alerted(username):
        try:
            publisher.send_plain(cfg.telegram, f"✅ 监控已恢复：重新开始获取 @{username} 的推文。")
        except ApiError:
            pass
        store.set_alerted(username, False)
    return tweets


def _strip_media_links(tweet, forward_media: bool) -> None:
    """媒体会直接发送时，先乐观剥离正文里的媒体 t.co 短链（发送失败再由页脚补回）。原地改 tweet.text。"""
    if forward_media and (tweet.photos or tweet.videos) and tweet.media_links:
        text = tweet.text
        for link in tweet.media_links:
            text = text.replace(link, "")
        tweet.text = text.rstrip()


def _post_placeholders_one(cfg, store: Store, tweet) -> bool:
    """发原文 + 占位（+媒体置底），入库。成功返回 True；发送失败返回 False（下轮重试）。"""
    _strip_media_links(tweet, cfg.telegram.forward_media)
    try:
        two_part, msg1_id, msg2_id, media_status = publisher.post_placeholders(cfg.telegram, cfg.tz, tweet)
    except ApiError as exc:
        log.error("发送原文失败 tweet=%s（下轮重试）：%s", tweet.id, exc)
        return False
    store.add_posted(tweet, cfg.telegram.chat_id, msg1_id, msg2_id, two_part, media_status)
    log.info("已发原文 @%s tweet=%s 布局=%s 图%d 视频%d 媒体=%s", tweet.username, tweet.id,
             "2条" if two_part else "1条", len(tweet.photos), len(tweet.videos), media_status)
    return True


def _enhance_pending(cfg, store: Store) -> None:
    """填充 pass：按「先翻译、后解读」给每条待填推文替换占位（edit 成功才标 done，避免占位卡住）。"""
    for row in store.pending():
        tid = row["tweet_id"]
        tweet = tweet_from_row(row)
        two_part = bool(row["two_part"])
        translation, t_status = row["translation"], row["trans_status"]

        # 1) 翻译
        if t_status == "pending":
            zh = _try_llm(cfg, translator.translate, tweet.text, "翻译", tid)
            if zh is None:
                if store.bump_trans(tid) >= cfg.llm.max_retries:
                    log.error("翻译重试用尽 tweet=%s，标注「翻译失败」", tid)
                    store.fail_trans(tid)
                    store.fail_expl(tid)  # 翻译没成，解读也不做了
                    try:
                        publisher.fill_translation(cfg.telegram, cfg.tz, two_part=two_part, msg1_id=row["msg1_id"],
                                                   tweet=tweet, translation=None, t_status="failed",
                                                   explanation=None, e_status="failed")
                        if two_part:  # 删掉「解读中…」那条
                            publisher.fill_explanation(cfg.telegram, cfg.tz, two_part=two_part, msg1_id=row["msg1_id"],
                                                       msg2_id=row["msg2_id"], tweet=tweet, translation=None,
                                                       t_status="failed", explanation=None, e_status="failed")
                    except ApiError:
                        pass
                continue  # 本轮翻译未成，不解读
            try:
                publisher.fill_translation(cfg.telegram, cfg.tz, two_part=two_part, msg1_id=row["msg1_id"],
                                           tweet=tweet, translation=zh, t_status="done",
                                           explanation=row["explanation"], e_status=row["expl_status"])
            except ApiError as exc:
                log.warning("填译文失败 tweet=%s（下轮重试）：%s", tid, exc)
                continue  # 不标 done，下轮重来
            store.set_translation(tid, zh)
            translation, t_status = zh, "done"
            log.info("已填译文 tweet=%s", tid)

        # 2) 解读（翻译已成才做）
        if t_status == "done" and row["expl_status"] == "pending":
            ex = _try_llm(cfg, translator.explain, tweet.text, "解读", tid)
            if ex is None:
                if store.bump_expl(tid) >= cfg.llm.max_retries:
                    log.error("解读重试用尽 tweet=%s，去掉解读", tid)
                    store.fail_expl(tid)
                    try:
                        publisher.fill_explanation(cfg.telegram, cfg.tz, two_part=two_part, msg1_id=row["msg1_id"],
                                                   msg2_id=row["msg2_id"], tweet=tweet, translation=translation,
                                                   t_status="done", explanation=None, e_status="failed")
                    except ApiError:
                        pass
                continue
            try:
                publisher.fill_explanation(cfg.telegram, cfg.tz, two_part=two_part, msg1_id=row["msg1_id"],
                                           msg2_id=row["msg2_id"], tweet=tweet, translation=translation,
                                           t_status="done", explanation=ex, e_status="done")
            except ApiError as exc:
                log.warning("填解读失败 tweet=%s（下轮重试）：%s", tid, exc)
                continue
            store.set_explanation(tid, ex)
            log.info("已填解读 tweet=%s（%s）", tid, "有术语" if ex else "无")


def _try_llm(cfg, fn, text: str, what: str, tid: str):
    """调一次 LLM（翻译/解读）；失败返回 None（重试计数与降级由调用方按轮处理）。"""
    try:
        return fn(cfg.llm, text)
    except ApiError as exc:
        log.warning("%s失败 tweet=%s：%s", what, tid, exc)
        return None


def run_forever(cfg, store: Store) -> int:
    stop = {"flag": False}

    def _stop(signum, _frame):
        log.info("收到信号 %s，准备优雅退出…", signum)
        stop["flag"] = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    log.info(
        "启动：监控 %s，每 %d 秒轮询；翻译模型=%s",
        ", ".join("@" + u for u in cfg.twitter.usernames),
        cfg.twitter.poll_interval_sec,
        cfg.llm.model,
    )
    while not stop["flag"]:
        try:
            run_cycle(cfg, store)
        except Exception as exc:  # 兜底：任何意外都不能让常驻循环崩掉
            log.exception("本轮出现未预期错误：%s", exc)
        for _ in range(cfg.twitter.poll_interval_sec):
            if stop["flag"]:
                break
            time.sleep(1)

    store.close()
    log.info("已退出。")
    return 0


# ---------- 辅助命令 ----------

def cmd_chat_id(cfg) -> int:
    if cfg.telegram.bot_token.startswith("PUT-YOUR"):
        log.error("请先在 config.toml 填好 telegram.bot_token，再运行 --chat-id")
        return 2
    chats = publisher.list_chats(cfg.telegram)
    if not chats:
        print("没拿到任何会话。请先把 bot 拉进目标群，在群里随便发一条消息，再运行本命令。")
        return 1
    print("发现以下会话（把目标群的 id 填进 config.toml 的 telegram.chat_id）：")
    for c in chats:
        title = c.get("title") or c.get("username") or c.get("first_name") or ""
        print(f"  id={c.get('id')}\ttype={c.get('type')}\t{title}")
    return 0


def cmd_check(cfg) -> int:
    ok = True

    try:
        me = publisher.get_me(cfg.telegram)
        log.info("Telegram bot：@%s ✅", me.get("username"))
    except ApiError as exc:
        ok = False
        log.error("Telegram getMe 失败（检查 bot_token）：%s", exc)

    try:
        data = http_json(
            cfg.llm.base_url.rstrip("/") + "/models",
            headers={"Authorization": f"Bearer {cfg.llm.api_key}"},
            timeout=15,
        )
        models = [m.get("id") for m in (data.get("data") or [])]
        hit = cfg.llm.model in models
        log.info("cliproxyapi：%d 个模型可用；目标模型 %s %s",
                 len(models), cfg.llm.model, "✅" if hit else "⚠️ 不在列表里")
    except ApiError as exc:
        ok = False
        log.error("cliproxyapi 连接失败（检查 base_url / api_key）：%s", exc)

    for u in cfg.twitter.usernames:
        try:  # 用便宜的 user/info 校验（约 1 条计费），不浪费 20 条
            n = source.fetch_user_status(cfg.twitter, u)
            log.info("推文 API @%s：statusesCount=%s ✅（便宜接口校验，未拉取列表）", u, n)
        except ApiError as exc:
            ok = False
            log.error("推文 API @%s 失败（检查 api_key / 用户名）：%s", u, exc)

    log.info("———— 自检结果：%s ————", "全部通过 ✅" if ok else "有问题，请看上面的 ❌")
    return 0 if ok else 1


def cmd_test(cfg) -> int:
    if cfg.telegram.bot_token.startswith("PUT-YOUR"):
        log.error("请先在 config.toml 填好 telegram.bot_token 再运行 --test")
        return 2
    tg = replace(cfg.telegram, chat_id=cfg.telegram.debug_chat_id)  # 测试消息发【调试会话】
    try:
        mid = publisher.send_plain(tg, "✅ twitter2telegram 自检：调试会话可达。")
        log.info("测试消息已发到调试会话 debug_chat_id=%s（message_id=%s）", cfg.telegram.debug_chat_id, mid)
    except ApiError as exc:
        log.error("发送失败：检查 bot_token，以及该用户是否已对 @bot 发过 /start：%s", exc)
        return 1

    sample = source.Tweet(
        id="selftest", username="demo", author_name="Demo Account",
        text="$NVDA crushed earnings again — datacenter rev +80% YoY on insatiable AI demand. "
             "Hyperscaler capex shows no sign of slowing. NFA 🚀 #AI #semis",
        url="https://x.com", created_at=None,
        photos=["https://picsum.photos/seed/t2t/800/450"],
    )
    try:
        _demo_send(tg, cfg.tz, cfg.llm, sample)
        log.info("自检完成：占位→填译文→填解读（发到调试会话），去看效果。")
    except ApiError as exc:
        log.error("翻译/解读/发送失败：%s", exc)
        return 1
    return 0


def _demo_send(tg, tz, llm, tweet):
    """同步演示一条（占位 → 填译文 → 填解读），供 --test / --send-latest 用。返回 (译文, 解读)。"""
    _strip_media_links(tweet, tg.forward_media)
    two_part, msg1, msg2, _ = publisher.post_placeholders(tg, tz, tweet)
    zh = translator.translate(llm, tweet.text)
    publisher.fill_translation(tg, tz, two_part=two_part, msg1_id=msg1, tweet=tweet,
                               translation=zh, t_status="done", explanation=None, e_status="pending")
    ex = translator.explain(llm, tweet.text)
    publisher.fill_explanation(tg, tz, two_part=two_part, msg1_id=msg1, msg2_id=msg2, tweet=tweet,
                               translation=zh, t_status="done", explanation=ex, e_status="done")
    return zh, ex


def cmd_send_latest(cfg) -> int:
    """用缓存里（即"刚才拉到"）的最新 1 条真实推文走完整三步发到群；无缓存才拉一次。

    不写去重库，纯属手动验证，可反复用而不重复计费（有缓存时完全不调远端 API）。
    """
    if cfg.telegram.bot_token.startswith("PUT-YOUR"):
        log.error("请先在 config.toml 填好 telegram.bot_token")
        return 2
    username = cfg.twitter.usernames[0]
    cache = _cache_path(cfg, username)

    tweets = source.load_cached(cfg.twitter, username, cache)
    if tweets:
        log.info("使用缓存的推文（不调用远端 API，零计费）")
    else:
        log.info("无缓存，拉取一次并写缓存…")
        try:
            tweets = source.fetch_latest(cfg.twitter, username, cache_path=cache)
        except ApiError as exc:
            log.error("抓取失败：%s", exc)
            return 1
    if not tweets:
        log.error("没有可用推文")
        return 1

    tw = tweets[0]
    tg = replace(cfg.telegram, chat_id=cfg.telegram.debug_chat_id)  # --send-latest 是测试，发【调试会话】
    log.info("发送最新 1 条 @%s tweet=%s（%d 图，视频=%s）到调试会话", tw.username, tw.id, len(tw.photos), tw.has_video)
    try:
        zh, ex = _demo_send(tg, cfg.tz, cfg.llm, tw)
        log.info("完成，去调试会话看效果。译文前 60 字：%s", (zh or "")[:60])
    except ApiError as exc:
        log.error("发送失败（多半是调试用户没对 @bot 发过 /start）：%s", exc)
        return 1
    return 0


# ---------- 入口 ----------

def main() -> int:
    ap = argparse.ArgumentParser(description="Twitter→翻译→Telegram 转发器")
    ap.add_argument("--once", action="store_true", help="只跑一轮就退出")
    ap.add_argument("--check", action="store_true", help="检查配置与三方连通性（不发消息）")
    ap.add_argument("--chat-id", action="store_true", help="列出 getUpdates 里的会话 id")
    ap.add_argument("--test", action="store_true", help="端到端自检：发测试消息并演示翻译 edit")
    ap.add_argument("--send-latest", action="store_true",
                    help="用缓存里最新 1 条真实推文走完整三步发到群（有缓存则零计费）")
    ap.add_argument("--config", default=str(CONFIG_PATH), help="配置文件路径")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 辅助命令（--check/--chat-id/--test）此时可能还没填全（比如还没拿到推文 API key），
    # 放宽校验；只有真正常驻运行才强制要求全部必填项。
    helper = args.chat_id or args.check or args.test or args.send_latest
    try:
        cfg = load_config(Path(args.config), strict=not helper)
    except ConfigError as exc:
        log.error("配置错误：\n%s", exc)
        return 2

    if args.chat_id:
        return cmd_chat_id(cfg)
    if args.check:
        return cmd_check(cfg)
    if args.test:
        return cmd_test(cfg)
    if args.send_latest:
        return cmd_send_latest(cfg)

    store = Store(cfg.state_db)
    if args.once:
        try:
            run_cycle(cfg, store)
        finally:
            store.close()
        return 0
    return run_forever(cfg, store)


if __name__ == "__main__":
    sys.exit(main())
