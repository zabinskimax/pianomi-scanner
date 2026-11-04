# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tests for the online scanner's guards (online.py): who may send scans, how
many pages, and the limits an hour and a day. homr isn't loaded: the scans
sent here wait in the queue, unread.

    tools/homr/.venv/Scripts/python -m unittest discover -s tools/homr

They need fastapi and httpx (in the scanner's environment); without them they're skipped.
"""

import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

try:
    from fastapi.testclient import TestClient

    import online
except ImportError:  # pragma: no cover
    online = None  # type: ignore[assignment]

PIANOMI = {"Origin": "https://pianomi.app", "X-Fluent-Keys": "1"}


def png() -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(out, "PNG")
    return out.getvalue()


@unittest.skipIf(online is None, "needs fastapi and httpx")
class OnlineScannerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.saved = (online.MAX_PAGES, online.PAGES_PER_HOUR, online.PAGES_PER_DAY)
        self.scanner = online.Online(cpu=True, salt="test")
        self.client = TestClient(self.scanner.app)
        self.picture = png()

    def tearDown(self) -> None:
        online.MAX_PAGES, online.PAGES_PER_HOUR, online.PAGES_PER_DAY = self.saved
        for job_id in list(self.scanner.jobs.jobs):
            self.scanner.jobs.remove(job_id)

    def send(self, pages: int = 1, headers: dict[str, str] = PIANOMI, ip: str = "203.0.113.7"):  # type: ignore[no-untyped-def]
        files = [("file", (f"page-{i + 1}.png", self.picture, "image/png")) for i in range(pages)]
        return self.client.post("/scan", files=files, headers={**headers, "X-Forwarded-For": ip})

    def test_health_says_the_limits(self) -> None:
        h = self.client.get("/health").json()
        self.assertEqual(h["maxPages"], online.MAX_PAGES)
        self.assertFalse(h["ready"])
        self.assertEqual(h["queue"], 0)

    def test_a_scan_from_pianomi_waits_its_turn(self) -> None:
        res = self.send(2)
        self.assertEqual(res.status_code, 202)
        body = res.json()
        self.assertEqual(body["pages"], 2)
        job = self.client.get(f"/scan/{body['id']}?wait=0").json()
        self.assertEqual(job["status"], "waiting")
        self.assertEqual(self.client.get(f"/scan/{body['id']}/musicxml").status_code, 409)
        self.assertEqual(self.client.delete(f"/scan/{body['id']}").status_code, 204)
        self.assertEqual(self.client.get(f"/scan/{body['id']}?wait=0").json()["code"], "NOT_FOUND")

    def test_only_pianomi_may_send(self) -> None:
        for origin in ("https://evil.example", "https://pianomi.app.evil.example", ""):
            res = self.send(headers={"Origin": origin, "X-Fluent-Keys": "1"})
            self.assertEqual(res.status_code, 403, origin)
        self.assertEqual(self.send(headers={"Origin": "https://pianomi.app"}).status_code, 403)
        for origin in ("https://www.pianomi.app", "https://pianomi-git-scan-max.vercel.app", "http://localhost:5174", "http://127.0.0.1:5180"):
            self.assertEqual(self.send(headers={"Origin": origin, "X-Fluent-Keys": "1"}).status_code, 202, origin)

    def test_the_browser_is_told_who_may_ask(self) -> None:
        ask = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "x-fluent-keys,content-type"}
        ok = self.client.options("/scan", headers={"Origin": "https://pianomi.app", **ask})
        self.assertEqual(ok.headers.get("access-control-allow-origin"), "https://pianomi.app")
        no = self.client.options("/scan", headers={"Origin": "https://evil.example", **ask})
        self.assertNotIn("access-control-allow-origin", no.headers)

    def test_too_many_pages(self) -> None:
        online.MAX_PAGES = 3
        res = self.send(4)
        self.assertEqual(res.status_code, 413)
        self.assertEqual(res.json()["code"], "TOO_MANY_PAGES")
        self.assertEqual(self.send(3).status_code, 202)

    def test_pages_an_hour_for_each_visitor(self) -> None:
        online.PAGES_PER_HOUR = 3
        self.assertEqual(self.send(2).status_code, 202)
        res = self.send(2)
        self.assertEqual(res.status_code, 429)
        self.assertEqual(res.json()["code"], "RATE_LIMITED")
        # A page more still fits; someone else has their own hour.
        self.assertEqual(self.send(1).status_code, 202)
        self.assertEqual(self.send(2, ip="198.51.100.9").status_code, 202)
        # An address made up in front of the proxy's own doesn't make a new visitor.
        self.assertEqual(self.send(1, ip="192.0.2.1, 203.0.113.7").status_code, 429)

    def test_pages_a_day_for_everyone(self) -> None:
        online.PAGES_PER_DAY = 2
        self.assertEqual(self.send(2).status_code, 202)
        res = self.send(1, ip="198.51.100.9")
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.json()["code"], "BUSY")

    def test_a_picture_that_isnt_one(self) -> None:
        res = self.client.post("/scan", files=[("file", ("notes.jpg", b"not a picture", "image/jpeg"))], headers=PIANOMI)
        self.assertEqual(res.status_code, 415)
        self.assertEqual(res.json()["code"], "UNREADABLE_IMAGE")

    def test_visitors_are_kept_apart_without_their_ip(self) -> None:
        a = online.visitor("203.0.113.7", "salt", "2026-11-04")
        self.assertEqual(a, online.visitor("203.0.113.7", "salt", "2026-11-04"))
        self.assertNotEqual(a, online.visitor("203.0.113.8", "salt", "2026-11-04"))
        self.assertNotEqual(a, online.visitor("203.0.113.7", "salt", "2026-11-05"))
        self.assertNotIn("203", a)


if __name__ == "__main__":
    unittest.main()
