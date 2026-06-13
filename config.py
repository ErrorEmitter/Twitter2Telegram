"""读取 config.toml 并校验。"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

# 这些是模板里的占位符，出现即视为「没填」
_PLACEHOLDERS = {
    "PUT-YOUR-TWEET-API-KEY",
    "PUT-YOUR-TELEGRAM-BOT-TOKEN",
    "-1001234567890",
    "",
}


@dataclass
class TwitterCfg:
    api_key: str
    usernames: list[str]
    poll_interval_sec: int = 120
    include_replies: bool = False
    include_retweets: bool = False
    max_per_poll: int = 5
    backfill_on_start: int = 0


@dataclass
class LLMCfg:
    base_url: str
    api_key: str
    model: str
    explain_model: str = ""   # 解读专用模型；空则回退到 model
    timeout_sec: int = 60
    max_retries: int = 3
    web_search: bool = True            # 解读时开启联网检索（Anthropic 原生 web_search 工具）
    web_search_max_uses: int = 5       # 单次解读最多联网搜索次数


@dataclass
class TelegramCfg:
    bot_token: str
    chat_id: str
    debug_chat_id: str = ""    # 调试/故障/测试消息发这里；空则回退到 chat_id
    forward_media: bool = True
    disable_notification: bool = False
    disable_web_page_preview: bool = True


@dataclass
class Config:
    twitter: TwitterCfg
    llm: LLMCfg
    telegram: TelegramCfg
    tz: ZoneInfo
    state_db: Path


class ConfigError(RuntimeError):
    pass


def load_config(path: Path, strict: bool = True) -> Config:
    if not path.exists():
        raise ConfigError(f"找不到配置文件：{path}")
    with path.open("rb") as fh:
        data = tomllib.load(fh)

    tw = data.get("twitter", {})
    llm = data.get("llm", {})
    tg = data.get("telegram", {})
    disp = data.get("display", {})
    store = data.get("storage", {})

    cfg = Config(
        twitter=TwitterCfg(
            api_key=str(tw.get("api_key", "")),
            usernames=[u.lstrip("@") for u in tw.get("usernames", []) if u.strip()],
            poll_interval_sec=int(tw.get("poll_interval_sec", 120)),
            include_replies=bool(tw.get("include_replies", False)),
            include_retweets=bool(tw.get("include_retweets", False)),
            max_per_poll=int(tw.get("max_per_poll", 5)),
            backfill_on_start=int(tw.get("backfill_on_start", 0)),
        ),
        llm=LLMCfg(
            base_url=str(llm.get("base_url", "")),
            api_key=str(llm.get("api_key", "")),
            model=str(llm.get("model", "")),
            explain_model=str(llm.get("explain_model", "")) or str(llm.get("model", "")),
            timeout_sec=int(llm.get("timeout_sec", 60)),
            max_retries=int(llm.get("max_retries", 3)),
            web_search=bool(llm.get("explain_web_search", True)),
            web_search_max_uses=int(llm.get("web_search_max_uses", 5)),
        ),
        telegram=TelegramCfg(
            bot_token=str(tg.get("bot_token", "")),
            chat_id=str(tg.get("chat_id", "")),
            debug_chat_id=str(tg.get("debug_chat_id", "")) or str(tg.get("chat_id", "")),
            forward_media=bool(tg.get("forward_media", True)),
            disable_notification=bool(tg.get("disable_notification", False)),
            disable_web_page_preview=bool(tg.get("disable_web_page_preview", True)),
        ),
        tz=ZoneInfo(str(disp.get("timezone", "Asia/Shanghai"))),
        state_db=path.parent / str(store.get("state_db", "state.db")),
    )
    if strict:
        _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    missing = []
    if cfg.twitter.api_key in _PLACEHOLDERS:
        missing.append("twitter.api_key（第三方推文 API 的 Key）")
    if not cfg.twitter.usernames:
        missing.append("twitter.usernames（至少一个博主）")
    if cfg.telegram.bot_token in _PLACEHOLDERS:
        missing.append("telegram.bot_token（BotFather 的 token）")
    if cfg.telegram.chat_id in _PLACEHOLDERS:
        missing.append("telegram.chat_id（目标群 id，见 README / `--chat-id`）")
    if missing:
        raise ConfigError(
            "config.toml 还有必填项没填好：\n  - " + "\n  - ".join(missing)
        )
