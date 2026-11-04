# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Checks the song scanner from the outside: health, the guards, and one real
scan. Uses the scanner that's running (npm run homr), or starts one on a
spare port and stops it afterwards.

    tools/homr/.venv/Scripts/python tools/homr/smoke_test.py [page.png | score.pdf ...]

Without files it scans bench/corpus/fixtures/ode-to-joy/page-1.png (from
npm run bench:corpus) when that's there.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DEFAULT_FILE = os.path.join(ROOT, "bench", "corpus", "fixtures", "ode-to-joy", "page-1.png")


def request(base: str, method: str, path: str, body: bytes | None = None, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(base + path, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=90) as res:  # noqa: S310 (our own scanner)
            return res.status, res.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def form(files: list[str]) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for path in files:
        kind = "application/pdf" if path.lower().endswith(".pdf") else "image/png" if path.lower().endswith(".png") else "image/jpeg"
        with open(path, "rb") as f:
            data = f.read()
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{os.path.basename(path)}"\r\n'
            f"Content-Type: {kind}\r\n\r\n".encode() + data + b"\r\n"
        )
    return b"".join(parts) + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def check(ok: bool, what: str) -> None:
    print(("  ok    " if ok else "  FAIL  ") + what)
    if not ok:
        sys.exit(1)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> None:
    files = [f for f in sys.argv[1:] if not f.startswith("--")]
    if not files and os.path.exists(DEFAULT_FILE):
        files = [DEFAULT_FILE]

    port = int(os.environ.get("HOMR_PORT") or 8771)
    base = f"http://127.0.0.1:{port}"
    server = None
    try:
        request(base, "GET", "/health")
        print(f"Using the scanner on port {port}")
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        print(f"Starting a scanner on port {port}")
        server = subprocess.Popen([sys.executable, os.path.join(HERE, "server.py"), "--port", str(port)])  # noqa: S603
    try:
        for _ in range(240):
            try:
                status, body = request(base, "GET", "/health")
                health = json.loads(body)
                if health.get("ready") or not health.get("ok", True):
                    break
            except (urllib.error.URLError, ConnectionError, json.JSONDecodeError):
                pass
            time.sleep(0.5)
        print("Health")
        check(status == 200 and health.get("ok") and health.get("ready"), f"ready on the {health.get('device')}, homr {health.get('homr')}")

        print("Guards")
        status, _ = request(base, "POST", "/scan", b"x", {"Content-Type": "image/png"})
        check(status == 403, "a scan without the app's header is refused")
        status, _ = request(base, "GET", "/health", headers={"Host": "evil.example:80"})
        check(status == 403, "another host name is refused")
        status, body = request(base, "POST", "/scan", b"not a picture", {"Content-Type": "image/png", "X-Fluent-Keys": "1"})
        check(status == 415 and json.loads(body)["code"] == "UNREADABLE_IMAGE", "a file that isn't a picture is explained")
        status, body = request(base, "GET", "/scan/nope")
        check(status == 404 and json.loads(body)["code"] == "NOT_FOUND", "an unknown scan is explained")

        if not files:
            print("No file to scan (give one, or run npm run bench:corpus); skipped the scan.")
            return
        print(f"Scanning {', '.join(os.path.basename(f) for f in files)}")
        body, ctype = form(files)
        start = time.time()
        status, answer = request(base, "POST", "/scan", body, {"Content-Type": ctype, "X-Fluent-Keys": "1"})
        check(status == 202, f"accepted ({answer.decode()[:80]})")
        job = json.loads(answer)
        version = -1
        steps = []
        while True:
            status, answer = request(base, "GET", f"/scan/{job['id']}?after={version}&wait=20")
            view = json.loads(answer)
            version = view["version"]
            if view["step"] not in steps:
                steps.append(view["step"])
            if view["status"] in ("done", "error", "cancelled"):
                break
        check(view["status"] == "done", f"read in {time.time() - start:.1f} s through: {' / '.join(steps)}")
        status, xml = request(base, "GET", f"/scan/{job['id']}/musicxml")
        root = ET.fromstring(xml)
        parts = root.findall("part")
        bars = parts[0].findall("measure") if parts else []
        notes = [n for n in root.iter("note") if n.find("pitch") is not None]
        check(len(parts) == 1 and len(bars) > 0 and len(notes) > 0, f"one part, {len(bars)} bars, {len(notes)} notes, title {root.findtext('work/work-title')!r}")
        status, _ = request(base, "DELETE", f"/scan/{job['id']}")
        check(status == 204, "forgotten afterwards")
        print("All good.")
    finally:
        if server:
            server.terminate()


if __name__ == "__main__":
    main()
