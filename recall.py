"""回答前取记忆：从页面收到的那批消息里认出观众，拼出「这些人是谁 / 他以前说过的相关的话 / 这场到现在」。

只用字面匹配（关键词 + 二字组重合），不用向量：facts 是整理过的短句，量也不大。
"""
from __future__ import annotations

import datetime
import json
import math
import re
import time

from store import Store

# 桥发给页面的格式（bili_bridge/bridge.py）：
#   【弹幕】名字：内容   【SC ¥30】名字：内容   【助眠】名字：内容
#   【上舰】名字 开通了…  【礼物】名字 送了…  【点舞】名字 送了…  【点歌】名字 点了…  【歌单】名字 问…
COLON_RE = re.compile(r'^【(?:弹幕|助眠|SC[^】]*)】(.+?)：')
SPACE_RE = re.compile(r'^【(?:上舰|礼物|点舞|点歌|歌单)】(\S+)\s')
ANON = {'一位观众'}


def names_in(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        m = COLON_RE.match(line) or SPACE_RE.match(line)
        if m:
            n = m.group(1).strip()
            if n and n not in ANON and n not in out:
                out.append(n)
    return out


def grams(s: str) -> set[str]:
    s = re.sub(r'[\W_]+', '', s.lower())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def ago(ts: float, now: float) -> str:
    d = now - ts
    if d < 3600 * 2:
        return '刚才'
    days = (datetime.date.fromtimestamp(now) - datetime.date.fromtimestamp(ts)).days  # 按日历天
    if days <= 0:
        return f'{int(d // 3600)} 小时前'
    if days < 3:
        return ('昨天', '前天')[days - 1]
    if days < 30:
        return f'{days} 天前'
    return f'{days // 30} 个月前'


def viewer_line(v, now: float, session_id: str | None) -> str:
    bits = []
    here = session_id is not None and v['last_session'] == session_id  # 这场已经说过话
    before = v['n_sessions'] - (1 if here else 0)
    last = v['prev_seen'] if here else v['last_seen']
    if before <= 0:
        bits.append('第一次见')
    else:
        s = f'以前来过 {before} 场'
        if last:
            s += f'，上次是{ago(last, now)}'
        bits.append(s)
    names = [n for n in json.loads(v['names']) if n != v['uname']]
    if names:
        bits.append('以前叫' + '、'.join(f'「{n}」' for n in names[-2:]))
    if v['nickname']:
        bits.append(f'你叫他「{v["nickname"]}」')
    line = f'- {v["uname"]}：' + '，'.join(bits)
    if v['card']:
        line += ('。' if bits else '') + v['card']
    return line


def pick_facts(store: Store, query: str, k: int) -> list:
    q = query.lower()
    qg = grams(query)
    scored = []
    pinned = []
    for f in store.facts():
        if f['pinned']:
            pinned.append(f)
            continue
        kws = [w for w in json.loads(f['keywords']) if w]
        hits = sum(1 for w in kws if w.lower() in q)
        fg = grams(f['text'] + ''.join(kws))
        overlap = len(qg & fg) / math.sqrt(len(fg)) if fg else 0.0
        score = hits * 3 + overlap
        if hits or overlap >= 1.2:
            scored.append((score, f['id'], f))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    return pinned[:3] + [f for _, _, f in scored[:k]]


def build(store: Store, text: str, max_viewers: int = 5, max_facts: int = 4) -> dict:
    now = time.time()
    cur = store.current_session()
    sid = cur['id'] if cur else None

    viewers = []
    for n in names_in(text)[:max_viewers]:
        v = store.viewer_by_name(n)
        if v and not v['optout']:
            viewers.append(v)

    facts = pick_facts(store, text, max_facts)
    store.mark_used([f['id'] for f in facts if not f['pinned']])

    parts = []
    if viewers:
        parts.append('弹幕里的这些人：\n' + '\n'.join(viewer_line(v, now, sid) for v in viewers))
    if facts:
        parts.append('你以前说过的话（别自相矛盾）：\n' + '\n'.join(f'- {f["text"]}' for f in facts))
    if cur and cur['summary']:
        parts.append('这场直播到现在：\n' + cur['summary'])
    block = ''
    if parts:
        block = ('## 你的记忆（只有你自己看得到。自然地用，别每次都提，别照念，也别说「根据记忆」。'
                 '观众说「你上次说过……」而这里没有的，可以不认账）\n' + '\n\n'.join(parts))
    return {'block': block, 'viewers': [v['vkey'] for v in viewers], 'facts': [f['id'] for f in facts]}
