# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Pianomi's song scanner online: the scanner on the owner's laptop (server.py)
for everyone, with what a public scanner needs. It answers the same requests,
so the app's client (src/songs/readers/homr.ts) works with either:

GET    /health                 {ok, ready, device, homr, queue, secondsPerPage, maxPages, maxMb}
POST   /scan                   one PDF or page images in order, as multipart form data -> 202 {id, pages}
GET    /scan/<id>?after=<v>    the job, waiting up to 20 s for news; once done, checkBars
GET    /scan/<id>/musicxml     the whole piece, once the job is done
DELETE /scan/<id>              forget it (or stop it)

What it adds to server.py:
- only the app's and the website's own addresses may use it (CORS, and the
  Origin of every scan);
- up to MAX_PAGES pages and MAX_MB a scan;
- PAGES_PER_HOUR pages an hour for each visitor, known by their IP hashed
  with a salt and the day (never kept as it is), and PAGES_PER_DAY for
  everyone, so a busy day can't run up the bill.

The pages are read as on the laptop (server.Jobs: homr, lines.py's second
look, join.py), one scan at a time, and deleted as soon as they're read; a
finished piece waits for its app until collected, or a day at most.

It runs on Modal (cloud.py), or here for testing with the real homr:

    tools/homr/.venv/Scripts/python tools/homr/online.py [--port 8772] [--cpu]

This file uses homr through server.py, so it is AGPL-3.0 too.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import os
import re
import secrets
import shutil
import tempfile
import threading
import traceback
from typing import Any, Protocol

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from server import CLIENT_HEADER, LONG_POLL, Jobs, Reader, ScanError, is_pdf, log, parse_multipart, save_pages

MAX_PAGES = int(os.environ.get("SCANNER_MAX_PAGES") or 16)
MAX_MB = int(os.environ.get("SCANNER_MAX_MB") or 40)
PAGES_PER_HOUR = int(os.environ.get("SCANNER_PAGES_PER_HOUR") or 40)
PAGES_PER_DAY = int(os.environ.get("SCANNER_PAGES_PER_DAY") or 1500)

# pianomi.app, its Vercel previews, and the app's dev servers.
ORIGINS = r"^(https://(www\.)?pianomi\.app|https://pianomi(-[a-z0-9-]+)?\.vercel\.app|http://(localhost|127\.0\.0\.1)(:\d+)?)$"


class Counter(Protocol):
    """Running totals that outlive one request: in memory here, a Modal Dict online."""

    def get(self, key: str) -> int: ...

    def add(self, key: str, n: int) -> int: ...


class MemoryCounter:
    def __init__(self) -> None:
        self.totals: dict[str, int] = {}
        self.lock = threading.Lock()

    def get(self, key: str) -> int:
        with self.lock:
            return self.totals.get(key, 0)

    def add(self, key: str, n: int) -> int:
        with self.lock:
            self.totals[key] = self.totals.get(key, 0) + n
            return self.totals[key]


def fail(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"code": code, "message": message}, status_code=status, headers={"Cache-Control": "no-store"})


def count_pages(files: list[tuple[str, str, bytes]]) -> int:
    """How many pages the files hold, before anything is drawn: a PDF's pages, one per picture."""
    import pypdfium2 as pdfium

    pages = 0
    for name, _ctype, data in files:
        if is_pdf(data):
            try:
                pdf = pdfium.PdfDocument(data)
            except pdfium.PdfiumError as e:
                if "password" in str(e).lower():
                    raise ScanError("ENCRYPTED_PDF", f"{name} is password-protected.") from e
                raise ScanError("DAMAGED_PDF", f"{name} couldn't be opened as a PDF.") from e
            try:
                pages += len(pdf)
            finally:
                pdf.close()
        else:
            pages += 1
    return pages


def visitor(ip: str, salt: str, day: str) -> str:
    """Who is scanning, without keeping their IP: the same visitor reads the same all day, and differently tomorrow."""
    return hashlib.sha256(f"{salt}|{day}|{ip}".encode()).hexdigest()[:20]


class Online:
    """The scanner, its jobs and its web app."""

    def __init__(self, cpu: bool = True, counter: Counter | None = None, salt: str | None = None, verbose: bool = False) -> None:
        self.reader = Reader(cpu=cpu, verbose=verbose)
        self.jobs = Jobs(self.reader)
        self.counter = counter or MemoryCounter()
        self.salt = salt or os.environ.get("SCANNER_SALT") or secrets.token_hex(16)
        self.app = self._make_app()

    def start(self) -> None:
        """Load homr and start reading, in the background: requests are answered meanwhile."""
        threading.Thread(target=self.reader.load, daemon=True).start()
        threading.Thread(target=self.jobs.work, daemon=True).start()
        threading.Thread(target=self.jobs.tidy, daemon=True).start()

    # Limits -----------------------------------------------------------------

    def _within_limits(self, request: Request, pages: int) -> JSONResponse | None:
        now = datetime.datetime.now(datetime.timezone.utc)
        day = now.strftime("%Y-%m-%d")
        # The right-most address is the one the host's proxy added; the ones before it the sender can make up.
        forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[-1].strip()
        ip = forwarded or (request.client.host if request.client else "unknown")
        hour_key = f"hour:{now.strftime('%Y-%m-%dT%H')}:{visitor(ip, self.salt, day)}"
        day_key = f"day:{day}"
        if self.counter.get(day_key) + pages > PAGES_PER_DAY:
            log(f"Refused {pages} page(s): {PAGES_PER_DAY} pages read today already")
            return fail(503, "BUSY", "The scanner has read all it can for today. Try again tomorrow.")
        if self.counter.get(hour_key) + pages > PAGES_PER_HOUR:
            return fail(429, "RATE_LIMITED", f"That's more than {PAGES_PER_HOUR} pages in an hour. Try again in a little while.")
        self.counter.add(day_key, pages)
        self.counter.add(hour_key, pages)
        return None

    # The web app ------------------------------------------------------------

    def _make_app(self) -> FastAPI:
        app = FastAPI(title="Pianomi song scanner", docs_url=None, redoc_url=None, openapi_url=None)
        app.add_middleware(
            CORSMiddleware,
            allow_origin_regex=ORIGINS,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=[CLIENT_HEADER, "Content-Type"],
            max_age=3600,
        )
        jobs, reader = self.jobs, self.reader

        @app.get("/health")
        def health() -> JSONResponse:
            return JSONResponse(
                {
                    "ok": not reader.failed,
                    "ready": reader.ready,
                    "device": reader.device or "cpu",
                    "homr": reader.version,
                    "queue": jobs.busy(),
                    "secondsPerPage": jobs.seconds_per_page(),
                    "error": "The scanner couldn't start." if reader.failed else None,
                    "maxPages": MAX_PAGES,
                    "maxMb": MAX_MB,
                },
                headers={"Cache-Control": "no-store"},
            )

        @app.post("/scan")
        async def scan(request: Request) -> Response:
            if not re.match(ORIGINS, request.headers.get("origin") or ""):
                return fail(403, "FORBIDDEN", "Only Pianomi can use this scanner.")
            if not request.headers.get(CLIENT_HEADER):
                return fail(403, "FORBIDDEN", f"Scans need the {CLIENT_HEADER} header.")
            length = int(request.headers.get("content-length") or 0)
            if length > MAX_MB * 1024 * 1024:
                return fail(413, "TOO_LARGE", f"Up to {MAX_MB} MB can be scanned at a time.")
            body = await request.body()
            if not body:
                return fail(411, "NO_FILES", "No file was sent.")
            if len(body) > MAX_MB * 1024 * 1024:
                return fail(413, "TOO_LARGE", f"Up to {MAX_MB} MB can be scanned at a time.")
            ctype = request.headers.get("content-type") or ""
            try:
                files = parse_multipart(body, ctype) if ctype.startswith("multipart/form-data") else [("upload", ctype, body)]
                if not files:
                    return fail(400, "NO_FILES", "No PDF or picture was sent.")
                pages = await run_in_threadpool(count_pages, files)
            except ScanError as e:
                return fail(e.status, e.code, str(e))
            if pages > MAX_PAGES:
                return fail(413, "TOO_MANY_PAGES", f"Up to {MAX_PAGES} pages can be scanned at a time.")
            refused = self._within_limits(request, pages)
            if refused:
                return refused
            folder = tempfile.mkdtemp(prefix="pianomi-scan-")
            try:
                images = await run_in_threadpool(save_pages, files, folder)
            except ScanError as e:
                shutil.rmtree(folder, ignore_errors=True)
                return fail(e.status, e.code, str(e))
            except Exception as e:  # noqa: BLE001
                shutil.rmtree(folder, ignore_errors=True)
                traceback.print_exc()
                return fail(500, "READ_FAILED", f"The files couldn't be prepared: {e}")
            # The file's name stays out of the scanner's log online.
            job = jobs.add("upload", folder, images)
            return JSONResponse({"id": job.id, "pages": len(images)}, status_code=202, headers={"Cache-Control": "no-store"})

        @app.get("/scan/{job_id}")
        def view(job_id: str, after: int = -1, wait: float = LONG_POLL) -> JSONResponse:
            jobs.touch()
            job = jobs.jobs.get(job_id)
            if job is None:
                return fail(404, "NOT_FOUND", "There's no scan with this number: the scanner was restarted, or the scan is over a day old.")
            wait = max(0.0, min(wait, LONG_POLL))
            if wait > 0:
                jobs.wait(job, after, wait)
            return JSONResponse(job.view(jobs.ahead_of(job)), headers={"Cache-Control": "no-store"})

        @app.get("/scan/{job_id}/musicxml")
        def musicxml(job_id: str) -> Response:
            job = jobs.jobs.get(job_id)
            if job is None:
                return fail(404, "NOT_FOUND", "There's no scan with this number.")
            if job.status != "done" or job.xml is None:
                return fail(409, "NOT_READY", "This scan isn't finished.")
            return Response(job.xml, media_type="application/vnd.recordare.musicxml+xml; charset=utf-8", headers={"Cache-Control": "no-store"})

        @app.delete("/scan/{job_id}")
        def remove(job_id: str) -> Response:
            jobs.touch()
            if jobs.remove(job_id):
                return Response(status_code=204)
            return fail(404, "NOT_FOUND", "There's no scan with this number.")

        return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Pianomi's online song scanner, on this computer for testing")
    parser.add_argument("--port", type=int, default=8772)
    parser.add_argument("--cpu", action="store_true", help="Read on the CPU only, as online")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    import uvicorn

    online = Online(cpu=args.cpu, verbose=args.verbose)
    online.start()
    log(f"The online scanner, for testing, listens on http://127.0.0.1:{args.port}")
    uvicorn.run(online.app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
