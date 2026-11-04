> This is the song scanner of [Pianomi](https://pianomi.app): [homr](https://github.com/liebharc/homr) with our additions, as it runs online for Pianomi's app and website. It's licensed under the AGPL-3.0, as homr is. Paths like `src/…` and `docs/…` refer to Pianomi's own repository, which isn't public.

# The song scanner (homr)

Reads scanned PDFs and photos of sheet music for free, on this computer, and turns them into the MusicXML the app stores and plays. It's [homr](https://github.com/liebharc/homr), an open-source music recognition program, behind a small local web service (`server.py`). The app talks to it through the dev server at `/homr`.

## Set up (once)

Needs Python 3.12 (`py -3.12` on Windows) and git.

```
npm run homr:setup
```

This makes a Python environment in `tools/homr/.venv` (not in git), installs homr from GitHub at a fixed commit (see `python.mjs`), and downloads its models (about 300 MB). With an NVIDIA graphics card it installs the GPU version (onnxruntime-gpu with CUDA 13 and cuDNN from pip, about 2.4 GB in all); `npm run homr:setup -- --cpu` skips the GPU (a few hundred MB). No CUDA toolkit is needed, only a recent NVIDIA driver.

On the owner's laptop (RTX 4060, Windows 11) the GPU works. Over the 244 bench pages a page took 10 seconds on average (median 9, slowest 30, the first page after starting); a dense page that takes 18 seconds on the GPU takes 24 on the CPU alone. Most of the time goes into finding the staves, which runs on the processor either way.

## Run

```
npm run homr
```

You don't have to: when you scan a PDF or photo and the scanner isn't running, the dev server starts it (`launch.ts`; the app calls `POST /homr-launch`). It runs in the background, its messages in the dev server's window marked `[scanner]`, and it stops after 20 minutes without scans (`--stop-when-idle 20`; a finished scan nobody collected keeps it up to 2 hours) or when the dev server stops. A scanner already running is left alone.

Started by hand, it keeps running until Ctrl+C. It listens on `http://127.0.0.1:8771` (only this computer). Options after `--`: `--cpu`, `--port 8772` (then set the `HOMR_PORT` environment variable to 8772 for the dev server too), `--verbose` (homr's own output and every request).

Then in the app: Songs › Add a song, and choose a PDF or photos of the pages. When the scanner answers, the scan is free and runs here; Flat stays available for anyone with a Flat token (Settings › Scanning sheet music). It works from a phone too, with `npm run dev:phone`: the phone's page calls `/homr` on the laptop's dev server, which passes it on.

## What it does

- `POST /scan` takes one PDF, or page images in order (multipart form data), and answers with a scan id. PDF pages are drawn at 300 dpi with pypdfium2; photos are turned upright from their EXIF orientation.
- One worker reads the pages one at a time with homr (its models stay loaded between pages), reporting each step.
- `lines.py` takes a second look at each page. homr finds the lines of music before reading them, and now and then misses one or finds only its right-hand part (then the clef, key and notes before its box are never read). The staves are also found plainly (five long, evenly spaced lines; two staves joined at their left end by the opening barline are one line) and compared with the lines homr read. A line homr missed, found in part or without its bottom staff is cut out and read on its own, squeezed to 60% width when its bars don't add up (a line holding one wide bar reads better narrower), and takes the place of what homr read there. Its own time signature is dropped (homr makes one up for a line alone) and the page's key and time are said again after it. Pages where the staves can't be found this way (photos), or whose lines don't match homr's, stay as homr read them. On Nuvole Bianche it read 7 of 51 lines again, about 2 s each.
- `join.py` joins the pages' bars into one piano part on two staves, numbered through, keeping key, time, clef and divisions only where they change. It also puts right what homr misreads in a way the score gives away:
  - a key that changes for one to three whole lines and then back, with no double barline before it and not the piece's usual key: the notes are spelled again in the key around it (homr sometimes misses a key signature and writes the line in C major);
  - the right hand read in bass clef that way, sitting less than a fourth above the left hand: its notes are moved back up to the treble clef. Other clef changes of that shape can't be told from real ones, so they're only flagged;
  - a voice lasting half as long again as another voice of the bar, in equal notes by threes: triplets homr read as plain eighths (it doesn't see unmarked triplets), made two-thirds as long and marked 3:2;
  - a time signature at a page top that only restates the bar length (homr writes one on every page, 6/4 for 12/8), one that the bars after it don't fill (homr read 4/4 as 6/4), and 24/16 for a 12/8 seen before.
- The scan's status says which bars to check against the sheet music once it's done (`checkBars`): bars changed or flagged above, and bars that don't add up to their time signature, which is where homr has lost notes (a short bar is fine first or last, and at a repeat, an ending or a double barline, where scores print them). The app shows them on the song.
- `GET /scan/<id>` reports progress (it waits up to 20 s for news); `GET /scan/<id>/musicxml` gives the result; `GET /health` says whether homr is ready, on the GPU or CPU.
- A page where homr finds no music is left out. Finished scans are forgotten after a day.
- Scans must carry an `X-Fluent-Keys` header and a local `Host`, so other websites open in the browser can't use it. The dev server's `/homr-launch` likewise needs the header and the app's own `Origin`.
- `--stop-when-idle MINUTES` stops it after that long with no scan waiting, being read or polled (`/health` doesn't count).

## Online, for everyone

The same scanner reads scans and photos for everyone who uses Pianomi: `online.py` puts it behind a small web server answering the same requests (so the app's client works with either), and `cloud.py` runs that on [Modal](https://modal.com), in the EU, on the CPU. The plan and the decisions behind it are in `docs/scanning.md`.

```
npm run scanner:login      once: signs this computer in to Modal (it opens the browser)
npm run scanner:deploy     builds and deploys it, and prints its address (https://…modal.run)
npm run scanner:online     the same on http://127.0.0.1:8772, for testing (homr on the CPU)
```

The address goes in `ONLINE_SCANNER` in `src/songs/readers/online.ts`; until then the app and the site say scans can't be read yet. To try the test copy from a dev server, set `localStorage['fk:scannerUrl']` to `http://127.0.0.1:8772` there.

What it adds to the laptop's scanner:

- Only pianomi.app, its Vercel previews and local dev servers may send it scans (CORS, and the `Origin` of every scan), with the `X-Fluent-Keys` header.
- Up to 16 pages and 40 MB a scan; 40 pages an hour for each visitor and 1,500 pages a day for everyone (`SCANNER_MAX_PAGES`, `SCANNER_MAX_MB`, `SCANNER_PAGES_PER_HOUR`, `SCANNER_PAGES_PER_DAY`). A visitor is known by their IP hashed with a salt and the day, never by the IP itself. The totals and the salt are kept in a Modal Dict (`pianomi-scanner-limits`).
- One container reads the scans one at a time, as here. It starts with the first request (homr loads in about half a minute; the scan waits meanwhile) and stops 10 minutes after the last one, which covers a scan of 16 pages even when nobody is watching. Pages are deleted as soon as they're read, and a finished piece waits for its app a day at most (or until the container stops).
- iPhone photos (HEIC) are read too (pillow-heif).
- The app and the site ask each time before sending anything ("Send my pages to be read") and link this folder's source code, as homr's licence asks.

Cost: Modal bills the container's 2 cores and 6 GB while it runs, about $0.02–0.05 for each time it wakes (a scan and the 10 minutes after), well within the free plan's $30 a month at today's numbers. Set a spending limit in Modal's settings anyway.

## Check it

```
tools/homr/.venv/Scripts/python -m unittest discover -s tools/homr
tools/homr/.venv/Scripts/python tools/homr/smoke_test.py [page.png | score.pdf]
```

The first tests the page joining, the second look at lines (`test_lines.py`, which needs numpy and Pillow, so homr's environment), upload reading and the online scanner's guards (`test_online.py`, which needs fastapi and httpx); none needs homr itself. The second checks a running scanner (or starts one): health, the guards, and one real scan. `npm run bench` scores homr on the bench pieces (readers `homr-pages` and `homr-photos`) when the scanner runs.

## Licence

homr is licensed under **AGPL-3.0**, so `server.py`, `online.py`, `cloud.py`, `join.py`, `lines.py` and the tests here, which use it, are AGPL-3.0 too. It runs as a separate program that the app only talks to over HTTP; none of its code is in `src/`, and it must stay that way. Since it's offered to everyone online, this folder's source is public, and the app and the site link it wherever a scan is asked for.

homr's **model weights** were trained partly on data with a **non-commercial** licence (Camera-PrIMuS, CC BY-NC 4.0, in every transformer checkpoint so far), and whether that carries over to the weights is an open legal question. See homr's [LICENSE_FAQ.md](https://github.com/liebharc/homr/blob/main/LICENSE_FAQ.md). Fine for personal use; check again before anything commercial. Title detection uses RapidOCR and its own models.
