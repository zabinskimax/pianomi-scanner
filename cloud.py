# SPDX-License-Identifier: AGPL-3.0-or-later
"""
The online song scanner (online.py) on Modal, in the EU, on the CPU:

    npm run scanner:deploy        (tools/homr/.venv/Scripts/python -m modal deploy tools/homr/cloud.py)

One container reads the scans one at a time, as the laptop's scanner does,
so every request about a scan reaches the container that holds it. It
starts with the first request (homr loads in about half a minute, the scan
waiting meanwhile) and stops after SCALEDOWN seconds without one, which
covers a scan of the most pages even when nobody is watching it.

The totals behind the limits are kept in a Modal Dict (pianomi-scanner-limits),
so a container starting again doesn't forget them, and so is the salt the
visitors' IPs are hashed with (made on the first start).

This file uses homr through online.py, so it is AGPL-3.0 too.
"""

from __future__ import annotations

import os
import re
import sys

import modal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Seconds without a request before the container stops: a scan of MAX_PAGES at about 35 s a page fits.
SCALEDOWN = 600
REGION = "eu"


def homr_commit() -> str:
    """The same homr as the laptop's (python.mjs), read when deploying."""
    with open(os.path.join(HERE, "python.mjs"), encoding="utf-8") as f:
        return re.search(r"HOMR_COMMIT = '([0-9a-f]{40})'", f.read()).group(1)  # type: ignore[union-attr]


def download_models() -> None:
    """homr's and the title reader's models, into the image, so a container starting doesn't fetch them."""
    from server import Reader

    reader = Reader(cpu=True, verbose=False)
    reader.load()
    if reader.failed:
        raise RuntimeError(reader.failed)


app = modal.App("pianomi-scanner")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("git")
    .pip_install(
        f"homr[cpu] @ git+https://github.com/liebharc/homr@{homr_commit() if modal.is_local() else ''}",
        "fastapi>=0.115,<1",
        "pillow-heif>=0.18,<2",
    )
    .add_local_python_source("server", "join", "lines", "online", copy=True)
    .run_function(download_models)
)

limits = modal.Dict.from_name("pianomi-scanner-limits", create_if_missing=True)


class DictCounter:
    """online.Counter over the Modal Dict. Two scans at the same moment may both count from the same total: a page or two either way."""

    def get(self, key: str) -> int:
        return int(limits.get(key, 0))

    def add(self, key: str, n: int) -> int:
        total = self.get(key) + n
        limits[key] = total
        return total


@app.cls(
    image=image,
    cpu=2.0,
    memory=6144,
    region=REGION,
    # Requests come in through Dublin, not Virginia (Modal's default). Bodies over 2 MiB still pass through its
    # storage in us-east (modal.com/docs/guide/region-selection); the pages are read in the EU either way.
    routing_region="eu-west",
    max_containers=1,
    scaledown_window=SCALEDOWN,
    timeout=300,
)
@modal.concurrent(max_inputs=100)
class Scanner:
    @modal.enter()
    def start(self) -> None:
        import secrets

        from online import Online

        salt = limits.get("salt")
        if not salt:
            salt = secrets.token_hex(16)
            limits["salt"] = salt
        self.online = Online(cpu=True, counter=DictCounter(), salt=salt)
        self.online.start()

    @modal.asgi_app()
    def web(self):  # type: ignore[no-untyped-def]
        return self.online.app
