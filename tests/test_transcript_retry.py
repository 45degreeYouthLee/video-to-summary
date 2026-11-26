import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import app as web


VIDEO_ID = "BVOffline01"
URL = f"https://video.invalid/{VIDEO_ID}"
TEXT = "这是一段用于离线回归的合成字幕。"
INFO = {
    "title": "合成标题",
    "uploader": "合成作者",
    "duration": 42,
    "thumbnail": "https://video.invalid/cover",
    "description": "合成简介",
}
META = {key: INFO[key] for key in ("title", "uploader", "duration")}


class TranscriptRetryTests(unittest.TestCase):
    def stub(self, target, name, *args, **kwargs):
        patcher = patch.object(target, name, *args, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.stub(web, "DB_PATH", self.root / "jobs.db")
        web.init_db()
        web.migrate_db()
        self.config = {
            "paths": {"yt_dlp_path": "offline", "temp_dir": str(self.root)},
            "summary": {"api_key": "offline", "model": "test-summary"},
            "asr": {"enabled": True, "api_key": "offline"},
            "summary_prompt": {
                "system_prompt": "测试",
                "user_prompt_template": "{title} {uploader} {duration} {transcript}",
                "max_tokens": 100, "temperature": 0,
            },
        }
        self.stub(web, "load_config", return_value=self.config)
        srt = self.root / "captions.srt"
        srt.write_text(f"1\n00:00:00,000 --> 00:00:02,000\n{TEXT}\n", encoding="utf-8")
        extractor = web.SubtitleExtractor
        self.probe = self.stub(extractor, "_check_yt_dlp")
        self.info = self.stub(extractor, "get_video_info", return_value=INFO)
        self.subtitles = self.stub(extractor, "list_subtitles", return_value=[("zh-CN", "srt")])
        self.download = self.stub(extractor, "download_subtitle", return_value=srt)
        self.audio = self.stub(extractor, "extract_audio", return_value=self.root / "synthetic.mp3")
        self.duration = self.stub(web, "_get_audio_duration", return_value=INFO["duration"])
        self.asr = self.stub(web, "_run_asr", return_value=(TEXT, "audio_asr"))
        self.acquisition = (self.probe, self.info, self.subtitles, self.download,
                            self.audio, self.duration, self.asr)
        self.chat = self.stub(web, "GenericChatClient", autospec=True)
        self.summarize = self.chat.return_value.summarize
        self.summarize.return_value = "合成总结"
        self.client = web.app.test_client()
        self.addCleanup(web.job_queue.join)

    def seed_job(self, job_id, transcript):
        with closing(web.db_conn()) as conn:
            conn.execute(
                "INSERT INTO jobs (id, url, status, transcript, error) VALUES (?, ?, 'failed', ?, ?)",
                (job_id, URL, transcript, "上次处理失败"),
            )
            conn.commit()

    def post_and_finish(self, endpoint, **kwargs):
        response = self.client.post(endpoint, **kwargs)
        self.assertEqual(response.status_code, 200, response.get_json())
        queued = response.get_json()
        self.assertEqual(queued["status"], "pending")
        web.job_queue.join()
        response = self.client.get(f"/api/jobs/{queued['id']}")
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def assert_no_acquisition(self):
        for dependency in self.acquisition:
            dependency.assert_not_called()

    def assert_summary_input(self, meta):
        self.summarize.assert_called_once()
        args = self.summarize.call_args.kwargs
        self.assertEqual(args["transcript"], TEXT)
        self.assertEqual(args["meta"], meta)

    def test_first_job_extracts_cc_subtitles_and_saves_video_info(self):
        job = self.post_and_finish("/api/jobs", json={"url": URL})
        self.assertEqual(job["status"], "completed", job["error"])
        self.info.assert_called_once_with(URL)
        self.subtitles.assert_called_once_with(URL)
        self.download.assert_called_once_with(URL, lang="zh-CN", ext="srt")
        self.audio.assert_not_called()
        self.asr.assert_not_called()
        self.assert_summary_input(META)
        self.assertEqual({key: job[key] for key in INFO}, INFO)
        self.assertEqual((job["video_id"], job["source"], job["transcript"]),
                         (VIDEO_ID, "subtitle:zh-CN", TEXT))

    def test_retry_after_summary_failure_uses_saved_asr_when_source_is_gone(self):
        self.subtitles.return_value = []
        self.summarize.side_effect = RuntimeError("合成总结失败")
        before = self.post_and_finish("/api/jobs", json={"url": URL})
        self.assertEqual((before["status"], before["error"]), ("failed", "合成总结失败"))
        self.asr.assert_called_once()
        self.assert_summary_input(META)
        self.assertEqual((before["source"], before["transcript"]), ("audio_asr", TEXT))
        self.assertEqual(before["audio_path"], str(self.audio.return_value))

        for dependency in self.acquisition:
            dependency.reset_mock()
        self.info.side_effect = OSError("合成视频来源不可用")
        self.summarize.reset_mock()
        self.summarize.side_effect = None
        after = self.post_and_finish(f"/api/jobs/{before['id']}/retry")
        self.assertEqual(after["status"], "completed", after["error"])
        self.assert_no_acquisition()
        self.assert_summary_input(META)
        self.assertEqual((after["summary"], after["model"], after["error"]),
                         ("合成总结", "test-summary", None))
        for key in (*INFO, "id", "url", "video_id", "source", "transcript", "audio_path", "created_at"):
            self.assertEqual(after[key], before[key], key)

    def test_retry_without_usable_transcript_extracts_again(self):
        self.seed_job("empty", " \t\n")
        job = self.post_and_finish("/api/jobs/empty/retry")
        self.assertEqual(job["status"], "completed", job["error"])
        self.info.assert_called_once_with(URL)
        self.download.assert_called_once_with(URL, lang="zh-CN", ext="srt")
        self.asr.assert_not_called()
        self.assert_summary_input(META)
        self.assertEqual((job["source"], job["transcript"]), ("subtitle:zh-CN", TEXT))

    def test_summary_only_passes_the_original_transcript_verbatim(self):
        original = f"  {TEXT}\n"
        self.seed_job("summary", original)
        web.process_job("summary", URL, resume_from="summary")
        job = self.client.get("/api/jobs/summary").get_json()
        self.assertEqual(job["status"], "completed", job["error"])
        self.assertEqual(job["transcript"], original)
        self.summarize.assert_called_once()
        self.assertEqual(self.summarize.call_args.kwargs["transcript"], original)
        self.assert_no_acquisition()

    def test_summary_only_without_transcript_fails_before_acquisition(self):
        for job_id, text in (("null", None), ("empty", ""), ("blank", "\t \n")):
            with self.subTest(job_id=job_id):
                self.seed_job(job_id, text)
                web.process_job(job_id, URL, resume_from="summary")
                job = self.client.get(f"/api/jobs/{job_id}").get_json()
                self.assertEqual(job["status"], "failed")
                self.assertEqual(job["error"], "没有可用的字幕，无法重新生成总结")
                self.assertEqual(job["transcript"], text)
                self.assert_no_acquisition()
                self.chat.assert_not_called()

    def test_migrated_rows_use_optional_fields_and_only_fill_missing_video_ids(self):
        self.stub(web, "DB_PATH", self.root / "legacy.db")
        old_ids = (("null", None), ("empty", ""), ("known", "BVStoredID"))
        with closing(web.db_conn()) as conn:
            conn.execute("""CREATE TABLE jobs (
                id TEXT PRIMARY KEY, url TEXT NOT NULL, video_id TEXT, status TEXT,
                source TEXT, transcript TEXT, summary TEXT, model TEXT,
                created_at TEXT, updated_at TEXT, error TEXT)""")
            conn.executemany(
                "INSERT INTO jobs (id, url, video_id, status, transcript) VALUES (?, ?, ?, 'failed', ?)",
                [(job_id, URL, old_id, TEXT) for job_id, old_id in old_ids],
            )
            conn.commit()
        web.init_db()
        web.migrate_db()
        for job_id, old_id in old_ids:
            with self.subTest(job_id=job_id):
                self.summarize.reset_mock()
                job = self.post_and_finish(f"/api/jobs/{job_id}/retry")
                self.assertEqual(job["status"], "completed", job["error"])
                self.assertEqual((job["video_id"], job["source"]), (old_id or VIDEO_ID, "cached"))
                self.assertEqual(job["transcript"], TEXT)
                self.assertTrue(all(job[field] is None for field in (*INFO, "audio_path")))
                self.assert_summary_input({"title": "", "uploader": "", "duration": None})
                self.assert_no_acquisition()

    def test_retry_checks_credentials_for_the_stage_it_needs(self):
        self.config["asr"]["api_key"] = ""
        self.seed_job("saved", TEXT)
        self.seed_job("missing", None)
        job = self.post_and_finish("/api/jobs/saved/retry")
        self.assertEqual(job["status"], "completed", job["error"])
        self.assertEqual(self.summarize.call_count, 1)

        missing = self.client.post("/api/jobs/missing/retry")
        self.assertEqual(missing.status_code, 400)
        self.assertIn("ASR", missing.get_json()["error"])
        self.config["summary"]["api_key"] = ""
        saved = self.client.post("/api/jobs/saved/retry")
        self.assertEqual(saved.status_code, 400)
        self.assertIn("总结模型", saved.get_json()["error"])
        web.job_queue.join()
        self.assertEqual(self.summarize.call_count, 1)
        self.assert_no_acquisition()
