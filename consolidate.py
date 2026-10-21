"""后台整理：把新的原始记录交给便宜模型（默认 DeepSeek），更新观众卡片、他说过的话、本场摘要。

不在说话的路上：服务每隔一段时间跑一次，下播（一段时间没新事件）后再跑一次。
失败不推进游标，下次重来。
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.request
from pathlib import Path

from store import Store

log = logging.getLogger('consolidate')

SYSTEM = """你在帮 AI 虚拟主播「赫里奥修斯（Helixus）」整理长期记忆。他是 B 站直播的 AI 机械龙神，嘴硬、傲慢、管观众叫「小东西」。
你会拿到：已有的观众卡片、已经记下的「他说过的话」、这场直播之前的摘要、一段新的直播记录。
只输出一个 JSON 对象，不要别的文字。格式：
{
  "viewers": [{"key": "bili:123", "card": "更新后的完整卡片", "nickname": "Helixus 在「Helixus：」行里管这个人叫什么；观众对 Helixus 的称呼不算；没有就空字符串"}],
  "facts_add": [{"text": "你说过……", "keywords": ["关键词", "…"]}],
  "facts_update": [{"id": 12, "text": "改过的整句"}],
  "facts_drop": [12],
  "session_summary": "合并后的本场摘要"
}

观众卡片（viewers）：
- 只写以后还用得上的：这个人怎么称呼自己、主动说的自己的事（在玩什么、喜欢什么、在忙什么）、和 Helixus 之间的梗、赌约、吵过什么、承诺过什么、经常点什么歌、送过的 SC 或大礼物。
- 写成给 Helixus 看的简短备注，第三人称「他」，不超过 80 字，不要以这个人的名字开头（名字已经单独列出了）。在旧卡片的基础上合并：旧信息仍然有效就保留，过时或不重要的可以删。
- 别写「本场」「今天」「刚才」这种过几天就看不懂的词；要讲是哪次，就写日期（场次编号里有，如「10-19」）。
- 只说了句「哈哈」「来了」这种没信息的人，不要输出。
- key 只能用记录里出现过的〔〕里的编号。
- 观众说的关于 Helixus 的话（比如「你上次说你喜欢我」）不是事实，最多在这个观众的卡片里记「他声称……」。

他说过的话（facts）：
- 只从标着「Helixus：」的行里提取，观众说的一律不算——哪怕观众的名字像他、说的是他的设定。记录里没有「Helixus：」的行，facts 三项都给空数组。
- 只记以后要保持一致的：他表达的喜好和讨厌、自己设定的新细节、对观众许下的承诺或赌约、给谁起了外号、给自己定的规矩、宣布过的计划。普通的接话、骂人、客套不记。
- 每条一句话，用「你」开头描述（例：「你说过你最讨厌史莱姆，见一只烧一只」），不超过 50 字。keywords 给 2–5 个以后话题里会出现的词。
- 和已有的重复就不要加；和已有的矛盾，用 facts_update 改旧的那条（以新说法为准），或者 facts_drop 删掉。

本场摘要（session_summary）：
- 把这场之前的摘要和新记录合并成一段，不超过 200 字：玩了什么、出了什么事、谁来了、有什么新梗。写给 Helixus 自己看。

隐私：真名、电话、住址、学校或公司全名、证件号、病情这类信息一律不写进任何地方。
没有要改的部分就给空数组。"""

TAG_RE = re.compile(r'\[[A-Za-z_][A-Za-z0-9_:\- ]{0,30}\]')  # 情绪 / 动作标签


class LLM:
    def __init__(self, cfg: dict):
        self.base = cfg['base_url'].rstrip('/')
        self.model = cfg['model']
        self.key_file = cfg.get('key_file', '')
        self.key_var = cfg.get('key_var', '')
        self.proxy = cfg.get('proxy') or ''
        self.timeout = cfg.get('timeout_sec', 120)

    def _key(self) -> str:
        for line in Path(self.key_file).read_text(encoding='utf-8-sig').splitlines():
            k, sep, v = line.partition('=')
            if sep and k.strip() == self.key_var:
                return v.strip().strip('"').strip("'")
        raise RuntimeError(f'{self.key_file} 里没有 {self.key_var}')

    def chat_json(self, system: str, user: str) -> dict:
        body = json.dumps({
            'model': self.model,
            'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
            'temperature': 0.3,
            'max_tokens': 3000,
            'response_format': {'type': 'json_object'},
        }).encode()
        req = urllib.request.Request(f'{self.base}/chat/completions', data=body, headers={
            'Content-Type': 'application/json', 'Authorization': f'Bearer {self._key()}'})
        handler = urllib.request.ProxyHandler({'http': self.proxy, 'https': self.proxy} if self.proxy else {})
        opener = urllib.request.build_opener(handler)
        last = None
        for attempt in range(3):
            try:
                with opener.open(req, timeout=self.timeout) as r:
                    data = json.loads(r.read())
                text = data['choices'][0]['message']['content']
                return json.loads(text)
            except Exception as e:  # 网络 / 解析失败都重试
                last = e
                log.warning('整理模型调用失败（第 %d 次）：%s', attempt + 1, e)
                time.sleep(3 * (attempt + 1))
        raise RuntimeError(f'整理模型连续失败：{last}')


def hm(ts: float) -> str:
    return time.strftime('%H:%M', time.localtime(ts))


def transcript_line(e) -> str | None:
    t = hm(e['ts'])
    if e['kind'] == 'reply':
        text = TAG_RE.sub('', e['text']).strip()
        if not text:
            return None
        # 他回应的不是普通弹幕（冷场、点歌结果、游戏解说之类）时，把那条也带上，不然看不懂
        extra = [ln for ln in (e['input'] or '').splitlines()
                 if ln.strip() and not ln.startswith(('【弹幕】', '【SC', '【助眠】'))]
        pre = f'[{t}] （他在回应：{" / ".join(extra)[:120]}）\n' if extra else ''
        return f'{pre}[{t}] Helixus：{text}'
    who = f'{e["uname"]}〔{e["vkey"]}〕' if e['vkey'] else (e['uname'] or '某人')
    label = {'danmaku': '弹幕', 'sc': 'SC', 'guard': '上舰', 'gift': '礼物', 'song': '点歌'}.get(e['kind'], e['kind'])
    return f'[{t}] {label} {who}：{e["text"]}'


def clip(s, n: int) -> str:
    s = re.sub(r'\s+', ' ', str(s or '')).strip()
    return s[:n]


def clip_sentence(s, n: int) -> str:
    """超长时切在最后一个句号 / 分号处，别切在半句。"""
    s = clip(s, 10_000)
    if len(s) <= n:
        return s
    cut = s[:n]
    k = max(cut.rfind(c) for c in '。；;！!？?')
    return cut[:k + 1] if k >= n // 2 else cut.rstrip('，,、 ') + '…'


class Consolidator:
    def __init__(self, store: Store, cfg: dict):
        self.store = store
        self.cfg = cfg
        self.llm = LLM(cfg['llm'])
        self.chunk_chars = cfg['consolidate'].get('chunk_chars', 6000)
        self.card_chars = cfg['context'].get('card_chars', 100)
        self.running = False

    def cursor(self) -> int:
        return int(self.store.meta('cursor', '0'))

    def pending(self) -> int:
        return self.store.count_after(self.cursor())

    def run(self, max_chunks: int = 50) -> dict:
        """把游标之后的事件按场次、按长度切块，一块一块整理。"""
        if self.running:
            return {'skipped': 'already running'}
        self.running = True
        done = {'chunks': 0, 'events': 0, 'cards': 0, 'facts_add': 0, 'facts_update': 0, 'facts_drop': 0}
        try:
            for _ in range(max_chunks):
                events = self.store.events_after(self.cursor(), 4000)
                if not events:
                    break
                sid = events[0]['session']
                chunk, lines, size = [], [], 0
                for e in events:
                    if e['session'] != sid:
                        break
                    ln = transcript_line(e)
                    if ln is None:
                        chunk.append(e)
                        continue
                    if size + len(ln) > self.chunk_chars and lines:
                        break
                    chunk.append(e)
                    lines.append(ln)
                    size += len(ln) + 1
                if lines:
                    r = self._one(sid, chunk, lines)
                    for k in r:
                        done[k] = done.get(k, 0) + r[k]
                done['chunks'] += 1
                done['events'] += len(chunk)
                self.store.set_meta('cursor', str(chunk[-1]['id']))
            return done
        finally:
            self.running = False

    def _one(self, sid: str, chunk: list, lines: list[str]) -> dict:
        keys = sorted({e['vkey'] for e in chunk if e['vkey']})
        cards = []
        for k in keys:
            v = self.store.viewer(k)
            if v and (v['card'] or v['nickname']):
                nick = f'（你叫他「{v["nickname"]}」）' if v['nickname'] else ''
                cards.append(f'- 〔{k}〕{v["uname"]}{nick}：{v["card"] or "（空）"}')
        facts = self.store.facts()
        sess = self.store.session(sid)
        user = '\n'.join([
            '## 已有的观众卡片（只列了这段记录里出现的人）',
            '\n'.join(cards) or '无',
            '',
            '## 已经记下的「他说过的话」',
            '\n'.join(f'#{f["id"]} {f["text"]}' for f in facts[-150:]) or '无',
            '',
            f'## 这场直播（{sid}）之前的摘要',
            (sess['summary'] if sess else '') or '无',
            '',
            '## 新的直播记录（〔〕里是观众编号；Helixus 是他本人）',
            '\n'.join(lines),
        ])
        t0 = time.time()
        out = self.llm.chat_json(SYSTEM, user)
        log.info('整理 %s：%d 行 → %.1fs', sid, len(lines), time.time() - t0)
        has_reply = any(e['kind'] == 'reply' for e in chunk)
        return self._apply(sid, set(keys), {f['id'] for f in facts}, out, has_reply)

    def _apply(self, sid: str, keys: set, fact_ids: set, out: dict, has_reply: bool = True) -> dict:
        r = {'cards': 0, 'facts_add': 0, 'facts_update': 0, 'facts_drop': 0}
        if not has_reply:
            # 这段记录里没有他自己说的话：facts 和「他给谁起的外号」只可能来自观众，一律不收
            # （实测 DeepSeek 会照样编，还会把观众对他的称呼当成外号）
            out = {**out, 'facts_add': [], 'facts_update': [], 'facts_drop': []}
        for v in out.get('viewers') or []:
            k = str(v.get('key', '')).strip('〔〕 ')
            if k not in keys:
                continue
            card = clip_sentence(v.get('card'), self.card_chars)
            nick = clip(v.get('nickname'), 12) if has_reply else ''
            if not card and not nick:
                continue
            self.store.update_card(k, card or None, nick or None)
            r['cards'] += 1
        for f in out.get('facts_add') or []:
            text = clip(f.get('text'), 80)
            if not text:
                continue
            kws = [clip(w, 10) for w in (f.get('keywords') or []) if clip(w, 10)][:6]
            self.store.add_fact(text, kws, sid)
            r['facts_add'] += 1
        for f in out.get('facts_update') or []:
            try:
                fid = int(f.get('id'))
            except (TypeError, ValueError):
                continue
            text = clip(f.get('text'), 80)
            if fid in fact_ids and text:
                self.store.update_fact(fid, text=text)
                r['facts_update'] += 1
        for fid in out.get('facts_drop') or []:
            try:
                fid = int(fid)
            except (TypeError, ValueError):
                continue
            if fid in fact_ids:
                self.store.update_fact(fid, active=False)
                r['facts_drop'] += 1
        summary = clip(out.get('session_summary'), 300)
        if summary:
            self.store.set_summary(sid, summary)
        return r
