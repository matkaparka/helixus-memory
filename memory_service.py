"""Helixus 长期记忆服务（只用标准库）。

  POST /event        桥：一条观众事件 {platform, uid, uname, kind, text, ts?}
  POST /reply        页面：他说完的一句 {text, input}
  POST /context      页面：回答前取记忆 {text} → {block, viewers, facts}
  POST /consolidate  马上整理一次（平时后台自己跑）
  GET  /health       计数和整理状态
  GET  /viewer?q=名字或编号   GET /facts

用法：python memory_service.py [--config config.toml]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import recall  # noqa: E402
from consolidate import Consolidator  # noqa: E402
from store import Store  # noqa: E402

log = logging.getLogger('memory')


def load_config(path: str | Path) -> dict:
    cfg = tomllib.loads(Path(path).read_text(encoding='utf-8'))
    db = Path(cfg['server']['db'])
    cfg['server']['db'] = str(db if db.is_absolute() else HERE / db)
    return cfg


def setup_logging():
    (HERE / 'logs').mkdir(exist_ok=True)
    fh = logging.FileHandler(HERE / 'logs' / f'memory_{time.strftime("%Y%m%d")}.log', encoding='utf-8')
    fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)-5s %(name)s %(message)s'))
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter('%(asctime)s %(levelname)-5s %(message)s', '%H:%M:%S'))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    root.addHandler(ch)


def row(r) -> dict | None:
    return dict(r) if r is not None else None


class App:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.store = Store(cfg['server']['db'], cfg['session'].get('gap_minutes', 120))
        self.cons = Consolidator(self.store, cfg)
        self.last_run = 0.0
        self.last_error = ''
        self.run_lock = threading.Lock()

    def consolidate_now(self) -> dict:
        with self.run_lock:
            try:
                r = self.cons.run()
                self.last_error = ''
                if r.get('events'):
                    log.info('整理完成：%s', r)
                return r
            except Exception as e:
                self.last_error = str(e)
                log.error('整理失败（游标没动，下次重来）：%s', e)
                return {'error': str(e)}
            finally:
                self.last_run = time.time()

    def consolidate_loop(self):
        c = self.cfg['consolidate']
        while True:
            time.sleep(30)
            if not c.get('enabled', True):
                continue
            try:
                n = self.cons.pending()
                if not n:
                    continue
                now = time.time()
                quiet = now - self.store.last_event_ts()
                due = now - self.last_run >= c.get('interval_sec', 600) and n >= c.get('min_events', 6)
                flush = quiet >= c.get('idle_flush_sec', 600)  # 下播 / 长时间没人说话：剩下的也整理掉
                if due or flush:
                    self.consolidate_now()
            except Exception:
                log.exception('整理循环出错')

    def context(self, text: str) -> dict:
        c = self.cfg['context']
        return recall.build(self.store, text, c.get('max_viewers', 5), c.get('max_facts', 4))


def make_handler(app: App):
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, code: int, obj):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get('Content-Length') or 0)
            raw = self.rfile.read(n) if n else b'{}'
            return json.loads(raw or b'{}')

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.send_header('Access-Control-Allow-Private-Network', 'true')
            self.end_headers()

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == '/health':
                return self._send(200, {
                    'ok': True, **app.store.stats(), 'pending': app.cons.pending(),
                    'last_consolidate': app.last_run, 'last_error': app.last_error,
                    'session': row(app.store.current_session())})
            if u.path == '/viewer':
                return self._send(200, row(app.store.find_viewer(q.get('q', ''))) or {})
            if u.path == '/facts':
                return self._send(200, [dict(f) for f in app.store.facts()])
            self._send(404, {'error': 'not found'})

        def do_POST(self):
            path = urlparse(self.path).path
            try:
                b = self._body()
            except Exception as e:
                return self._send(400, {'error': f'bad json: {e}'})
            try:
                if path == '/event':
                    r = app.store.add_viewer_event(
                        str(b.get('platform') or 'bili'), b.get('uid'), str(b.get('uname') or ''),
                        str(b.get('kind') or 'danmaku'), str(b.get('text') or ''), b.get('ts'))
                    if r in ('forgot', 'remember'):
                        log.info('观众 %s(%s) %s', b.get('uname'), b.get('uid'), '要求忘掉他' if r == 'forgot' else '恢复记录')
                    return self._send(200, {'ok': True, 'result': r})
                if path == '/reply':
                    text = str(b.get('text') or '').strip()
                    if text:
                        app.store.add_reply(text, str(b.get('input') or ''), b.get('ts'))
                    return self._send(200, {'ok': True})
                if path == '/context':
                    t0 = time.perf_counter()
                    r = app.context(str(b.get('text') or ''))
                    r['ms'] = round((time.perf_counter() - t0) * 1000, 1)
                    return self._send(200, r)
                if path == '/consolidate':
                    return self._send(200, app.consolidate_now())
                self._send(404, {'error': 'not found'})
            except Exception as e:
                log.exception('%s 出错', path)
                self._send(500, {'error': str(e)})
    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', default=str(HERE / 'config.toml'))
    args = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    setup_logging()
    cfg = load_config(args.config)
    app = App(cfg)
    threading.Thread(target=app.consolidate_loop, daemon=True).start()
    s = cfg['server']
    srv = ThreadingHTTPServer((s.get('host', '127.0.0.1'), s.get('port', 8790)), make_handler(app))
    log.info('==== Helixus 记忆服务 http://%s:%d  %s ====', s.get('host', '127.0.0.1'), s.get('port', 8790), app.store.stats())
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
