"""记忆服务的命令行工具（直接读写数据库；整理会先看服务开没开，开着就交给服务做）。

  mem.py stats
  mem.py viewers [--top 30]          常来的人和卡片
  mem.py viewer 名字或编号
  mem.py card 名字或编号 "新卡片"     手改卡片（--nick 外号）
  mem.py forget 名字或编号            删掉这个人的卡片和记录，之后不再记他
  mem.py facts                        他说过的话
  mem.py fact-drop ID / fact-pin ID / fact-unpin ID / fact-add "你说过……" --kw 词1,词2
  mem.py summary [场次]               各场摘要
  mem.py context "【弹幕】名字：内容"  看看页面会拿到什么
  mem.py consolidate                  马上整理
  mem.py import-logs E:/aivup/bili_bridge/logs   从桥的旧日志导入弹幕（只导一次）
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import recall  # noqa: E402
from consolidate import Consolidator  # noqa: E402
from memory_service import load_config  # noqa: E402
from store import Store  # noqa: E402

# 桥日志：2026-10-16 17:43:30,756 INFO  bridge   入队弹幕 名字(UID)：内容
LOG_TS = r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \S+\s+\S+\s+'
DANMAKU_RE = re.compile(LOG_TS + r'入队弹幕 (.+)\((\d+)\)：(.*)$')
SC_RE = re.compile(LOG_TS + r'入队 SC ¥(\d+) (.+?)：(.*)$')
GUARD_RE = re.compile(LOG_TS + r'入队上舰 (\S+) (\S+) x(\d+)$')


def service_up(cfg) -> str | None:
    s = cfg['server']
    url = f'http://{s.get("host", "127.0.0.1")}:{s.get("port", 8790)}'
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url + '/health', timeout=2):
            return url
    except Exception:
        return None


def ts_of(s: str) -> float:
    return time.mktime(time.strptime(s, '%Y-%m-%d %H:%M:%S'))


def import_logs(store: Store, folder: str):
    done = set(json.loads(store.meta('imported_logs', '[]')))
    files = sorted(glob.glob(os.path.join(folder, 'bridge_*.log')))
    rows = []
    for f in files:
        name = os.path.basename(f)
        if name in done:
            print(f'  跳过（导过了）{name}')
            continue
        n = 0
        for line in open(f, encoding='utf-8', errors='replace'):
            line = line.rstrip('\n')
            m = DANMAKU_RE.match(line)
            if m:
                rows.append((ts_of(m.group(1)), 'danmaku', m.group(3), m.group(2), m.group(4)))
                n += 1
                continue
            m = SC_RE.match(line)
            if m:
                rows.append((ts_of(m.group(1)), 'sc', None, m.group(3), f'¥{m.group(2)} {m.group(4)}'))
                n += 1
                continue
            m = GUARD_RE.match(line)
            if m:
                rows.append((ts_of(m.group(1)), 'guard', None, m.group(2), f'开通了{m.group(3)}'))
                n += 1
        done.add(name)
        print(f'  {name}: {n} 条')
    rows.sort(key=lambda r: r[0])
    for ts, kind, uid, uname, text in rows:
        if uid is None:  # SC / 上舰的日志里没有 UID：按名字找
            v = store.viewer_by_name(uname)
            uid = v['vkey'].split(':', 1)[1] if v else None
        store.add_viewer_event('bili', uid, uname, kind, text, ts, source='import')
    store.set_meta('imported_logs', json.dumps(sorted(done)))
    print(f'导入 {len(rows)} 条。现在：{store.stats()}')


def show_viewer(v):
    if not v:
        print('没找到')
        return
    d = dict(v)
    for k in ('first_seen', 'last_seen', 'prev_seen', 'card_updated'):
        if d.get(k):
            d[k] = time.strftime('%Y-%m-%d %H:%M', time.localtime(d[k]))
    for k, val in d.items():
        print(f'{k:>13}: {val}')


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--config', default=str(HERE / 'config.toml'))
    ap.add_argument('cmd')
    ap.add_argument('args', nargs='*')
    ap.add_argument('--top', type=int, default=30)
    ap.add_argument('--nick')
    ap.add_argument('--kw', default='')
    a = ap.parse_args()
    cfg = load_config(a.config)
    store = Store(cfg['server']['db'], cfg['session'].get('gap_minutes', 120))

    if a.cmd == 'stats':
        print(store.stats(), 'cursor', store.meta('cursor', '0'))
    elif a.cmd == 'viewers':
        for v in store.top_viewers(a.top):
            flag = ' [不记]' if v['optout'] else ''
            nick = f'（{v["nickname"]}）' if v['nickname'] else ''
            print(f'{v["vkey"]:<22} {v["uname"]}{nick}{flag}  {v["n_msgs"]} 条 / {v["n_sessions"]} 场')
            if v['card']:
                print(f'    {v["card"]}')
    elif a.cmd == 'viewer':
        show_viewer(store.find_viewer(a.args[0]))
    elif a.cmd == 'card':
        v = store.find_viewer(a.args[0])
        if not v:
            sys.exit('没找到')
        store.update_card(v['vkey'], a.args[1] if len(a.args) > 1 else None, a.nick)
        show_viewer(store.viewer(v['vkey']))
    elif a.cmd == 'forget':
        v = store.find_viewer(a.args[0])
        if not v:
            sys.exit('没找到')
        store.forget(v['vkey'])
        print(f'已删掉 {v["uname"]}（{v["vkey"]}）的卡片和记录，之后不再记他（他发「记住我」可以恢复）')
    elif a.cmd == 'facts':
        for f in store.facts(active_only=False):
            flag = '' if f['active'] else ' [已删]'
            pin = ' [置顶]' if f['pinned'] else ''
            print(f'#{f["id"]}{pin}{flag} {f["text"]}  {json.loads(f["keywords"])}  用过 {f["uses"]} 次')
    elif a.cmd == 'fact-drop':
        store.update_fact(int(a.args[0]), active=False)
    elif a.cmd == 'fact-pin':
        store.update_fact(int(a.args[0]), pinned=True)
    elif a.cmd == 'fact-unpin':
        store.update_fact(int(a.args[0]), pinned=False)
    elif a.cmd == 'fact-add':
        cur = store.current_session()
        fid = store.add_fact(a.args[0], [w for w in a.kw.split(',') if w], cur['id'] if cur else None)
        print('#', fid)
    elif a.cmd == 'summary':
        with store.lock:
            q = 'SELECT * FROM sessions' + (' WHERE id=?' if a.args else '') + ' ORDER BY started'
            for s in store.db.execute(q, tuple(a.args[:1])).fetchall():
                print(f'[{s["id"]}] {s["summary"] or "（没有摘要）"}')
    elif a.cmd == 'context':
        text = ' '.join(a.args).replace('\\n', '\n')
        r = recall.build(store, text, cfg['context'].get('max_viewers', 5), cfg['context'].get('max_facts', 4))
        print(r['block'] or '（没有记忆可带）')
    elif a.cmd == 'consolidate':
        url = service_up(cfg)
        if url:
            print('服务开着，交给服务整理…')
            req = urllib.request.Request(url + '/consolidate', data=b'{}', headers={'Content-Type': 'application/json'})
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=3600) as r:
                print(json.loads(r.read()))
        else:
            import logging
            logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s', datefmt='%H:%M:%S')
            print(Consolidator(store, cfg).run(max_chunks=200))
    elif a.cmd == 'import-logs':
        if service_up(cfg):
            sys.exit('先停掉记忆服务再导入（避免两边同时写）')
        import_logs(store, a.args[0] if a.args else 'E:/aivup/bili_bridge/logs')
    else:
        ap.print_help()


if __name__ == '__main__':
    main()
