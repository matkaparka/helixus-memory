"""SQLite 存储：原始事件、观众卡片、Helixus 说过的话（facts）、每场直播的摘要。

一个连接 + 一把锁，HTTP 线程和整理线程共用。整理时调 LLM 不持锁。
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  session TEXT NOT NULL,
  source TEXT NOT NULL,          -- bridge / page / import
  kind TEXT NOT NULL,            -- danmaku / sc / guard / gift / song / reply
  vkey TEXT,                     -- 观众：'bili:123'；reply 为空
  uname TEXT,
  text TEXT NOT NULL,
  input TEXT                     -- reply 回应的那条消息（页面收到的原文）
);
CREATE INDEX IF NOT EXISTS events_vkey ON events(vkey);
CREATE INDEX IF NOT EXISTS events_session ON events(session, id);

CREATE TABLE IF NOT EXISTS viewers(
  vkey TEXT PRIMARY KEY,
  uname TEXT NOT NULL,           -- 最近用的名字
  names TEXT NOT NULL DEFAULT '[]',  -- 用过的名字
  card TEXT NOT NULL DEFAULT '',
  nickname TEXT NOT NULL DEFAULT '', -- Helixus 给他起的称呼
  first_seen REAL, last_seen REAL,
  prev_seen REAL,                -- 这一场之前最后一次出现
  n_msgs INTEGER NOT NULL DEFAULT 0,
  n_sessions INTEGER NOT NULL DEFAULT 0,
  last_session TEXT,
  optout INTEGER NOT NULL DEFAULT 0,
  card_updated REAL
);
CREATE INDEX IF NOT EXISTS viewers_uname ON viewers(uname);

CREATE TABLE IF NOT EXISTS facts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  text TEXT NOT NULL,
  keywords TEXT NOT NULL DEFAULT '[]',
  created REAL NOT NULL,
  session TEXT,
  pinned INTEGER NOT NULL DEFAULT 0,
  uses INTEGER NOT NULL DEFAULT 0,
  last_used REAL,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY,
  started REAL NOT NULL,
  last REAL NOT NULL,
  summary TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""

FORGET_RE = re.compile(r'^(忘掉我|忘了我|别记住我|不要记住我|删掉我的记忆)[!！。.~～]*$')
REMEMBER_RE = re.compile(r'^(记住我|你可以记住我了)[!！。.~～]*$')


class Store:
    def __init__(self, path: str | Path, session_gap_min: float = 120):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()
        self.gap = session_gap_min * 60

    # ---------------------------------------------------------------- meta

    def meta(self, k: str, default: str | None = None) -> str | None:
        with self.lock:
            r = self.db.execute('SELECT v FROM meta WHERE k=?', (k,)).fetchone()
            return r['v'] if r else default

    def set_meta(self, k: str, v: str):
        with self.lock:
            self.db.execute('INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v', (k, v))
            self.db.commit()

    # ---------------------------------------------------------------- sessions

    def session_for(self, ts: float) -> str:
        """两条事件之间隔了 gap 以上就算新的一场。"""
        with self.lock:
            r = self.db.execute('SELECT id, last FROM sessions ORDER BY started DESC LIMIT 1').fetchone()
            if r and ts - r['last'] <= self.gap:
                if ts > r['last']:
                    self.db.execute('UPDATE sessions SET last=? WHERE id=?', (ts, r['id']))
                return r['id']
            sid = time.strftime('%Y%m%d-%H%M', time.localtime(ts))
            while self.db.execute('SELECT 1 FROM sessions WHERE id=?', (sid,)).fetchone():
                sid += 'b'
            self.db.execute('INSERT INTO sessions(id, started, last) VALUES(?,?,?)', (sid, ts, ts))
            return sid

    def current_session(self) -> sqlite3.Row | None:
        with self.lock:
            r = self.db.execute('SELECT * FROM sessions ORDER BY started DESC LIMIT 1').fetchone()
            if r and time.time() - r['last'] <= self.gap:
                return r
            return None

    def session(self, sid: str) -> sqlite3.Row | None:
        with self.lock:
            return self.db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()

    def set_summary(self, sid: str, summary: str):
        with self.lock:
            self.db.execute('UPDATE sessions SET summary=? WHERE id=?', (summary, sid))
            self.db.commit()

    # ---------------------------------------------------------------- events

    def add_viewer_event(self, platform: str, uid, uname: str, kind: str, text: str,
                         ts: float | None = None, source: str = 'bridge') -> str:
        """记一条观众事件。返回 'logged' / 'optout' / 'forgot' / 'remember'。"""
        ts = ts or time.time()
        uname = (uname or '').strip()
        vkey = f'{platform}:{uid}' if uid else None
        with self.lock:
            if vkey and kind == 'danmaku' and FORGET_RE.match(text.strip()):
                self.forget(vkey, optout=True)
                return 'forgot'
            v = self.db.execute('SELECT * FROM viewers WHERE vkey=?', (vkey,)).fetchone() if vkey else None
            if vkey and kind == 'danmaku' and REMEMBER_RE.match(text.strip()):
                if v and v['optout']:
                    self.db.execute('UPDATE viewers SET optout=0 WHERE vkey=?', (vkey,))
                    self.db.commit()
                    return 'remember'
            if v and v['optout']:
                return 'optout'
            sid = self.session_for(ts)
            self.db.execute(
                'INSERT INTO events(ts, session, source, kind, vkey, uname, text) VALUES(?,?,?,?,?,?,?)',
                (ts, sid, source, kind, vkey, uname, text))
            if vkey:
                if v is None:
                    self.db.execute(
                        'INSERT INTO viewers(vkey, uname, names, first_seen, last_seen, n_msgs, n_sessions, last_session)'
                        ' VALUES(?,?,?,?,?,1,1,?)', (vkey, uname, json.dumps([uname], ensure_ascii=False), ts, ts, sid))
                else:
                    names = json.loads(v['names'])
                    if uname and uname not in names:
                        names.append(uname)
                    new_session = v['last_session'] != sid
                    self.db.execute(
                        'UPDATE viewers SET uname=?, names=?, last_seen=?, n_msgs=n_msgs+1,'
                        ' prev_seen=CASE WHEN ? THEN last_seen ELSE prev_seen END,'
                        ' n_sessions=n_sessions+?, last_session=? WHERE vkey=?',
                        (uname or v['uname'], json.dumps(names[-8:], ensure_ascii=False), max(ts, v['last_seen'] or 0),
                         1 if new_session else 0, 1 if new_session else 0, sid, vkey))
            self.db.commit()
            return 'logged'

    def add_reply(self, text: str, input_text: str = '', ts: float | None = None, source: str = 'page'):
        ts = ts or time.time()
        with self.lock:
            sid = self.session_for(ts)
            self.db.execute(
                'INSERT INTO events(ts, session, source, kind, text, input) VALUES(?,?,?,?,?,?)',
                (ts, sid, source, 'reply', text, input_text))
            self.db.commit()

    def events_after(self, cursor: int, limit: int = 2000) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute('SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?', (cursor, limit)).fetchall()

    def count_after(self, cursor: int) -> int:
        with self.lock:
            return self.db.execute('SELECT COUNT(*) c FROM events WHERE id>?', (cursor,)).fetchone()['c']

    def last_event_ts(self) -> float:
        with self.lock:
            r = self.db.execute('SELECT MAX(ts) t FROM events').fetchone()
            return r['t'] or 0.0

    # ---------------------------------------------------------------- viewers

    def viewer(self, vkey: str) -> sqlite3.Row | None:
        with self.lock:
            return self.db.execute('SELECT * FROM viewers WHERE vkey=?', (vkey,)).fetchone()

    def viewer_by_name(self, name: str) -> sqlite3.Row | None:
        """弹幕文本里只有名字：取最近用这个名字的人。"""
        with self.lock:
            return self.db.execute(
                'SELECT * FROM viewers WHERE uname=? ORDER BY last_seen DESC LIMIT 1', (name,)).fetchone()

    def find_viewer(self, q: str) -> sqlite3.Row | None:
        return self.viewer(q) or self.viewer_by_name(q)

    def update_card(self, vkey: str, card: str | None = None, nickname: str | None = None):
        with self.lock:
            if card is not None:
                self.db.execute('UPDATE viewers SET card=?, card_updated=? WHERE vkey=?', (card, time.time(), vkey))
            if nickname is not None:
                self.db.execute('UPDATE viewers SET nickname=? WHERE vkey=?', (nickname, vkey))
            self.db.commit()

    def forget(self, vkey: str, optout: bool = True):
        """删掉这个人的卡片和原始记录；optout 之后他的话不再记。"""
        with self.lock:
            self.db.execute('DELETE FROM events WHERE vkey=?', (vkey,))
            self.db.execute("UPDATE viewers SET card='', nickname='', optout=? WHERE vkey=?", (1 if optout else 0, vkey))
            self.db.commit()

    def top_viewers(self, n: int = 30) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute('SELECT * FROM viewers ORDER BY n_msgs DESC LIMIT ?', (n,)).fetchall()

    # ---------------------------------------------------------------- facts

    def facts(self, active_only: bool = True) -> list[sqlite3.Row]:
        with self.lock:
            q = 'SELECT * FROM facts' + (' WHERE active=1' if active_only else '') + ' ORDER BY id'
            return self.db.execute(q).fetchall()

    def add_fact(self, text: str, keywords: list[str], session: str | None) -> int:
        with self.lock:
            cur = self.db.execute('INSERT INTO facts(text, keywords, created, session) VALUES(?,?,?,?)',
                                  (text, json.dumps(keywords, ensure_ascii=False), time.time(), session))
            self.db.commit()
            return cur.lastrowid

    def update_fact(self, fid: int, text: str | None = None, keywords: list[str] | None = None,
                    active: bool | None = None, pinned: bool | None = None):
        with self.lock:
            if text is not None:
                self.db.execute('UPDATE facts SET text=? WHERE id=?', (text, fid))
            if keywords is not None:
                self.db.execute('UPDATE facts SET keywords=? WHERE id=?', (json.dumps(keywords, ensure_ascii=False), fid))
            if active is not None:
                self.db.execute('UPDATE facts SET active=? WHERE id=?', (1 if active else 0, fid))
            if pinned is not None:
                self.db.execute('UPDATE facts SET pinned=? WHERE id=?', (1 if pinned else 0, fid))
            self.db.commit()

    def mark_used(self, ids: list[int]):
        if not ids:
            return
        with self.lock:
            now = time.time()
            self.db.executemany('UPDATE facts SET uses=uses+1, last_used=? WHERE id=?', [(now, i) for i in ids])
            self.db.commit()

    def stats(self) -> dict:
        with self.lock:
            one = lambda q: self.db.execute(q).fetchone()[0]
            return {
                'events': one('SELECT COUNT(*) FROM events'),
                'replies': one("SELECT COUNT(*) FROM events WHERE kind='reply'"),
                'viewers': one('SELECT COUNT(*) FROM viewers'),
                'cards': one("SELECT COUNT(*) FROM viewers WHERE card!=''"),
                'facts': one('SELECT COUNT(*) FROM facts WHERE active=1'),
                'sessions': one('SELECT COUNT(*) FROM sessions'),
            }
