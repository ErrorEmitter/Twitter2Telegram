# twitter2telegram

监控指定 X(Twitter) 博主的新推文，用本地 **cliproxyapi** 翻译成中文，转发到 **Telegram 群**。

> 💡 **本项目源于 [Liyao](https://github.com/lindabutterfield1997-cell) 的思路**（idea & concept by Liyao）。

**核心特性：先发原文，再逐个填上译文与解读。** 新推一到先发到群（顺序＝译文、原文、解读；译文与解读位先显示斜体「翻译中…」「解读中…」占位）→ 翻译好替换译文占位 → 再补上「📖 名词解释 + 💬 推文解读」、替换解读占位。内容短一条消息搞定，长则拆两条，图片始终在最下方，**每条推文最多 3 条消息。译文 / 原文 / 解读 三段都是默认折叠、点按展开的折叠块（`<blockquote expandable>`）。**

> 正式监控到的推文、以及**故障提示**都发到群；只有 `--test`/`--send-latest` 等**手动测试**消息发到 `debug_chat_id`（个人会话）。译文/原文/解读三段都是**带 emoji 标签、默认折叠可展开**的折叠块，解读标签后还带一行摘要（折叠时可见）。

```
X 博主发推
   │  每 10 分钟用 advanced_search 查"上次→现在"时间窗（首次仅建基线，不刷历史）
   ▼
① 先发原文到群：顺序【译文位「翻译中…」 / 原文 / 解读位「解读中…」】；有图片则置底（≤3 条消息）
② 翻译好 → 替换译文占位；③ 解读好（📖 名词解释 + 💬 推文解读）→ 替换解读占位（都没有则去掉）
   内容长则拆两条（① 译文+原文 ② 解读）；翻译失败→标注；图片失败→靠🔗原推兜底
```

零第三方依赖，仅用 Python 标准库（`tomllib` / `sqlite3` / `zoneinfo` / `urllib`）。

---

## 一、准备三样东西

### 1. 第三方推文 API 的 Key（推文来源）
本项目通过一个第三方 X(Twitter) 数据 API 获取推文。默认实现对接 `api.twitterapi.io` 的接口——注册后把 API Key 填进配置即可；也可替换为其它兼容服务。按量计费，监控单个博主成本很低。

### 2. Telegram 机器人 Token
在 Telegram 找 [@BotFather](https://t.me/BotFather) → `/newbot` → 按提示起名 → 拿到形如 `123456:ABC-DEF...` 的 token。

### 3. 目标群的 chat_id
1. 把刚建的 bot **拉进目标群**（群设置 → 添加成员 → 搜 bot 用户名）。
2. 在群里**随便发一条消息**（让 bot 能在 getUpdates 里看到这个群）。
3. 填好 `config.toml` 的 `bot_token` 后运行：
   ```bash
   python3 main.py --chat-id
   ```
   会列出形如 `id=-1001234567890  type=supergroup  群名` 的会话，把那个 **id** 填进 `config.toml`。

> 注：bot 默认开启隐私模式，看不到群里别人的普通消息也没关系——它只负责**发**消息；`--chat-id` 用的是 bot 自己能收到的更新（被加群、@、命令等）。若 `--chat-id` 拿不到群，可在群里 @一下你的 bot 或发 `/start@你的bot`。

---

## 二、填配置

编辑 [`config.toml`](config.toml)，至少填这 4 项（cliproxyapi 的地址和 key 已自动填好）：

| 字段 | 说明 |
|---|---|
| `twitter.api_key` | 第三方推文 API 的 Key |
| `twitter.usernames` | 要监控的博主，如 `["elonmusk"]`（不带 @，可多个） |
| `telegram.bot_token` | BotFather 给的 token |
| `telegram.chat_id` | 上一步拿到的群 id（正式推文发这里） |
| `telegram.debug_chat_id` | 调试/故障/测试消息发的个人会话 id（该用户需先对 bot 发 /start）；留空＝同 chat_id |

机密信息在里面，建议 `chmod 600 config.toml`。

---

## 三、自检（强烈建议按顺序跑一遍）

```bash
cd /path/to/twitter2telegram     # 改成你的项目目录

python3 main.py --check      # ① 检查配置、cliproxyapi、Telegram、推文 API 是否都通（不发消息）
python3 main.py --chat-id    # ② 不确定 chat_id 时用它列出来
python3 main.py --test       # ③ 往群里发测试消息，演示「译文 / 原文 / 📖术语解释（+图片置底）」效果
python3 main.py --send-latest # ④ 用缓存里最新 1 条真实推文发到群（有缓存则零计费，反复可用）
python3 main.py --once       # ⑤ 真跑一轮（首次按 backfill_on_start 决定发几条，当前=1）
```

`--check` 全绿后再继续。

---

## 四、部署为常驻服务（systemd）

```bash
# 安装为当前用户的用户级服务（推荐）
mkdir -p ~/.config/systemd/user
# 用模板：复制后按里面注释改 WorkingDirectory / ExecStart / HOME 三处路径
cp twitter2telegram.service.example ~/.config/systemd/user/twitter2telegram.service
$EDITOR ~/.config/systemd/user/twitter2telegram.service   # 改成你的实际路径
systemctl --user daemon-reload
systemctl --user enable --now twitter2telegram
loginctl enable-linger "$USER"      # 让服务在你登出后也持续运行

# 看日志
journalctl --user -u twitter2telegram -f
```

> 也可以装成系统级：把 service 文件放到 `/etc/systemd/system/`，加一行 `User=你的用户名`，再用 `sudo systemctl enable --now twitter2telegram`。

改完 `config.toml` 后重启即可生效：`systemctl --user restart twitter2telegram`。

---

## 五、常用调参（config.toml）

| 字段 | 默认 | 作用 |
|---|---|---|
| `twitter.poll_interval_sec` | 600 | 轮询间隔（当前 10 分钟）。越短越实时，但请求更频繁 |
| `twitter.include_replies` | false | 是否也翻译博主的回复 |
| `twitter.include_retweets` | false | 是否也翻译转推(RT) |
| `twitter.max_per_poll` | 5 | 单轮每博主最多发几条（防异常刷屏；超出的最旧几条只记录不发） |
| `twitter.backfill_on_start` | 1 | 首次启动补发最近 N 条历史推文；**当前=1（首次只发最新 1 条做测试）**，满意后可改 0 |
| `llm.model` | claude-sonnet-4-6 | **翻译**模型（快、稳） |
| `llm.explain_model` | claude-opus-4-8 | **解读**模型（更详尽深入）；留空则与 `model` 相同 |
| `llm.timeout_sec` | 120 | 单次调用超时（opus 出详尽解读较慢，给足） |
| `llm.max_retries` | 3 | 每条推文内翻译/解读的重试次数（无 sleep）；翻译用尽则只发原文并标注「翻译失败」 |
| `telegram.forward_media` | true | 是否把图片+视频作为最后一条消息直接转发（落在最下方）；发送失败则页脚补回媒体链接 |
| `display.timezone` | America/Los_Angeles | 页脚时间时区，自动区分夏令时并注明 PDT/PST |

---

## 六、原理与架构

### 先用大白话讲一遍（非技术读者看这段就够）

把它想成一个**全自动的"翻译搬运工"**：每隔 10 分钟，它去推特上瞄一眼你关注的博主，发现新推文就翻成中文、发到你的 Telegram 群。你什么都不用管。

它靠三个"帮手"干活：

1. **眼睛（抓取）**——推特官方不提供免费的"新推提醒"，所以只能用"定时去看一眼"的办法。它通过一个**第三方推文 API**问一句：*"这个博主从我上次看完到现在，有没有发新的？"* 没有就返回空（几乎不花钱），有就把新的几条拿回来。
2. **大脑（翻译）**——把英文推文交给**你自己部署的 AI**（cliproxyapi，背后是 Claude；任何 OpenAI 兼容接口都行）翻成中文，不额外花钱。
3. **嘴巴（发送）**——通过你创建的 Telegram 机器人(bot)，把消息发进群。

**一条推文的旅程：**

> 博主发了条新推 →（10 分钟内）工具看到它 → **先把原文发进群**，译文和解读的位置先显示「翻译中…」「解读中…」占位 → AI 翻好后**替换译文占位** → 再补上**名词解释**和对**推文的解读**、**替换解读占位**（都没有就去掉）。内容短就一条消息（顺序：译文、原文、解读），太长拆成两条；带图则图片在最下方。每条推文最多 3 条消息。

**几个你可能好奇的点：**

- **"解读"包含两部分**（由更强的 **Opus** 模型生成，力求详尽）：① **📖 名词解释**——把推文里非专业人士可能看不懂的术语、缩写、公司/股票代码($XXX)逐条解释（说清是什么、用途、为何重要）；② **💬 解读**——客观把内容本身（背景、关键概念间的关系、来龙去脉）充分讲透，帮读者完全看明白。**不是总结或复述，不站在推文/作者的立场说话，不评价**（也无免责声明，给内部人员看）。没有可解释/可说明的就不显示。万一翻译出错，也会保留原文并标注，不会卡住。
- **为什么图片在文字下面而不是上面？** Telegram 规定"带图消息"的文字最多 1024 字，而"纯文字消息"能到 4096 字。这位博主常发长文，所以把文字单独发一条（放得下更多），图片跟在下面。
- **会不会重复发、或漏发？** 工具记着"上次看到哪了"，每次只看那之后的，并记下已发过的推文编号去重。万一某次没看成（网络/接口故障），它**不会**把进度往前推，下次自动把这段补上——所以不重不漏。
- **接口挂了会怎样？** 它会在群里提示**一次**「监控异常」，绝不刷屏；等恢复了再说**一次**「已恢复」。
- **要花多少钱？** 翻译走你本地 AI，不花钱；抓取按量计费，但因为只问"有没有新的"、空闲时返回空，单个博主每月通常只有几美分。

> 下面是给开发者看的技术细节，普通使用可以跳过。

### 数据流（每 10 分钟一轮）

```
for 每个博主:
    advanced_search 查 [上次检查-180s, 现在] 时间窗      # 只拿新推；空闲窗口返回 0 条
       └─ 抓取失败 → 群里提示一次(不重试)、不推进游标(下轮窗口变大补回)
    对每条新推(seen() 去重后，按旧→新):
       发原文占位: sendMessage 发 1~2 条(译文位「翻译中…」/解读位「📖解读中…」)；有图→置底；入库
       (发送失败→不入库、不推进游标，下轮重试)
填充 pass(遍历未填好的推文，先翻译后解读):
       ① translate → editMessageText 替换译文占位
       ② explain   → edit 替换解读占位(无术语→删掉解读那条)
       (edit 成功才标 done；各槽失败 bump，到 max_retries 降级：翻译标注/解读去掉)
```

### 模块职责

| 文件 | 职责 |
|---|---|
| `main.py` | 入口：轮询主循环、首次基线、发原文占位 + 填充 pass（先翻译后解读）、故障提示、自检命令（`--check/--chat-id/--test/--send-latest/--once`） |
| `config.py` | 读取 / 校验 `config.toml`（缺必填项给出清晰报错） |
| `source.py` | 抓取层：第三方推文 API 的 `advanced_search`(监控)/`user/info`(校验)/`last_tweets`(--send-latest)，归一化为 `Tweet`；内置 QPS 节流 |
| `translator.py` | 翻译/解读层：cliproxyapi（OpenAI 兼容）`translate()` 译成中文、`explain()` 输出术语/公司解释表 |
| `publisher.py` | 发布层：发原文占位 + `fill_translation`/`fill_explanation` 替换占位、1/2 条布局、图片置底 |
| `store.py` | 状态库：SQLite 去重 + 翻译/解读两槽状态机（`tweets`）、每博主游标 `last_checked` / 故障旗标 `alerted` |
| `util.py` | 标准库 HTTP/JSON 小工具（零第三方依赖） |
| `twitter2telegram.service` | systemd 用户级单元 |

### 运行机制（便于排错）

- **监控方式（时间窗增量）**：用 `advanced_search` 查 `from:博主 since_time:上次 until_time:现在`。每轮只查"上次到现在"的窗口，空闲时返回 0 条最省。回复/转推用 `-filter:replies` 在查询端就排除，不为其付费。窗口左端多回看 180s（`_OVERLAP_SEC`）兜住索引延迟，配合 `seen()` 去重不会重发。
- **去重 / 状态**：`state.db`（SQLite）。`tweets` 表每条推文存消息 id、布局(1/2 条)、译文/解读两槽状态（`pending→done/failed` + 重试计数）；`users` 表存 `last_checked`（时间窗游标）与 `alerted`（是否已提示故障）。发送/抓取失败都不推进游标，下轮窗口变大补回、靠去重避免重发（至少一次送达）。
- **首次基线**：第一次跑某博主时只记录 `last_checked=现在`、不刷历史；只有**之后产生的新推**才转发。想首发补几条历史就调 `backfill_on_start`。
- **限速**：所用推文 API 免费档限 1 请求/5 秒，代码内置全局节流（`source._MIN_REQUEST_GAP`，付费档可调小）。
- **媒体（图片+视频，置底）**：有媒体则作为**最后一条消息**发出（单项 `sendPhoto`/`sendVideo`、多项 `sendMediaGroup`，URL 交 Telegram 抓取）。**直接转发时先剥掉正文里指向媒体的 `t.co` 短链**（从 `extendedEntities.media[].url` 收集）；若媒体**发送失败**（如视频超 Telegram URL 直发的 ~20MB 上限），则页脚把链接**补回来并注明「📎 媒体」**（`media_status` 存库、随渲染）。
- **消息路由**：正式监控推文 + 故障提示 → 群 `chat_id`；`--test`/`--send-latest` 等手动测试 → `debug_chat_id`（个人会话）。
- **折叠块**：译文 / 原文 / 解读三段各为带 emoji 标签（🌐译文 / 🐦原文 / 💡解读）的 `<blockquote expandable>`，默认折叠、点按展开；解读标签后跟一行摘要（`📌 摘要`，折叠时可见）。
- **作者署名**：整体做成指向 X 主页的链接，避免 `@handle` 被 Telegram 当成用户名而可点击跳转到 TG 用户。
- **时间**：页脚时间按 `display.timezone`（默认洛杉矶）显示并注明时区（PDT/PST，自动区分夏令时）。
- **抓取故障提示**：某轮无法获取推文时向群提示**一次**（不重试本轮）；下一轮恢复则提示「已恢复」，若仍故障则**不再重复提示**（由 `users.alerted` 控制）。
- **异步占位、≤3 条消息**：先发原文（译文/解读位占位），填充 pass 先填译文、再填术语解释（`editMessageText` 成功才标 done，避免占位卡住）；内容长则拆 2 条文字 + 图片置底；各槽失败重试 `max_retries` 次，翻译失败标注、解读失败则去掉。

### 设计要点（为什么这么设计）

- **异步占位、顺序填充**：先发原文让你第一时间看到，译文与术语解释作为占位逐个 `editMessageText` 替换；按原文长度预判 1 或 2 条布局，图片作为独立的最后一条保证置底。
- **时间窗增量而非每次拉全量**：`advanced_search` 只查"上次→现在"，空闲返回 0 条，比"每次拉最近 N 条"省一个量级；回复/转推服务端过滤、不付费。
- **至少一次送达**：抓取失败不推进 `last_checked` 游标，下轮窗口自然变大补回，不丢推。
- **图片置底用独立消息**：文字单独一条（4096 上限、不被 caption 的 1024 截断，且先到、不被拉图阻塞），图片紧跟其后、靠发送顺序落在文字下方。
- **作者署名做成 X 链接**：否则纯文本 `@handle` 会被 Telegram 当用户名、可点跳转到无关 TG 用户。
- **调 cliproxyapi 只用单条 user 消息、不带 system**：该代理走 Claude Code 的 OAuth，上游会注入"你是 Claude Code"人格、压过 system 角色导致拒翻；把指令放进 user 消息、并把推文框成"待翻译的数据"最稳，也能抵御推文里的提示注入。换其它 OpenAI 兼容端点可不受此限。
- **零第三方依赖**：仅用标准库（`urllib`/`sqlite3`/`tomllib`/`zoneinfo`），systemd 下最省心、最稳。

---

## 七、费用

- **翻译**：走你本地 cliproxyapi（Claude 账号），不额外花钱。
- **抓取**：推文 API 按返回条数计费。监控用 `advanced_search` 时间窗查询，**空闲窗口返回 0 条**（按最低请求计费），有新推时只返回那几条。回复/转推在查询端就过滤掉、不付费。
  - 估算（单博主、600s=10 分钟轮询）：约 144 次请求/天，绝大多数返回 0 条，成本极低（约每月几美分量级）。
  - 免费档限速 1 请求/5 秒，代码已内置节流；若换付费档可把 `source._MIN_REQUEST_GAP` 调小、轮询间隔调短。

---

## 八、常见问题

| 现象 | 排查 |
|---|---|
| `--check` Telegram 失败 | `bot_token` 错了，或网络不通 |
| `--test` 发送失败 | bot 没在群里 / `chat_id` 不对（群常为负数、超级群以 -100 开头） |
| 只发了原文、没有译文/解读 | 看日志「翻译/解读失败」原因；确认 cliproxyapi 在 8317 正常（`python3 main.py --check`） |
| 抓不到推文 | `twitter.api_key` 或 `usernames` 写错；推文 API 余额不足 |
| 报 429 / QPS 超限 | 推文 API 免费档限 1 请求/5 秒，已内置节流；若仍频繁，调大 `source._MIN_REQUEST_GAP` 或减少博主数 |
| 启动后没动静 | 正常——首次只建基线，要等博主发**新**推才会转发；想验证可临时把 `backfill_on_start` 设为 1 |
| systemd 起不来 | `journalctl --user -u twitter2telegram -e` 看报错；确认 ExecStart 的 python 路径存在 |

> 模块职责见上面「六、原理与架构」的表格。

---

## 开源说明

- **协议**：[MIT](LICENSE)。
- **机密 / 本机信息不入库**：`config.toml`（含 API key / bot token / chat_id）和 `twitter2telegram.service`（含本机绝对路径/用户名）都已在 `.gitignore` 中；公开仓库只含 `*.example` 模板。首次使用：`cp config.example.toml config.toml` 和 `cp twitter2telegram.service.example twitter2telegram.service`，再分别按注释填好。
- `state.db`、`last_*.json` 缓存、`__pycache__` 也已忽略。
- 仅依赖 Python ≥ 3.11 标准库（用到 `tomllib`），无需 `pip install`。

---

## 致谢

- **[Liyao](https://github.com/lindabutterfield1997-cell)** —— 本项目源于 Liyao 的思路（original idea & concept）。
