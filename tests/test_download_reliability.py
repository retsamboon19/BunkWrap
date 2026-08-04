import tempfile
import unittest
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

        ok = server.download_file(
            "https://cdn.example/video.mp4", self.dest, session,
            make_job(), "2", max_retries=2,
        )

        self.assertTrue(ok)
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


class JobSafetyTests(unittest.TestCase):
    def test_api_clamps_unsafe_concurrency_and_uses_safer_retries(self):
        with patch.object(server.threading, "Thread") as thread:
            response = server.app.test_client().post("/api/download", json={
                "url": "https://bunkr.cr/a/example",
                "concurrency_images": 15,
                "concurrency_videos": 10,
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


if __name__ == "__main__":
    unittest.main()
