"""不联网的测试：python -m unittest discover -s tests"""
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import recall  # noqa: E402
from consolidate import Consolidator, transcript_line  # noqa: E402
from store import Store  # noqa: E402

CFG = {
    'llm': {'base_url': 'http://127.0.0.1:9', 'model': 'x', 'key_file': '', 'key_var': ''},
    'consolidate': {'chunk_chars': 6000},
    'context': {'card_chars': 100},
}


class FakeLLM:
    def __init__(self, out):
        self.out = out
        self.calls = []

    def chat_json(self, system, user):
        self.calls.append(user)
        return self.out


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'm.db', session_gap_min=120)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_names_in(self):
        text = '\n'.join([
            '【弹幕】张三：你好', '【SC ¥30】李 四：加油', '【上舰】王五 开通了舰长',
            '【点歌】赵六 点了《晴天》', '【冷场】已经3分钟没有人发弹幕了', '【礼物】一位观众 送了 1 个 小花花',
            '【助眠】钱七：晚安'])
        self.assertEqual(recall.names_in(text), ['张三', '李 四', '王五', '赵六', '钱七'])

    def test_sessions_and_counts(self):
        t0 = time.time() - 5 * 86400
        s = self.store
        s.add_viewer_event('bili', 1, '张三', 'danmaku', '第一场', t0)
        s.add_viewer_event('bili', 1, '张三', 'danmaku', '还是第一场', t0 + 600)
        s.add_viewer_event('bili', 1, '张三丰', 'danmaku', '第二场，改名了', t0 + 3 * 86400)
        v = s.viewer('bili:1')
        self.assertEqual(v['n_msgs'], 3)
        self.assertEqual(v['n_sessions'], 2)
        self.assertEqual(v['uname'], '张三丰')
        self.assertAlmostEqual(v['prev_seen'], t0 + 600)
        self.assertEqual(s.stats()['sessions'], 2)

    def test_forget_and_remember(self):
        s = self.store
        s.add_viewer_event('bili', 2, '李四', 'danmaku', '我在考研')
        s.update_card('bili:2', '他在考研')
        self.assertEqual(s.add_viewer_event('bili', 2, '李四', 'danmaku', '忘掉我！'), 'forgot')
        v = s.viewer('bili:2')
        self.assertEqual(v['card'], '')
        self.assertEqual(v['optout'], 1)
        self.assertEqual(s.stats()['events'], 0)
        self.assertEqual(s.add_viewer_event('bili', 2, '李四', 'danmaku', '又来了'), 'optout')
        self.assertEqual(s.add_viewer_event('bili', 2, '李四', 'danmaku', '记住我'), 'remember')
        self.assertEqual(s.add_viewer_event('bili', 2, '李四', 'danmaku', '又来了'), 'logged')

    def test_context_block(self):
        s = self.store
        old = time.time() - 3 * 86400
        s.add_viewer_event('bili', 1, '张三', 'danmaku', '上次', old)
        s.update_card('bili:1', '他在考研，赌过你 BG3 会团灭', nickname='考研仔')
        s.add_viewer_event('bili', 1, '张三', 'danmaku', '史莱姆好可爱')
        s.add_viewer_event('bili', 3, '新人', 'danmaku', '第一次来')
        s.add_fact('你说过你最讨厌史莱姆，见一只烧一只', ['史莱姆'], None)
        s.add_fact('你说过你的胸甲是自己锻的', ['胸甲', '盔甲'], None)
        r = recall.build(s, '【弹幕】张三：史莱姆好可爱\n【弹幕】新人：第一次来')
        b = r['block']
        self.assertIn('张三：以前来过 1 场，上次是3 天前，你叫他「考研仔」。他在考研', b)
        self.assertIn('新人：第一次见', b)
        self.assertIn('最讨厌史莱姆', b)
        self.assertNotIn('胸甲', b)
        self.assertEqual(r['viewers'], ['bili:1', 'bili:3'])

    def test_no_names_no_block(self):
        self.assertEqual(recall.build(self.store, '【冷场】已经3分钟没有人发弹幕了')['block'], '')

    def test_transcript_line(self):
        s = self.store
        s.add_viewer_event('bili', 1, '张三', 'danmaku', '你好')
        s.add_reply('[happy]哼，小东西。[nod]', '【冷场】已经3分钟没有人发弹幕了')
        ev = s.events_after(0)
        self.assertIn('弹幕 张三〔bili:1〕：你好', transcript_line(ev[0]))
        line = transcript_line(ev[1])
        self.assertIn('（他在回应：【冷场】已经3分钟没有人发弹幕了）', line)
        self.assertIn('Helixus：哼，小东西。', line)
        self.assertNotIn('[happy]', line)

    def test_consolidate_applies_and_validates(self):
        s = self.store
        s.add_viewer_event('bili', 1, '张三', 'danmaku', '我在考研，周末来看你')
        s.add_reply('考研？老子最讨厌史莱姆，跟你一样黏糊。以后叫你考研仔。', '【弹幕】张三：我在考研，周末来看你')
        old = s.add_fact('你说过你喜欢史莱姆', ['史莱姆'], None)
        c = Consolidator(s, CFG)
        c.llm = FakeLLM({
            'viewers': [
                {'key': 'bili:1', 'card': '他在考研，周末会来', 'nickname': '考研仔'},
                {'key': 'bili:999', 'card': '编出来的人', 'nickname': ''},
            ],
            'facts_add': [{'text': '你说过你最讨厌史莱姆', 'keywords': ['史莱姆', '黏糊']}],
            'facts_update': [{'id': 12345, 'text': '不存在'}],
            'facts_drop': [old],
            'session_summary': '张三来了，说在考研。',
        })
        r = c.run()
        self.assertEqual(r['cards'], 1)
        self.assertEqual(r['facts_add'], 1)
        self.assertEqual(r['facts_drop'], 1)
        self.assertEqual(r['facts_update'], 0)
        self.assertEqual(s.viewer('bili:1')['nickname'], '考研仔')
        self.assertIsNone(s.viewer('bili:999'))
        self.assertEqual([f['text'] for f in s.facts()], ['你说过你最讨厌史莱姆'])
        self.assertEqual(c.pending(), 0)
        self.assertIn('张三〔bili:1〕', c.llm.calls[0])

    def test_no_facts_without_his_lines(self):
        s = self.store
        s.add_viewer_event('bili', 5, '同名号', 'danmaku', '我是恒星核心锻造的')
        c = Consolidator(s, CFG)
        c.llm = FakeLLM({'viewers': [{'key': 'bili:5', 'card': '顶着同名 ID 起哄', 'nickname': '金龙老师'}],
                         'facts_add': [{'text': '你说过你是恒星核心锻造的', 'keywords': ['恒星']}],
                         'facts_update': [], 'facts_drop': [], 'session_summary': ''})
        r = c.run()
        self.assertEqual(r['facts_add'], 0)
        self.assertEqual(s.facts(), [])
        v = s.viewer('bili:5')
        self.assertEqual(v['card'], '顶着同名 ID 起哄')
        self.assertEqual(v['nickname'], '')  # 观众对他的称呼不能当外号

    def test_clip_sentence_and_ago(self):
        from consolidate import clip_sentence
        self.assertEqual(clip_sentence('第一句很长很长。第二句也很长很长很长', 12), '第一句很长很长。')
        self.assertEqual(clip_sentence('短', 12), '短')
        self.assertTrue(clip_sentence('一二三四五六七八九十十一十二，十三', 12).endswith('…'))
        now = time.mktime((2026, 10, 21, 18, 40, 0, 0, 0, -1))
        self.assertEqual(recall.ago(time.mktime((2026, 10, 19, 21, 0, 0, 0, 0, -1)), now), '前天')
        self.assertEqual(recall.ago(time.mktime((2026, 10, 20, 23, 0, 0, 0, 0, -1)), now), '昨天')
        self.assertEqual(recall.ago(time.mktime((2026, 10, 21, 9, 0, 0, 0, 0, -1)), now), '9 小时前')

    def test_consolidate_failure_keeps_cursor(self):
        s = self.store
        s.add_viewer_event('bili', 1, '张三', 'danmaku', '你好')
        c = Consolidator(s, CFG)

        class Boom:
            def chat_json(self, *a):
                raise RuntimeError('down')
        c.llm = Boom()
        with self.assertRaises(RuntimeError):
            c.run()
        self.assertEqual(c.pending(), 1)


if __name__ == '__main__':
    unittest.main()
