import json
import shutil
import subprocess
import threading
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from werkzeug.serving import make_server
import app as web

ROOT = Path(__file__).resolve().parents[1]
URL = 'https://video.invalid/BVUrlDerived123'
TEXT = '  第一行 synthetic\n第二行  \n'


class RetryPageTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_patch = patch.object(web, 'DB_PATH', Path(self.temp.name) / 'synthetic.db')
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        web.init_db()
        web.migrate_db()
        self.config = {
            'paths': {'yt_dlp_path': 'offline', 'temp_dir': self.temp.name},
            'summary': {'api_key': 'synthetic-summary', 'model': 'synthetic-model'},
            'asr': {'enabled': True, 'api_key': ''},
            'summary_prompt': {'system_prompt': 'system', 'user_prompt_template': '{transcript}',
                               'max_tokens': 100, 'temperature': 0},
        }
        enqueue_patch = patch.object(web.job_queue, 'put', wraps=web.job_queue.put)
        self.enqueue = enqueue_patch.start()
        self.addCleanup(enqueue_patch.stop)
        for name, opts in (
            ('load_config', {'return_value': self.config}),
            ('SubtitleExtractor', {'side_effect': AssertionError('unexpected acquisition')}),
            ('_extract_transcript_raw', {'side_effect': AssertionError('unexpected subtitle extraction')}),
            ('_run_asr', {'side_effect': AssertionError('unexpected ASR')}),
            ('GenericChatClient', {}),
        ):
            p = patch.object(web, name, **opts)
            obj = p.start()
            self.addCleanup(p.stop)
            setattr(self, name, obj)
        self.GenericChatClient.return_value.summarize.return_value = 'synthetic summary'
        self.server = make_server('127.0.0.1', 0, web.app, threaded=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(web.job_queue.join)
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        with closing(web.db_conn()) as conn:
            conn.execute("INSERT INTO jobs (id,url,video_id,status,source,transcript,title,uploader,duration) VALUES (?, ?, ?, 'failed', ?, ?, ?, ?, ?)",
                         ('synthetic-job', URL, 'BVStoredSentinel999', 'audio_asr', TEXT, 'title', 'author', 42))
            conn.commit()

    def row(self):
        with closing(web.db_conn()) as conn:
            return dict(conn.execute('SELECT * FROM jobs WHERE id=?', ('synthetic-job',)).fetchone())

    def click(self, action):
        node = shutil.which('node')
        self.assertIsNotNone(node, 'Node.js is required to run the page handler regression')
        result = subprocess.run([node, str(ROOT / 'tests' / 'ui_retry_driver.js'),
                                 str(ROOT / 'index.html'), self.base, action,
                                 json.dumps(self.config), json.dumps(self.row())],
                                capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def assert_no_acquisition(self):
        self.SubtitleExtractor.assert_not_called()
        self._extract_transcript_raw.assert_not_called()
        self._run_asr.assert_not_called()

    def test_both_cached_retry_buttons_use_server_stage_validation(self):
        for action in ('retry', 'regenerate'):
            with self.subTest(action=action):
                result = self.click(action)
                self.assertEqual(result['posts'], [{'url': '/api/jobs/synthetic-job/retry', 'method': 'POST'}], result)
                self.assertEqual(result['responses'][0]['status'], 200, result)
                web.job_queue.join()
                row = self.row()
                self.assertEqual((row['id'], row['status'], row['transcript'], row['video_id'], row['source']),
                                 ('synthetic-job', 'completed', TEXT, 'BVStoredSentinel999', 'audio_asr'))
                self.assert_no_acquisition()
        self.assertEqual(self.GenericChatClient.return_value.summarize.call_count, 2)
        self.assertEqual(self.enqueue.call_count, 2)

    def test_retry_missing_transcript_reports_asr_error_without_enqueue(self):
        with closing(web.db_conn()) as conn:
            conn.execute("UPDATE jobs SET transcript=NULL WHERE id='synthetic-job'")
            conn.commit()
        result = self.click('retry')
        self.assertEqual(len(result['responses']), 1, result)
        self.assertEqual(result['responses'][0]['status'], 400, result)
        self.assertIn('ASR', result['toasts'][0])
        self.assertEqual(self.row()['status'], 'failed')
        self.enqueue.assert_not_called()
        self.GenericChatClient.assert_not_called()
        self.assert_no_acquisition()

    def test_retry_missing_summary_key_reports_error_without_enqueue(self):
        self.config['summary']['api_key'] = ''
        result = self.click('retry')
        self.assertEqual(len(result['responses']), 1, result)
        self.assertEqual(result['responses'][0]['status'], 400, result)
        self.assertIn('总结模型', result['toasts'][0])
        self.assertEqual(self.row()['status'], 'failed')
        self.enqueue.assert_not_called()
        self.GenericChatClient.assert_not_called()
        self.assert_no_acquisition()

    def test_new_job_keeps_asr_frontend_validation(self):
        result = self.click('create')
        self.assertEqual(result['posts'], [])
        self.assertIn('ASR', result['toasts'][0])
        self.enqueue.assert_not_called()
        self.GenericChatClient.assert_not_called()
        self.assert_no_acquisition()
