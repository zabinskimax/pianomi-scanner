# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fluent Keys' song scanner: homr (https://github.com/liebharc/homr) behind a
small web service on this computer, so the app can turn a scanned PDF or
photos of sheet music into MusicXML for free.

    npm run homr                 (or: .venv/Scripts/python server.py)
    --port 8771  --cpu  --verbose  --init (download the models, then stop)
    --stop-when-idle 20          (stop after 20 minutes without scans; the
                                 dev server starts it that way, launch.ts)

GET    /health                 {ok, ready, device, homr, queue, secondsPerPage}
POST   /scan                   one PDF or page images in order, as multipart
                               form data (or one file as the raw body) -> 202 {id, pages}
GET    /scan/<id>?after=<v>    the job; waits up to 20 s for a version after v; once done,
                               checkBars: the bars to check against the sheet music
GET    /scan/<id>/musicxml     the whole piece, once the job is done
DELETE /scan/<id>              forget it (or stop it)

Pages are read one at a time, in order, by one worker: PDF pages are drawn
at 300 dpi with pypdfium2, photos are turned upright, homr reads each page,
lines homr missed or found only in part are read again on their own
(lines.py), and join.py joins the pages into one piano part. Only
127.0.0.1 is served.

This file uses homr, which is AGPL-3.0, so it is AGPL-3.0 too. homr's model
weights were trained on data that includes non-commercial material; see
README.md.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import io
import json
import os
import queue
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from join import JoinError, System, join_pages

MAX_UPLOAD = 200 * 1024 * 1024
MAX_PAGES = 40
DPI = 300
# Longest side of a page image; bigger ones are scaled down (homr resizes anyway).
MAX_SIDE = 5000
# A margin around each page, as a share of its longer side; see with_margin.
MARGIN = 0.02
LONG_POLL = 20.0
# Finished scans wait this long to be collected (an app closed mid-scan picks them up when it opens).
KEEP_FINISHED = 24 * 60 * 60
# With --stop-when-idle, a finished scan nobody collected keeps the scanner running this long.
UNCOLLECTED_GRACE = 2 * 60 * 60
# A custom header makes a browser ask before sending, which no other website gets past.
CLIENT_HEADER = "X-Fluent-Keys"


def log(*args: Any) -> None:
    # A file name the console can't spell ("après" on a Central European
    # Windows, whose code page has é but not è) mustn't stop the scan.
    line = " ".join([time.strftime("%H:%M:%S"), *map(str, args)])
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(line.encode(enc, "replace").decode(enc), flush=True)


class ScanError(Exception):
    """A problem with what was sent, told plainly."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


# ---------------------------------------------------------------------------
# homr


class Reader:
    """homr, loaded once. Its models stay in memory between pages."""

    def __init__(self, cpu: bool, verbose: bool) -> None:
        self.want_cpu = cpu
        self.verbose = verbose
        self.ready = False
        self.failed: str | None = None
        # Known once homr is loaded.
        self.device: str | None = None
        self.version: str | None = None
        self._progress: Callable[[float, str], None] | None = None
        # The lines homr read on the last page: each its staves, as fractions of the page (x0, y0, x1, y1).
        self.systems: list[list[tuple[float, float, float, float]]] = []
        # The same lines for the page map: x0, y0, x1, y1 and barline xs, fractions of the page as sent.
        self.found: list[tuple[float, float, float, float, list[float]]] = []
        self.page_size: tuple[int, int] | None = None
        self._frame: tuple[int, int, Any] = (1, 1, None)

    def load(self) -> None:
        try:
            import importlib.metadata

            import onnxruntime as ort
            from homr import main as homr_main
            from homr.music_xml_generator import XmlGeneratorArguments
            from homr.onnx_providers import cuda_available
            from homr.title_detection import download_ocr_weights

            self.version = importlib.metadata.version("homr")
            gpu = not self.want_cpu and cuda_available()
            if gpu:
                try:
                    ort.preload_dlls()
                except Exception as e:  # noqa: BLE001
                    log("The GPU can't be used, reading on the CPU:", e)
                    gpu = False
            self.device = "gpu" if gpu else "cpu"
            ort.set_default_logger_severity(2 if self.verbose else 3)
            log(f"homr {self.version} on the {self.device.upper()}; getting the models (only downloaded once)")
            with self._quiet():
                homr_main.download_weights(gpu, gpu, False)
                download_ocr_weights()
            self._main = homr_main
            self._xml_args = XmlGeneratorArguments(False, None, None)
            self._configs = {
                title: homr_main.ProcessingConfig(False, False, False, False, -1, gpu, gpu, False, title)
                for title in (True, False)
            }
            self._hook_steps()
            self.ready = True
            log("Ready")
        except Exception as e:  # noqa: BLE001
            self.failed = f"{type(e).__name__}: {e}"
            log("homr couldn't be loaded:", self.failed)
            traceback.print_exc()

    def _hook_steps(self) -> None:
        """Report how far into a page homr is, from the steps it goes through."""
        steps = {
            "get_predictions": (0.2, "Finding the staves"),
            "detect_staff": (0.5, "Reading the notes"),
            "parse_staffs": (0.95, "Writing the page"),
        }
        for name, (fraction, next_step) in steps.items():
            original = getattr(self._main, name, None)
            if original is None:
                continue

            @functools.wraps(original)
            def hooked(*args: Any, _original: Any = original, _f: float = fraction, _s: str = next_step, _name: str = name, **kwargs: Any) -> Any:
                if _name == "parse_staffs" and len(args) >= 3:
                    self._keep_systems(args[1], args[2].shape, kwargs.get("page_to_input_image"))
                result = _original(*args, **kwargs)
                if _name == "parse_staffs":
                    self._keep_barlines(result)
                if self._progress:
                    self._progress(_f, _s)
                return result

            setattr(self._main, name, hooked)

    def _keep_systems(self, multi_staffs: Any, shape: tuple[int, ...], to_input: Callable[[tuple[float, float]], tuple[float, float]] | None = None) -> None:
        """
        Where homr found the lines it's about to read: for lines.py's second
        look, and for the page map (`found`) mapped back through homr's own
        cropping and resizing to the page as sent.
        """
        h, w = shape[0], shape[1]
        self.systems = [[(s.min_x / w, s.min_y / h, s.max_x / w, s.max_y / h) for s in ms.staffs] for ms in multi_staffs]
        self._frame = (*(self.page_size if self.page_size and to_input else (w, h)), to_input)
        pw, ph = self._frame[:2]
        at = to_input or (lambda p: p)
        self.found = []
        for ms in multi_staffs:
            corners = [(at((s.min_x, s.min_y)), at((s.max_x, s.max_y))) for s in ms.staffs]
            self.found.append((
                min(a[0] for a, _ in corners) / pw,
                min(a[1] for a, _ in corners) / ph,
                max(b[0] for _, b in corners) / pw,
                max(b[1] for _, b in corners) / ph,
                [],
            ))

    def _keep_barlines(self, voices: Any) -> None:
        """
        The x of each bar's closing barline on each line, from the barlines
        homr's reading of the top voice placed (their place on the page as sent).
        Only when its rows are the lines it found.
        """
        try:
            rows: list[list[float]] = [[]]
            for symbol in voices[0]:
                if symbol.rhythm == "newline":
                    rows.append([])
                elif symbol.rhythm == "barline" and symbol.image_coordinates is not None:
                    rows[-1].append(symbol.image_coordinates[0] / self._frame[0])
            if not rows[-1]:
                rows.pop()
            if len(rows) == len(self.found):
                self.found = [(*line[:4], xs) for line, xs in zip(self.found, rows)]
        except Exception as e:  # noqa: BLE001 (no bar positions; the lines still hold)
            log("No barlines for the page map:", f"{type(e).__name__}: {e}")

    @contextlib.contextmanager
    def _quiet(self):  # type: ignore[no-untyped-def]
        """homr talks a lot on stderr; keep it unless --verbose."""
        if self.verbose:
            yield io.StringIO()
            return
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            yield buffer

    def read_page(self, image: str, title: bool, progress: Callable[[float, str], None]) -> str:
        self._progress = progress
        self.systems = []
        self.found = []
        self.page_size = None
        try:
            from PIL import Image

            with Image.open(image) as img:
                self.page_size = img.size
        except Exception:  # noqa: BLE001 (the map's frame falls back to homr's own)
            pass
        try:
            with self._quiet() as said:
                try:
                    out = self._main.process_image(image, self._configs[title], self._xml_args)
                except Exception as e:
                    raise PageError(str(e), said.getvalue()) from e
            with open(out, encoding="utf-8") as f:
                return f.read()
        finally:
            self._progress = None


class PageError(Exception):
    def __init__(self, message: str, log_text: str) -> None:
        super().__init__(message)
        self.log_text = log_text

    @property
    def no_music(self) -> bool:
        return bool(re.search(r"No (noteheads|staffs) found|no staff", str(self), re.I))


# ---------------------------------------------------------------------------
# Jobs


@dataclass
class Job:
    id: str
    name: str
    folder: str
    pages: list[str]
    status: str = "waiting"  # waiting | reading | done | error | cancelled
    done: int = 0
    fraction: float = 0.0
    step: str = "Waiting for its turn"
    skipped: list[int] = field(default_factory=list)
    xml: str | None = None
    check_bars: list[str] = field(default_factory=list)
    # Where the bars are on the pages, once done (see page_map).
    page_bars: list[dict[str, Any] | None] = field(default_factory=list)
    page_lines: list[dict[str, Any]] = field(default_factory=list)
    code: str | None = None
    message: str | None = None
    created: float = field(default_factory=time.time)
    finished: float | None = None
    seconds: list[float] = field(default_factory=list)
    version: int = 0

    def view(self, queue_position: int) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "pages": len(self.pages),
            "done": self.done,
            "fraction": round(self.fraction, 3),
            "step": self.step,
            "skipped": self.skipped,
            "checkBars": self.check_bars,
            "pageBars": self.page_bars,
            "pageLines": self.page_lines,
            "ahead": queue_position,
            "code": self.code,
            "message": self.message,
            "version": self.version,
        }


class Jobs:
    def __init__(self, reader: Reader) -> None:
        self.reader = reader
        self.jobs: dict[str, Job] = {}
        self.waiting: queue.Queue[str] = queue.Queue()
        self.order: list[str] = []
        self.changed = threading.Condition()
        self.page_seconds: list[float] = []
        # Anything to do with a scan; asking for /health isn't.
        self.last_active = time.time()

    def touch(self) -> None:
        self.last_active = time.time()

    def update(self, job: Job, **changes: Any) -> None:
        with self.changed:
            for k, v in changes.items():
                setattr(job, k, v)
            job.version += 1
            self.last_active = time.time()
            self.changed.notify_all()

    def add(self, name: str, folder: str, pages: list[str]) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], name=name, folder=folder, pages=pages)
        with self.changed:
            self.jobs[job.id] = job
            self.order.append(job.id)
            self.last_active = time.time()
        self.waiting.put(job.id)
        log(f"Scan {job.id}: {name}, {len(pages)} page(s)")
        return job

    def ahead_of(self, job: Job) -> int:
        with self.changed:
            return sum(
                1
                for i in self.order
                if i != job.id and self.jobs.get(i) and self.jobs[i].status in ("waiting", "reading") and self.order.index(i) < self.order.index(job.id)
            )

    def busy(self) -> int:
        with self.changed:
            return sum(1 for j in self.jobs.values() if j.status in ("waiting", "reading"))

    def wait(self, job: Job, after: int, seconds: float) -> None:
        with self.changed:
            self.changed.wait_for(lambda: job.version > after or job.status in ("done", "error", "cancelled"), timeout=seconds)

    def remove(self, job_id: str) -> bool:
        with self.changed:
            job = self.jobs.pop(job_id, None)
            if job_id in self.order:
                self.order.remove(job_id)
        if job is None:
            return False
        reading = job.status == "reading"
        if job.status in ("waiting", "reading"):
            self.update(job, status="cancelled", step="Stopped")
        # A page being read is the worker's to tidy up.
        if not reading:
            shutil.rmtree(job.folder, ignore_errors=True)
        return True

    def seconds_per_page(self) -> float | None:
        recent = self.page_seconds[-10:]
        return round(sum(recent) / len(recent), 1) if recent else None

    # The worker -------------------------------------------------------------

    def work(self) -> None:
        while True:
            job_id = self.waiting.get()
            job = self.jobs.get(job_id)
            if job is None or job.status == "cancelled":
                continue
            while not self.reader.ready and not self.reader.failed:
                time.sleep(0.5)
            try:
                self.run(job)
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                self.update(job, status="error", code="READ_FAILED", message=f"The scanner ran into a problem: {e}", finished=time.time())
            finally:
                if job.status == "cancelled" or job.id not in self.jobs:
                    shutil.rmtree(job.folder, ignore_errors=True)

    def run(self, job: Job) -> None:
        if self.reader.failed:
            self.update(job, status="error", code="NOT_LOADED", message=f"homr couldn't be loaded: {self.reader.failed}", finished=time.time())
            return
        n = len(job.pages)
        xmls: list[str] = []
        placed: list[list[System] | None] = []  # each page's lines, for the page map
        problems: list[PageError] = []
        self.update(job, status="reading", step=f"Reading page 1 of {n}" if n > 1 else "Reading the page")
        for i, page in enumerate(job.pages):
            if job.status == "cancelled":
                log(f"Scan {job.id}: stopped")
                return
            start = time.time()
            prefix = f"Page {i + 1} of {n}: " if n > 1 else ""
            self.update(job, fraction=0.05, step=f"{prefix}Getting the page ready")

            def progress(fraction: float, step: str, _prefix: str = prefix) -> None:
                self.update(job, fraction=fraction, step=f"{_prefix}{step}")

            try:
                xml = self.reader.read_page(page, title=i == 0, progress=progress)
                xml, lines = self.second_look(job, page, xml, prefix, i)
                xmls.append(xml)
                placed.append(lines)
            except PageError as e:
                problems.append(e)
                job.skipped.append(i + 1)
                log(f"Scan {job.id}: page {i + 1} not read ({e})")
                if self.reader.verbose:
                    log(e.log_text[-2000:])
            took = time.time() - start
            self.page_seconds.append(took)
            job.seconds.append(round(took, 1))
            self.update(job, done=i + 1, fraction=0.0)
            log(f"Scan {job.id}: page {i + 1} of {n} in {took:.1f} s")

        if not xmls:
            no_music = all(p.no_music for p in problems)
            self.update(
                job,
                status="error",
                code="NO_MUSIC" if no_music else "READ_FAILED",
                message="No sheet music was found on the pages." if no_music else f"homr couldn't read the pages: {problems[0]}",
                finished=time.time(),
            )
            return
        try:
            joined = join_pages(xmls, systems=placed)
        except JoinError as e:
            self.update(job, status="error", code="NO_MUSIC", message=str(e), finished=time.time())
            return
        page_bars, page_lines = page_map(joined.places)
        self.update(job, status="done", xml=joined.xml, check_bars=joined.check_bars, page_bars=page_bars, page_lines=page_lines, step="Done", finished=time.time())
        log(f"Scan {job.id}: done in {sum(job.seconds):.0f} s" + (f", pages {job.skipped} skipped" if job.skipped else ""))
        shutil.rmtree(job.folder, ignore_errors=True)

    def second_look(self, job: Job, page: str, xml: str, prefix: str, index: int) -> tuple[str, list[System] | None]:
        """
        The page with the lines homr missed or found in part read again
        (lines.py), and where its lines sit on page `index`. Any trouble
        keeps the page as read.
        """
        systems = list(self.reader.systems)
        found = list(self.reader.found)
        size = self.reader.page_size  # reading a line again changes these
        placed = found
        try:
            from lines import second_look  # numpy and Pillow: homr's environment

            from lines import Box

            boxes: list[Box] = []
            xml, again = second_look(
                page,
                xml,
                systems,
                lambda line: self.reader.read_page(line, title=False, progress=lambda _f, _s: None),
                job.folder,
                lambda step: self.update(job, fraction=0.97, step=f"{prefix}{step}"),
                [Box(*b[:4], barlines=b[4]) for b in found],
                boxes,
            )
            placed = [(b.x0, b.y0, b.x1, b.y1, b.barlines) for b in boxes]
        except Exception as e:  # noqa: BLE001
            log(f"Scan {job.id}: no second look at {os.path.basename(page)} ({type(e).__name__}: {e})")
            return xml, page_systems(index, found, size)
        if again:
            log(f"Scan {job.id}: {os.path.basename(page)}: {again} {'line' if again == 1 else 'lines'} read again")
        return xml, page_systems(index, placed, size)

    def idle(self, seconds: float) -> bool:
        """Nothing waiting, read or left to collect, and no scan news for this long."""
        with self.changed:
            # A finished scan nobody collected yet (the app was closed) keeps it going a while.
            keep = any(
                j.status in ("waiting", "reading") or (j.status == "done" and time.time() - (j.finished or 0) < UNCOLLECTED_GRACE)
                for j in self.jobs.values()
            )
        return not keep and time.time() - self.last_active > seconds

    def stop_when_idle(self, minutes: float, server: "Server") -> None:
        while not self.idle(minutes * 60):
            time.sleep(15)
        log(f"No scans for {minutes:g} minutes: stopping")
        server.shutdown()

    def tidy(self) -> None:
        """Forget finished scans nobody collected."""
        while True:
            time.sleep(300)
            now = time.time()
            for job in list(self.jobs.values()):
                if job.finished and now - job.finished > KEEP_FINISHED:
                    self.remove(job.id)


# ---------------------------------------------------------------------------
# Files in


def parse_multipart(body: bytes, content_type: str) -> list[tuple[str, str, bytes]]:
    """(file name, type, bytes) of each file in multipart form data, in order."""
    m = re.search(r'boundary="?([^";]+)"?', content_type)
    if not m:
        raise ScanError("BAD_REQUEST", "The form data has no boundary.")
    boundary = b"--" + m.group(1).encode("latin-1")
    files = []
    for chunk in body.split(boundary)[1:]:
        if chunk.startswith(b"--"):
            break
        head, sep, data = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        headers = head.decode("utf-8", "replace")
        name = re.search(r'filename="([^"]*)"', headers)
        if not name:
            continue  # a plain field
        ctype = re.search(r"content-type:\s*([^\r\n;]+)", headers, re.I)
        files.append((name.group(1), ctype.group(1).strip() if ctype else "", data))
    return files


def is_pdf(data: bytes) -> bool:
    return b"%PDF-" in data[:1024]


def page_systems(page: int, boxes: list[tuple[float, float, float, float, list[float]]], size: tuple[int, int] | None) -> list[System] | None:
    """
    A page's lines for the page map: fractions of the page as saved, with
    `with_margin`'s margin taken off, so they are fractions of the page the
    player sent. None when the page's size isn't known.
    """
    if not size:
        return None
    w, h = size
    pad = round(MARGIN * max(w, h) / (1 + 2 * MARGIN))
    inner_w, inner_h = max(1, w - 2 * pad), max(1, h - 2 * pad)

    def fx(x: float) -> float:
        return round(min(1.0, max(0.0, (x * w - pad) / inner_w)), 4)

    def fy(y: float) -> float:
        return round(min(1.0, max(0.0, (y * h - pad) / inner_h)), 4)

    return [System(page, fx(x0), fy(y0), fx(x1), fy(y1), [fx(x) for x in barlines]) for x0, y0, x1, y1, barlines in boxes]


def page_map(places: list[Any]) -> tuple[list[dict[str, Any] | None], list[dict[str, Any]]]:
    """
    The job's pageBars (one for each bar of the joined score, null where the
    bar has no known place) and pageLines (the lines they sit on, in order).
    A bar's x0 and x1 are given only when its line's barlines say them.
    """
    lines: list[dict[str, Any]] = []
    index: dict[int, int] = {}
    bars: list[dict[str, Any] | None] = []
    for place in places:
        if place is None:
            bars.append(None)
            continue
        s = place.system
        if id(s) not in index:
            index[id(s)] = len(lines)
            lines.append({"page": s.page, "x0": s.x0, "x1": s.x1, "y0": s.y0, "y1": s.y1})
        bar: dict[str, Any] = {"page": s.page, "line": index[id(s)]}
        if place.x0 is not None and place.x1 is not None:
            bar["x0"] = round(place.x0, 4)
            bar["x1"] = round(place.x1, 4)
        bars.append(bar)
    return bars, lines


def with_margin(img: "Image.Image") -> "Image.Image":
    """
    The page with a margin of its own edge colour: white around a clean scan,
    more desk around a photo. homr finds the paper first, and on a page that is
    white right to its edges it sometimes loses a whole staff (on the bench,
    usually the first system's left hand, on 21 of 72 clean pages); with a
    margin it read all of them in full.
    """
    import numpy as np
    from PIL import Image

    pad = round(max(img.size) * MARGIN)
    return Image.fromarray(np.pad(np.asarray(img), ((pad, pad), (pad, pad), (0, 0)), mode="edge"))


def save_pages(files: list[tuple[str, str, bytes]], folder: str) -> list[str]:
    """Each file as page images in the folder: PDFs page by page, photos upright."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        # iPhone photos (HEIC), where pillow-heif is installed (online it is).
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except ImportError:
        pass

    pages: list[str] = []

    def save(img: Image.Image) -> None:
        if len(pages) >= MAX_PAGES:
            raise ScanError("TOO_MANY_PAGES", f"Up to {MAX_PAGES} pages can be scanned at a time.", 413)
        img = img.convert("RGB")
        if max(img.size) > MAX_SIDE:
            scale = MAX_SIDE / max(img.size)
            img = img.resize((round(img.width * scale), round(img.height * scale)), Image.Resampling.LANCZOS)
        img = with_margin(img)
        path = os.path.join(folder, f"page-{len(pages) + 1}.png")
        img.save(path)
        pages.append(path)

    for name, _ctype, data in files:
        if is_pdf(data):
            import pypdfium2 as pdfium

            try:
                pdf = pdfium.PdfDocument(data)
            except pdfium.PdfiumError as e:
                if "password" in str(e).lower():
                    raise ScanError("ENCRYPTED_PDF", f"{name} is password-protected.") from e
                raise ScanError("DAMAGED_PDF", f"{name} couldn't be opened as a PDF.") from e
            try:
                if len(pdf) + len(pages) > MAX_PAGES:
                    raise ScanError("TOO_MANY_PAGES", f"Up to {MAX_PAGES} pages can be scanned at a time.", 413)
                for page in pdf:
                    w, h = page.get_size()
                    scale = min(DPI / 72, MAX_SIDE / max(w, h, 1))
                    save(page.render(scale=scale).to_pil())
            finally:
                pdf.close()
            continue
        try:
            img = Image.open(io.BytesIO(data))
            img.load()
        except (UnidentifiedImageError, OSError) as e:
            raise ScanError("UNREADABLE_IMAGE", f"{name} isn't a picture that can be read (use JPEG, PNG or HEIC).", 415) from e
        save(ImageOps.exif_transpose(img))
    if not pages:
        raise ScanError("NO_FILES", "No PDF or picture was sent.")
    return pages


# ---------------------------------------------------------------------------
# HTTP


class Handler(BaseHTTPRequestHandler):
    server_version = "FluentKeysHomr/1"
    jobs: Jobs
    reader: Reader
    allowed_hosts: set[str]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        if self.reader.verbose:
            log(self.address_string(), format % args)

    def send_json(self, status: int, data: Any) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def fail(self, status: int, code: str, message: str) -> None:
        self.send_json(status, {"code": code, "message": message})

    def check_host(self) -> bool:
        """Only this computer's own names, so another website can't reach it by a DNS trick."""
        host = (self.headers.get("Host") or "").lower()
        if host in self.allowed_hosts:
            return True
        self.fail(HTTPStatus.FORBIDDEN, "FORBIDDEN", "Only this computer can use the scanner.")
        return False

    def route(self) -> tuple[str, list[str], dict[str, list[str]]]:
        url = urlsplit(self.path)
        return url.path, [p for p in url.path.split("/") if p], parse_qs(url.query)

    def do_GET(self) -> None:  # noqa: N802
        if not self.check_host():
            return
        path, parts, query = self.route()
        if path == "/health":
            self.send_json(
                200,
                {
                    "ok": not self.reader.failed,
                    "ready": self.reader.ready,
                    "device": self.reader.device,
                    "homr": self.reader.version,
                    "queue": self.jobs.busy(),
                    "secondsPerPage": self.jobs.seconds_per_page(),
                    "error": self.reader.failed,
                },
            )
            return
        if len(parts) >= 2 and parts[0] == "scan":
            self.jobs.touch()
            job = self.jobs.jobs.get(parts[1])
            if job is None:
                self.fail(HTTPStatus.NOT_FOUND, "NOT_FOUND", "There's no scan with this number: the scanner was restarted, or the scan is over a day old.")
                return
            if len(parts) == 2:
                after = int(query.get("after", ["-1"])[0] or -1)
                wait = min(float(query.get("wait", [str(LONG_POLL)])[0] or 0), 60.0)
                if wait > 0:
                    self.jobs.wait(job, after, wait)
                self.send_json(200, job.view(self.jobs.ahead_of(job)))
                return
            if len(parts) == 3 and parts[2] == "musicxml":
                if job.status != "done" or job.xml is None:
                    self.fail(HTTPStatus.CONFLICT, "NOT_READY", "This scan isn't finished.")
                    return
                body = job.xml.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.recordare.musicxml+xml; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        self.fail(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Nothing here.")

    def do_POST(self) -> None:  # noqa: N802
        if not self.check_host():
            return
        path, _, _ = self.route()
        if path != "/scan":
            self.fail(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Nothing here.")
            return
        if not self.headers.get(CLIENT_HEADER):
            self.fail(HTTPStatus.FORBIDDEN, "FORBIDDEN", f"Scans need the {CLIENT_HEADER} header.")
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self.fail(HTTPStatus.LENGTH_REQUIRED, "NO_FILES", "No file was sent.")
            return
        if length > MAX_UPLOAD:
            self.fail(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "TOO_LARGE", f"Up to {MAX_UPLOAD // (1024 * 1024)} MB can be scanned at a time.")
            return
        body = self.rfile.read(length)
        ctype = self.headers.get("Content-Type") or ""
        folder = tempfile.mkdtemp(prefix="fk-homr-")
        try:
            if ctype.startswith("multipart/form-data"):
                files = parse_multipart(body, ctype)
            else:
                files = [(self.headers.get("X-File-Name") or "upload", ctype, body)]
            pages = save_pages(files, folder)
        except ScanError as e:
            shutil.rmtree(folder, ignore_errors=True)
            self.fail(e.status, e.code, str(e))
            return
        except Exception as e:  # noqa: BLE001
            shutil.rmtree(folder, ignore_errors=True)
            traceback.print_exc()
            self.fail(HTTPStatus.INTERNAL_SERVER_ERROR, "READ_FAILED", f"The files couldn't be prepared: {e}")
            return
        job = self.jobs.add(files[0][0] if files else "upload", folder, pages)
        self.send_json(HTTPStatus.ACCEPTED, {"id": job.id, "pages": len(pages)})

    def do_DELETE(self) -> None:  # noqa: N802
        if not self.check_host():
            return
        _, parts, _ = self.route()
        self.jobs.touch()
        if len(parts) == 2 and parts[0] == "scan" and self.jobs.remove(parts[1]):
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        self.fail(HTTPStatus.NOT_FOUND, "NOT_FOUND", "There's no scan with this number.")


class Server(ThreadingHTTPServer):
    # On Windows, reusing an address lets a second program take a port that's in use, without an error.
    allow_reuse_address = sys.platform != "win32"
    daemon_threads = True

    def server_bind(self) -> None:
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main() -> None:
    parser = argparse.ArgumentParser(description="Fluent Keys' song scanner (homr) on this computer")
    parser.add_argument("--port", type=int, default=int(os.environ.get("HOMR_PORT") or 8771))
    parser.add_argument("--cpu", action="store_true", help="Don't use the GPU even when there is one")
    parser.add_argument("--verbose", action="store_true", help="Show homr's own output and every request")
    parser.add_argument("--init", action="store_true", help="Download homr's models, then stop")
    parser.add_argument("--stop-when-idle", type=float, metavar="MINUTES", help="Stop after this many minutes without scans")
    args = parser.parse_args()
    # Anything else printed (homr's output, a traceback) mustn't stop it either.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    reader = Reader(cpu=args.cpu, verbose=args.verbose)
    if args.init:
        reader.load()
        sys.exit(1 if reader.failed else 0)
    jobs = Jobs(reader)
    Handler.jobs = jobs
    Handler.reader = reader
    Handler.allowed_hosts = {f"{h}:{args.port}" for h in ("127.0.0.1", "localhost", "[::1]")}

    try:
        httpd = Server(("127.0.0.1", args.port), Handler)
    except OSError as e:
        log(f"Port {args.port} is taken ({e.strerror}). Is the scanner already running? Otherwise use --port.")
        sys.exit(1)
    threading.Thread(target=reader.load, daemon=True).start()
    threading.Thread(target=jobs.work, daemon=True).start()
    threading.Thread(target=jobs.tidy, daemon=True).start()
    if args.stop_when_idle:
        threading.Thread(target=jobs.stop_when_idle, args=(args.stop_when_idle, httpd), daemon=True).start()
    log(f"The song scanner listens on http://127.0.0.1:{args.port} (Ctrl+C stops it)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("Stopped")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
