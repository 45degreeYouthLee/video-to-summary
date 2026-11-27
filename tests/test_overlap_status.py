import threading
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import app as web


class OverlapStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patch(web, 'DB_PATH', new=self.root / 'synthetic.db')
        web.init_db()
        web.migrate_db()
        self.config = {
            'paths': {'yt_dlp_path': 'offline', 'temp_dir': str(self.root)},
            'summary': {'api_key': 'synthetic', 'model': 'synthetic-model'},
            'asr': {'enabled': True, 'api_key': 'synthetic'},
            'summary_prompt': {'system_prompt': 'synthetic', 'user_prompt_template': '{transcript}',
                               'max_tokens': 100, 'temperature': 0},
        }
        self.patch(web, 'load_config', return_value=self.config)
        self.extractor = self.patch(web, 'SubtitleExtractor', side_effect=AssertionError('unexpected extractor'))
        self.subtitle = self.patch(web, '_extract_transcript_raw', side_effect=AssertionError('unexpected subtitles'))
        self.asr = self.patch(web, '_run_asr', side_effect=AssertionError('unexpected ASR'))
        self.chat = self.patch(web, 'GenericChatClient')
        self.client = web.app.test_client()
        self.first = threading.Event()
        self.release_first = threading.Event()
        self.b_enter = threading.Event()
        self.release_b = threading.Event()
        self.counts = {'A': 0, 'B': 0}
        self.inputs = []
        self.addCleanup(self.finish_queue)

    def patch(self, target, name, **kwargs):
        p = patch.object(target, name, **kwargs)
        value = p.start()
        self.addCleanup(p.stop)
        return value

    def finish_queue(self):
        self.release_first.set()
        self.release_b.set()
        web.job_queue.join()

    def seed(self, job_id):
        with closing(web.db_conn()) as conn:
            conn.execute("INSERT INTO jobs (id,url,video_id,status,title,source,transcript,error) VALUES (?,?,?,'failed',?,'audio_asr',?,'old error')",
                         (job_id, 'https://video.invalid/BV' + job_id, 'BVStored' + job_id,
                          job_id, ' synthetic ' + job_id + '\n'))
            conn.commit()

    def row(self, job_id):
        return self.client.get('/api/jobs/' + job_id).get_json()

    def assert_cached_only(self):
        self.extractor.assert_not_called()
        self.subtitle.assert_not_called()
        self.asr.assert_not_called()

    def run_overlap(self, fail_first=False, fail_second=False):
        for jid in ('A', 'B'):
            self.seed(jid)

        def summarize(**kwargs):
            jid = kwargs['meta']['title']
            self.counts[jid] += 1
            number = self.counts[jid]
            self.inputs.append((jid, kwargs['transcript']))
            if jid == 'A' and number == 1:
                self.first.set()
                if not self.release_first.wait(10):
                    raise RuntimeError('first barrier timeout')
                if fail_first:
                    raise RuntimeError('first synthetic error')
            if jid == 'B':
                self.b_enter.set()
                if not self.release_b.wait(10):
                    raise RuntimeError('B barrier timeout')
            if jid == 'A' and number == 2 and fail_second:
                raise RuntimeError('second synthetic error')
            return f'{jid}-{number}'

        self.chat.return_value.summarize.side_effect = summarize
        self.assertEqual(self.client.post('/api/jobs/A/retry').status_code, 200)
        self.assertTrue(self.first.wait(10))
        self.assertEqual(self.client.post('/api/jobs/B/retry').status_code, 200)
        self.assertEqual(self.client.post('/api/jobs/A/retry').status_code, 200)
        self.release_first.set()
        self.assertTrue(self.b_enter.wait(10))
        with web.job_queue.mutex:
            self.assertEqual([item['args'][0] for item in web.job_queue.queue], ['A'])
        a, b = self.row('A'), self.row('B')
        self.assertEqual((a['status'], a['error'], a['transcript'], a['video_id']),
                         ('pending', None, ' synthetic A\n', 'BVStoredA'))
        self.assertEqual(a['summary'], None if fail_first else 'A-1')
        self.assertEqual(b['status'], 'summarizing')
        self.release_b.set()
        web.job_queue.join()
        a, b = self.row('A'), self.row('B')
        self.assertEqual((b['status'], b['summary']), ('completed', 'B-1'))
        if fail_second:
            self.assertEqual((a['status'], a['summary'], a['error']), ('failed', 'A-1', 'second synthetic error'))
        else:
            self.assertEqual((a['status'], a['summary'], a['error']), ('completed', 'A-2', None))
        self.assertEqual((a['transcript'], a['video_id'], a['id']), (' synthetic A\n', 'BVStoredA', 'A'))
        self.assertEqual(self.counts, {'A': 2, 'B': 1})
        self.assertEqual(self.inputs, [('A', ' synthetic A\n'), ('B', ' synthetic B\n'), ('A', ' synthetic A\n')])
        self.assert_cached_only()
        with web._queue_state_lock:
            self.assertEqual(web._waiting_jobs, {})

    def test_queued_retry_does_not_expose_early_completion(self):
        self.run_overlap()

    def test_early_failure_then_later_success(self):
        self.run_overlap(fail_first=True)

    def test_later_failure_is_visible(self):
        self.run_overlap(fail_second=True)

    def test_single_retry_finishes_normally(self):
        self.seed('A')
        self.chat.return_value.summarize.return_value = 'only result'
        self.assertEqual(self.client.post('/api/jobs/A/retry').status_code, 200)
        web.job_queue.join()
        row = self.row('A')
        self.assertEqual((row['status'], row['summary'], row['error']), ('completed', 'only result', None))
        self.chat.return_value.summarize.assert_called_once()
        self.assert_cached_only()
        with web._queue_state_lock:
            self.assertEqual(web._waiting_jobs, {})

    def test_all_enqueue_routes_register_and_worker_consumes(self):
        for jid in ('A', 'C', 'D'):
            self.seed(jid)
        audio = self.root / 'BVC-audio.mp3'
        audio.write_bytes(b'synthetic')
        with closing(web.db_conn()) as conn:
            conn.execute('UPDATE jobs SET audio_path=? WHERE id=?', (str(audio), 'C'))
            conn.commit()

        def summarize(**kwargs):
            if kwargs['meta']['title'] == 'A':
                self.first.set()
                if not self.release_first.wait(10):
                    raise RuntimeError('barrier timeout')
            return 'synthetic result'

        self.chat.return_value.summarize.side_effect = summarize
        self.asr.side_effect = None
        self.asr.return_value = ('synthetic ASR', 'audio_asr')
        self.assertEqual(self.client.post('/api/jobs/A/retry').status_code, 200)
        self.assertTrue(self.first.wait(10))
        single = self.client.post('/api/jobs', json={'url': 'https://video.invalid/BVnew'}).get_json()['id']
        batch = self.client.post('/api/jobs/batch', json={'videos': [{'bv': 'BVbatch'}]}).get_json()['jobs'][0]['id']
        self.assertEqual(self.client.post('/api/jobs/C/retry-asr').status_code, 200)
        self.assertEqual(self.client.post('/api/jobs/D/retry-asr').status_code, 200)
        with web._queue_state_lock:
            self.assertEqual(web._waiting_jobs, {single: 1, batch: 1, 'C': 1, 'D': 1})
        with web.job_queue.mutex:
            items = list(web.job_queue.queue)
            self.assertEqual([item['args'][0] for item in items], [single, batch, 'C', 'D'])
            self.assertEqual(items[2]['kwargs'], {'resume_from': 'asr'})
            self.assertEqual(items[3]['kwargs'], {})
        self.release_first.set()
        web.job_queue.join()
        with web._queue_state_lock:
            self.assertEqual(web._waiting_jobs, {})
        self.assertEqual(self.row('A')['status'], 'completed')
        # This test intentionally forbids extraction; the saved-audio path
        # still queries metadata, so C fails at that stub after being claimed.
        self.assertEqual(self.row('C')['status'], 'failed')
        self.assertEqual(self.row('D')['status'], 'completed')
        self.assertEqual(self.row(single)['status'], 'failed')
        self.assertEqual(self.row(batch)['status'], 'failed')

    def test_deleted_job_early_return_does_not_leave_queue_count(self):
        self.seed('A')

        def summarize(**kwargs):
            self.first.set()
            if not self.release_first.wait(10):
                raise RuntimeError('barrier timeout')
            return 'result'

        self.chat.return_value.summarize.side_effect = summarize
        self.assertEqual(self.client.post('/api/jobs/A/retry').status_code, 200)
        self.assertTrue(self.first.wait(10))
        self.assertEqual(self.client.post('/api/jobs/A/retry').status_code, 200)
        self.assertEqual(self.client.delete('/api/jobs/A').status_code, 200)
        self.release_first.set()
        web.job_queue.join()
        self.assertEqual(self.client.get('/api/jobs/A').status_code, 404)
        with web._queue_state_lock:
            self.assertEqual(web._waiting_jobs, {})


if __name__ == '__main__':
    unittest.main()
