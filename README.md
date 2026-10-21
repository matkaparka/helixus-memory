# helixus_memory — Helixus 的长期记忆

让他记住观众（按 B站 UID 认人，改名也认得），也记住自己说过的话。只用 Python 标准库。

```
弹幕桥 ──/event（UID、名字、内容）──▶ 记忆服务 ◀──/context（回答前）── AITuberKit 页面
                                       ▲  │              ──/reply（说完后）──▶
                                       │  └─ 后台整理（DeepSeek）：观众卡片 / 他说过的话 / 本场摘要
```

## 每次回答带什么（大小固定，不随直播时长增长）

- 这批弹幕里的人（最多 5 个）：来过几场、上次什么时候、他给这人起的称呼、卡片（≤120 字）
- 他以前说过的相关的话（关键词 + 二字组匹配，最多 4 条，置顶的另算，最多 3 条）
- 本场到现在的摘要（≤200 字）
- 页面另外附最近几轮对话（条数 = 页面设置里的 maxPastMessages），只在人设里没写 `[conversation_history]` 时附

原始记录全留在 `data/memory.db`，不进上下文。

## 跑起来

```
start_memory.bat                 # http://127.0.0.1:8790
```

- 弹幕桥：`bili_bridge/config.toml` 的 `[memory] enabled = true`（默认 false）
- 页面：AITuberKit 分支 `helixus-memory`（`src/features/helixus/memory.ts`，`messageReceiver.tsx` 的 ai_generate 分支）。地址改 `NEXT_PUBLIC_HELIXUS_MEMORY_URL`
- 服务没开：桥不记、页面不带记忆（最近对话照样附），都不影响说话；页面 30 秒内不会再问
- 整理模型：`config.toml` 的 `[llm]`，key 从 `key_file` 按变量名读（默认用 trpg-table 的 DEEPSEEK_API_KEY）

## 整理规则（consolidate.py）

- 直播中每 10 分钟、攒够 6 条新记录整理一次；10 分钟没新记录（下播）把剩下的整理掉。失败不推进游标
- 「他说过的话」和「他给谁起的外号」只从他自己的话（页面记回来的 reply）里取。一段记录里没有他的话，这两样模型给什么都不收——DeepSeek 实测会把观众的话、观众对他的称呼编进去
- 观众说「你上次说过……」不当真；卡片里最多记「他声称……」
- 不记真名、电话、住址、学校公司全名、证件号、病情
- 观众发「忘掉我」：删他的卡片和原始记录，之后不再记；发「记住我」恢复

## 命令行（mem.py）

```
mem.py stats | viewers [--top 30] | viewer 名字 | facts | summary [场次]
mem.py card 名字 "新卡片" [--nick 外号]      手改
mem.py forget 名字                          删人
mem.py fact-add "你说过……" --kw 词1,词2 | fact-drop ID | fact-pin ID | fact-unpin ID
mem.py context "【弹幕】名字：内容"          看页面会拿到什么
mem.py consolidate                          马上整理（服务开着就交给服务）
mem.py import-logs E:/aivup/bili_bridge/logs  从桥的旧日志导入（只导一次，先停服务）
```

## 现状（2026-10-21）

- 已从桥日志导入 9-24 到 10-19 的 3417 条弹幕（382 人、21 场），整理出老观众卡片和各场摘要。历史里没有他自己的话，所以「他说过的话」和外号目前是空的，从下一场开始攒
- 测试：`python -m unittest discover -s tests`（不联网）。端到端在测试端口跑过：桥 Router → 服务 → 页面 → Haiku 5.5 回答用上了卡片、外号、他说过的话和最近对话 → 说完记回 → DeepSeek 整理出新卡片、新的「他说过的话」
- 没在真直播里跑过；他会不会生硬地翻旧账要真播几场看
