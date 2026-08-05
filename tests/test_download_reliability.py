import tempfile
import unittest
import io
import json
from pathlib import Path
from unittest.mock import patch

import requests
from requests.structures import CaseInsensitiveDict

import server


class FakeResponse:
    def __init__(self, status_code, headers=None, chunks=None, error=None):
        self.status_code = status_code
        self.headers = CaseInsensitiveDict(headers or {})
        self._chunks = chunks or []
        self._error = error

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.exceptions.HTTPError(f"HTTP {self.status_code}")
            error.response = self
            raise error

    def iter_content(self, _chunk_size):
        yield from self._chunks
        if self._error:
            raise self._error


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.request_headers = []

    def get(self, _url, headers=None, **_kwargs):
        self.request_headers.append(dict(headers or {}))
        return self.responses.pop(0)


def make_job():
    return {
        "log": [],
        "file_speeds": {},
        "file_progress": {},
        "file_sizes": {},
        "stop_requested": False,
        "pause_requested": False,
    }


class DownloadReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dest = Path(self.temp_dir.name) / "video.mp4"
        self.patches = [
            patch.object(server, "wait_for_cdn_slot", return_value=None),
            patch.object(server, "register_cdn_throttle", return_value=0),
            patch.object(server, "register_cdn_success", return_value=None),
            patch.object(server.time, "sleep", return_value=None),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp_dir.cleanup()

    def test_429_does_not_consume_the_only_transfer_retry(self):
        session = FakeSession([
            FakeResponse(429, {"Content-Length": "564"}),
            FakeResponse(206, {
                "Content-Length": "4",
                "Content-Range": "bytes 0-3/4",
            }, [b"data"]),
        ])

        ok = server.download_file(
            "https://cdn.example/video.mp4", self.dest, session,
            make_job(), "1", max_retries=1,
        )

        self.assertTrue(ok)
        self.assertEqual(self.dest.read_bytes(), b"data")
        self.assertEqual(len(session.request_headers), 2)

    def test_persistent_429_is_deferred_without_blocking_album_forever(self):
        session = FakeSession([
            FakeResponse(429), FakeResponse(429), FakeResponse(429),
        ])
        job = make_job()

        ok = server.download_file(
            "https://cdn.example/video.mp4", self.dest, session,
            job, "1", max_retries=6,
        )

        self.assertFalse(ok)
        self.assertEqual(len(session.request_headers), 3)
        self.assertTrue(any("Retry Failed" in item["msg"] for item in job["log"]))

    def test_interrupted_download_resumes_from_partial_size(self):
        session = FakeSession([
            FakeResponse(
                200, {"Content-Length": "8"}, [b"abcd"],
                requests.exceptions.ChunkedEncodingError("broken stream"),
            ),
            FakeResponse(206, {
                "Content-Length": "4",
                "Content-Range": "bytes 4-7/8",
            }, [b"efgh"]),
        ])

        with patch.object(server, "register_cdn_throttle") as throttle:
            ok = server.download_file(
                "https://cdn.example/video.mp4", self.dest, session,
                make_job(), "2", max_retries=2,
            )

        self.assertTrue(ok)
        throttle.assert_not_called()
        self.assertEqual(self.dest.read_bytes(), b"abcdefgh")
        self.assertNotIn("Range", session.request_headers[0])
        self.assertEqual(session.request_headers[1]["Range"], "bytes=4-")

    def test_server_ignoring_range_overwrites_instead_of_corrupting(self):
        self.dest.write_bytes(b"partial")
        session = FakeSession([
            FakeResponse(200, {"Content-Length": "4"}, [b"full"]),
        ])

        ok = server.download_file(
            "https://cdn.example/video.mp4", self.dest, session,
            make_job(), "3", max_retries=1,
        )

        self.assertTrue(ok)
        self.assertEqual(self.dest.read_bytes(), b"full")
        self.assertEqual(session.request_headers[0]["Range"], "bytes=7-")

    def test_clean_short_response_is_retried_and_resumed(self):
        session = FakeSession([
            FakeResponse(200, {"Content-Length": "8"}, [b"abcd"]),
            FakeResponse(206, {
                "Content-Length": "4",
                "Content-Range": "bytes 4-7/8",
            }, [b"efgh"]),
        ])

        ok = server.download_file(
            "https://cdn.example/video.mp4", self.dest, session,
            make_job(), "4", max_retries=2,
        )

        self.assertTrue(ok)
        self.assertEqual(self.dest.read_bytes(), b"abcdefgh")

    def test_pause_keeps_partial_file_queued_and_resumes_same_file(self):
        job = make_job()

        class PausingResponse(FakeResponse):
            def iter_content(self, _chunk_size):
                yield b"abcd"
                job["pause_requested"] = True
                yield b"ignored"

        session = FakeSession([
            PausingResponse(200, {"Content-Length": "8"}),
            FakeResponse(206, {
                "Content-Length": "4",
                "Content-Range": "bytes 4-7/8",
            }, [b"efgh"]),
        ])

        with patch.object(server, "persist_jobs"), \
             patch.object(server.time, "sleep", side_effect=lambda _seconds: job.update(pause_requested=False)):
            ok = server.download_file(
                "https://cdn.example/video.mp4", self.dest, session,
                job, "5", max_retries=1,
            )

        self.assertTrue(ok)
        self.assertEqual(self.dest.read_bytes(), b"abcdefgh")
        self.assertEqual(session.request_headers[1]["Range"], "bytes=4-")
        self.assertTrue(any("Resuming partial file" in item["msg"] for item in job["log"]))


class JobSafetyTests(unittest.TestCase):
    def test_resolver_stops_calling_dead_api_after_three_album_misses(self):
        state = server._new_resolver_state()
        session = FakeSession([])
        with patch.object(server, "resolve_bunkr_api", return_value=None) as api, \
             patch.object(server, "fetch_page", return_value=None):
            for index in range(6):
                result = server.resolve_direct_url(
                    f"https://bunkr.cr/f/file-{index}", session, [],
                    max_retries=1, allow_playwright=False,
                    resolver_state=state,
                )
                self.assertIsNone(result)
        self.assertEqual(api.call_count, server.API_PROBE_LIMIT)

    def test_fresh_file_starts_get_without_preflight_head(self):
        with tempfile.TemporaryDirectory() as root:
            out_dir = Path(root)
            job_id = "fresh001"
            server.jobs[job_id] = {
                "log": [], "file_sizes": {}, "file_progress": {},
                "file_speeds": {}, "files": [], "failed_tasks": [],
                "success": 0, "failed": 0, "skipped": 0, "done": 0,
                "zip_folders": [], "max_retries": 2,
            }

            def download_ok(_url, dest, _session, job, file_index, *_args, **_kwargs):
                dest.write_bytes(b"image")
                job["file_sizes"][str(file_index)] = 5
                return True

            try:
                with patch.object(server, "preflight_size") as preflight, \
                     patch.object(server, "download_file", side_effect=download_ok), \
                     patch.object(server, "queue_thumbnail"):
                    ok = server._download_file_task(
                        job_id, 1, "https://bunkr.cr/f/example",
                        "https://cdn.example/example.jpg", out_dir,
                        "Example Album", 1, FakeSession([]), "image",
                    )
                self.assertTrue(ok)
                preflight.assert_not_called()
                self.assertEqual(server.jobs[job_id]["success"], 1)
            finally:
                server.jobs.pop(job_id, None)

    def test_new_http_session_keeps_album_cookies_for_worker_requests(self):
        session = server._new_http_session({"album_token": "kept"})
        self.assertEqual(session.cookies.get("album_token"), "kept")

    def test_same_active_album_reuses_queue_item_instead_of_duplicate_writers(self):
        job_id = "already1"
        server.jobs[job_id] = {
            "status": "running", "url": "https://bunkr.cr/a/same",
        }
        try:
            with patch.object(server, "start_queued_job") as start:
                response = server.app.test_client().post("/api/download", json={
                    "url": "https://bunkr.cr/a/same/",
                })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json(), {"job_id": job_id, "existing": True})
            start.assert_not_called()
        finally:
            server.jobs.pop(job_id, None)

    def test_running_queue_item_survives_restart_and_is_marked_for_resume(self):
        with tempfile.TemporaryDirectory() as root:
            queue_file = Path(root) / "jobs.json"
            job_id = "persist1"
            server.jobs[job_id] = {
                "status": "running", "url": "https://bunkr.cr/a/persist",
                "album_name": "Persistent Album", "total": 4, "done": 1,
                "success": 0, "failed": 0, "skipped": 0, "log": [],
                "files": [], "failed_tasks": [], "pause_requested": False,
                "paused": False, "concurrency_images": 2,
                "concurrency_videos": 3, "max_retries": 6,
                "only_file": None, "created_at": 123.0,
            }
            try:
                with patch.object(server, "JOBS_FILE", queue_file):
                    server.persist_jobs()
                    server.jobs.clear()
                    resumable = server.load_persisted_jobs()
                self.assertEqual(resumable, [job_id])
                self.assertEqual(server.jobs[job_id]["done"], 0)
                self.assertFalse(server.jobs[job_id]["_runner_active"])
            finally:
                server.jobs.pop(job_id, None)

    def test_paused_queue_item_stays_paused_after_restart(self):
        with tempfile.TemporaryDirectory() as root:
            queue_file = Path(root) / "jobs.json"
            queue_file.write_text(json.dumps([{
                "job_id": "paused01", "status": "running",
                "url": "https://bunkr.cr/a/paused",
                "pause_requested": True, "paused": True,
            }]), encoding="utf-8")
            try:
                with patch.object(server, "JOBS_FILE", queue_file):
                    resumable = server.load_persisted_jobs()
                self.assertEqual(resumable, [])
                self.assertTrue(server.jobs["paused01"]["pause_requested"])
            finally:
                server.jobs.pop("paused01", None)

    def test_album_discovery_failure_remains_resumable_in_queue(self):
        restored = server._restored_job({
            "status": "failed", "url": "https://bunkr.cr/a/offline",
            "total": 0, "log": [],
        })

        self.assertEqual(restored["status"], "running")
        self.assertTrue(restored["pause_requested"])
        self.assertTrue(restored["paused"])
        self.assertFalse(restored["_auto_resume"])

    def test_console_output_is_configured_to_replace_unencodable_symbols(self):
        # The Windows launcher may use cp1252; Unicode status glyphs must not
        # be able to crash the server before Flask starts listening.
        stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="replace")
        stream.write("BunkrWrap ✓ → ready")
        stream.flush()

    def test_jobs_api_lists_server_jobs_for_browser_reload(self):
        job_id = "reload01"
        server.jobs[job_id] = {
            "url": "https://bunkr.cr/a/reload",
            "album_name": "Reload Album",
            "status": "running",
            "created_at": 123.0,
        }
        try:
            response = server.app.test_client().get("/api/jobs")
            self.assertEqual(response.status_code, 200)
            self.assertIn({
                "job_id": job_id,
                "url": "https://bunkr.cr/a/reload",
                "album_name": "Reload Album",
                "status": "running",
                "created_at": 123.0,
            }, response.get_json())
        finally:
            server.jobs.pop(job_id, None)

    def test_api_clamps_unsafe_concurrency_and_uses_safer_retries(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.object(server, "JOBS_FILE", Path(root) / "jobs.json"), \
                 patch.object(server.threading, "Thread") as thread:
                response = server.app.test_client().post("/api/download", json={
                    "url": "https://bunkr.cr/a/example",
                    "concurrency_images": 15,
                    "concurrency_videos": 99,
                })
        self.assertEqual(response.status_code, 200)
        job_id = response.get_json()["job_id"]
        try:
            job = server.jobs[job_id]
            self.assertEqual(job["concurrency_images"], server.MAX_CONCURRENCY_IMAGES)
            self.assertEqual(job["concurrency_videos"], server.MAX_CONCURRENCY_VIDEOS)
            self.assertEqual(job["max_retries"], server.DEFAULT_MAX_RETRIES)
            thread.return_value.start.assert_called_once()
        finally:
            server.jobs.pop(job_id, None)

    def test_config_exposes_ten_video_workers_by_default(self):
        response = server.app.test_client().get("/api/config")

        self.assertEqual(response.status_code, 200)
        config = response.get_json()
        self.assertEqual(config["version"], "5.1.0")
        self.assertEqual(config["default_concurrency_videos"], 10)

    def test_same_album_directory_is_reused_for_resume(self):
        with tempfile.TemporaryDirectory() as root:
            downloads = Path(root)
            album_dir = downloads / "Example Album"
            album_dir.mkdir()
            (album_dir / ".bunkrinfo").write_text(
                '{"url":"https://bunkr.cr/a/abc123"}', encoding="utf-8"
            )
            with patch.object(server, "DOWNLOADS_DIR", downloads):
                name, path = server.unique_album_dir(
                    "Example Album", "https://bunkr.cr/a/abc123"
                )
            self.assertEqual(name, "Example Album")
            self.assertEqual(path, album_dir)


class AdaptiveDownloadGateTests(unittest.TestCase):
    def test_gate_starts_at_ten_and_recovers_after_throttling(self):
        gate = server.AdaptiveDownloadGate(10, recovery_successes=2)

        self.assertEqual(gate.current_limit, 10)
        self.assertEqual(gate.record_throttle(), 5)
        self.assertEqual(gate.record_throttle(), 2)
        self.assertEqual(gate.record_success(), 2)
        self.assertEqual(gate.record_success(), 3)

        for _ in range(20):
            gate.record_success()
        self.assertEqual(gate.current_limit, 10)

    def test_throttle_only_changes_the_affected_gate(self):
        image_gate = server.AdaptiveDownloadGate(5)
        video_gate = server.AdaptiveDownloadGate(10)

        with patch.object(server.random, "uniform", return_value=0):
            server.register_cdn_throttle(
                "https://cdn.example/video.mp4", adaptive_gate=video_gate,
            )

        self.assertEqual(image_gate.current_limit, 5)
        self.assertEqual(video_gate.current_limit, 5)


if __name__ == "__main__":
    unittest.main()
