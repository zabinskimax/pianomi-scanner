# SPDX-License-Identifier: AGPL-3.0-or-later
"""
A second look at the lines of music homr missed on a page.

homr finds the lines (systems) on a page before it reads them, and now and
then it misses one, or finds only the right-hand part of it: then the notes
before its box are never read, nor the clef and key at the start of the
line. Read on its own, the same line usually comes out right.

So each page's staves are also found here, the plain way (five long, evenly
spaced lines; two staves joined at their left end by the line's opening
barline are one line), and compared with the lines homr read. A line homr
missed, found only part of, or found without all its staves is cut out and
read again on its own (squeezed to 60% width when its bars don't add up,
which helps a line holding a single wide bar), and its bars take the place
of what homr read there.

Only clean pages: where the staves can't be found this way (photos, curved
pages), or homr's lines can't be matched to them, the page stays as homr
read it. Needs numpy and Pillow (homr's own environment).
"""

from __future__ import annotations

import copy
import os
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction

import numpy as np
from PIL import Image

from join import piano_part

# A staff line is dark across at least this share of the page's width.
LINE_SHARE = 0.35
DARK = 140
# homr's line starting later (or ending earlier) than this share of the line's width: it found only part.
PART_FOUND = 0.1
SQUEEZE = 0.6
# Room around a cut-out line, and around the first or last one on the page, in staff heights.
PAGE_EDGE = 1.5


@dataclass
class Box:
    """A staff or a line, as fractions of the page's width and height."""

    x0: float
    y0: float
    x1: float
    y1: float
    # Where barlines were found inside it (homr's lines only).
    barlines: list[float] = field(default_factory=list)


@dataclass
class Line:
    staves: list[Box]

    @property
    def box(self) -> Box:
        return Box(min(s.x0 for s in self.staves), self.staves[0].y0, max(s.x1 for s in self.staves), self.staves[-1].y1)


def find_lines(dark: np.ndarray) -> list[Line]:
    """The page's lines of music, top to bottom, from a dark-pixel mask."""
    h, w = dark.shape
    rows = dark.sum(1)
    marks: list[list[int]] = []
    for y in np.flatnonzero(rows > LINE_SHARE * w):
        if marks and y - marks[-1][-1] <= 2:
            marks[-1].append(int(y))
        else:
            marks.append([int(y)])
    ys = [sum(m) / len(m) for m in marks]

    staves: list[tuple[float, float, float, float]] = []
    i = 0
    while i + 4 < len(ys):
        gaps = np.diff(ys[i : i + 5])
        if gaps.max() - gaps.min() <= max(2.0, 0.25 * gaps.mean()) and gaps.mean() > 3:
            middle = int(round(ys[i + 2]))
            xs = np.flatnonzero(dark[max(0, middle - 1) : middle + 2].any(0))
            # The longest dark stretch along the middle line is the staff.
            runs, start = [], xs[0]
            for a, b in zip(xs, xs[1:]):
                if b - a > 3:
                    runs.append((start, a))
                    start = b
            runs.append((start, xs[-1]))
            x0, x1 = max(runs, key=lambda r: r[1] - r[0])
            staves.append((float(x0), ys[i], float(x1), ys[i + 4]))
            i += 5
        else:
            i += 1

    lines: list[list[tuple[float, float, float, float]]] = []
    for staff in staves:
        if lines and _joined(dark, lines[-1][-1], staff):
            lines[-1].append(staff)
        else:
            lines.append([staff])
    return [Line([Box(x0 / w, y0 / h, x1 / w, y1 / h) for x0, y0, x1, y1 in line]) for line in lines]


def _joined(dark: np.ndarray, upper: tuple[float, float, float, float], lower: tuple[float, float, float, float]) -> bool:
    """The line's opening barline (or brace) runs from one staff down to the next at their left end."""
    x = int(min(upper[0], lower[0]))
    y0, y1 = int(upper[3]) + 2, int(lower[1]) - 2
    if y1 <= y0:
        return True
    return bool(dark[y0:y1, max(0, x - 40) : x + 6].any(1).mean() > 0.9)


def bad_lines(lines: list[Line], read: list[Box]) -> list[int] | None:
    """
    The lines homr missed, found in part, or found without all their staves,
    by index. `read` is each line homr read (its staves' extent). None when
    homr's lines don't match these.
    """
    found: list[list[int]] = [[] for _ in lines]
    for k, box in enumerate(read):
        middle = (box.y0 + box.y1) / 2
        home = [i for i, line in enumerate(lines) if line.box.y0 - 0.01 <= middle <= line.box.y1 + 0.01]
        if len(home) != 1:
            return None
        found[home[0]].append(k)
    order = [k for ks in found for k in ks]
    if order != sorted(order):
        return None
    bad = []
    for i, (line, ks) in enumerate(zip(lines, found)):
        width = line.box.x1 - line.box.x0
        staff = (line.staves[0].y1 - line.staves[0].y0)
        if len(ks) != 1:
            bad.append(i)
            continue
        box = read[ks[0]]
        part = box.x0 > line.box.x0 + PART_FOUND * width or box.x1 < line.box.x1 - PART_FOUND * width
        # homr may keep a grand staff as one staff: what counts is that it reaches from the top staff to the bottom one.
        short = box.y0 > line.box.y0 + staff / 2 or box.y1 < line.box.y1 - staff / 2
        if part or short:
            bad.append(i)
    return bad


def cut_line(page: Image.Image, lines: list[Line], i: int, factor: float = 1.0) -> Image.Image:
    """Line i alone on a white page of the same width, halfway to the lines around it, squeezed by `factor`."""
    w, h = page.size
    box = lines[i].box
    staff = (box.y1 - box.y0) / len(lines[i].staves)
    top = (lines[i - 1].box.y1 + box.y0) / 2 if i > 0 else max(0.0, box.y0 - PAGE_EDGE * staff)
    bottom = (box.y1 + lines[i + 1].box.y0) / 2 if i + 1 < len(lines) else min(1.0, box.y1 + PAGE_EDGE * staff)
    strip = page.crop((0, int(top * h), w, int(bottom * h)))
    if factor != 1.0:
        strip = strip.resize((int(w * factor), strip.height), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (w, strip.height + 600), "white")
    canvas.paste(strip, (0, 300))
    return canvas


# ---------------------------------------------------------------------------
# Bars


def _systems(part: ET.Element) -> list[list[ET.Element]]:
    """A page's bars, line by line (homr starts each line after the first with a new-system print)."""
    out: list[list[ET.Element]] = []
    for m in part.findall("measure"):
        if not out or m.find("print[@new-system='yes']") is not None:
            out.append([])
        out[-1].append(m)
    return out


def _state(measures: list[ET.Element]) -> dict[str, ET.Element]:
    """The divisions, key, time and clefs in force after these bars, as their elements."""
    state: dict[str, ET.Element] = {}
    for m in measures:
        for attrs in m.iter("attributes"):
            for c in attrs:
                if c.tag in ("divisions", "key", "time"):
                    state[c.tag] = c
                elif c.tag == "clef":
                    state[f"clef{c.get('number', '1')}"] = c
    return state


def doubts(xml: str, time: tuple[int, int] | None) -> int:
    """How doubtful a line's reading is: its bars that don't fill `time`, and a right hand read in bass clef."""
    try:
        part = piano_part(ET.fromstring(xml))
    except ET.ParseError:
        return 1000
    if part is None or not part.findall("measure"):
        return 1000
    if part.find(".//divisions") is None:
        return 1000  # its lengths can't be read
    length = Fraction(4 * time[0], time[1]) if time else None
    divisions, count = 1, 0
    for m in part.findall("measure"):
        for attrs in m.iter("attributes"):
            if (attrs.findtext("divisions") or "").strip().isdigit():
                divisions = int(attrs.findtext("divisions"))
            for c in attrs.findall("clef"):
                if c.get("number", "1") == "1" and (c.findtext("sign") or "").strip() == "F":
                    count += 2
        pos = longest = 0
        for el in m:
            if el.tag == "backup":
                pos -= int(el.findtext("duration") or 0)
            elif el.tag == "forward" or (el.tag == "note" and el.find("chord") is None and el.find("grace") is None):
                pos += int(el.findtext("duration") or 0)
                longest = max(longest, pos)
        if length is not None and Fraction(longest, divisions) != length:
            count += 1
    return count


def _time_of(state: dict[str, ET.Element]) -> tuple[int, int] | None:
    t = state.get("time")
    if t is None or not (t.findtext("beats") or "").strip().isdigit() or not (t.findtext("beat-type") or "").strip().isdigit():
        return None
    return (int(t.findtext("beats")), int(t.findtext("beat-type")))


def splice(page_xml: str, owners: list[int], replacements: dict[int, str], lines: int) -> str:
    """
    The page with some lines read again. `owners` gives the line each of
    homr's lines lies on (in order); `replacements` the new reading of some
    lines. A replaced line drops its own time signature (the page's holds:
    homr makes one up for a line read alone), and the bar after it restates
    what was in force on the page, so the line's misreadings don't carry on.
    """
    root = ET.fromstring(page_xml)
    part = piano_part(root)
    if part is None:
        return page_xml
    systems = _systems(part)
    by_line: dict[int, list[list[ET.Element]]] = {}
    for k, system in enumerate(systems):
        by_line.setdefault(owners[k], []).append(system)

    out: list[ET.Element] = []
    before: list[ET.Element] = []  # the page's own bars so far, for what's in force
    restate = False
    for i in range(lines):
        if i in replacements:
            line_part = piano_part(ET.fromstring(replacements[i]))
            new = [copy.deepcopy(m) for m in line_part.findall("measure")] if line_part is not None else []
            for m in new:
                for p in m.findall("print"):
                    m.remove(p)
                for attrs in m.iter("attributes"):
                    for t in attrs.findall("time"):
                        attrs.remove(t)
            for system in by_line.get(i, []):
                before.extend(system)
            if new:
                # The page's time holds, even where the bars it was stated in are the ones replaced.
                time = _state(before).get("time")
                if time is not None:
                    _restate(new[0], {"time": time})
                if out:
                    new[0].insert(0, ET.Element("print", {"new-system": "yes"}))
            out.extend(new)
            restate = True
            continue
        for system in by_line.get(i, []):
            first = system[0]
            if restate:
                _restate(first, _state(before))
                restate = False
            if out and first.find("print[@new-system='yes']") is None:
                first.insert(0, ET.Element("print", {"new-system": "yes"}))
            out.extend(system)
            before.extend(system)
    for m in part.findall("measure"):
        part.remove(m)
    for n, m in enumerate(out, 1):
        m.set("number", str(n))
        part.append(m)
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def _restate(m: ET.Element, state: dict[str, ET.Element]) -> None:
    """Say again, at the start of bar m, what the page had in force (joining drops what's unchanged)."""
    attrs = next((c for c in m if c.tag == "attributes"), None)
    if attrs is None or any(c.tag in ("note", "backup", "forward") for c in list(m)[: list(m).index(attrs)]):
        attrs = ET.Element("attributes")
        m.insert(0, attrs)
    have = {c.tag + (c.get("number", "1") if c.tag == "clef" else "") for c in attrs}
    for name, el in state.items():
        if name not in have:
            attrs.append(copy.deepcopy(el))


# ---------------------------------------------------------------------------


def second_look(
    page_path: str,
    page_xml: str,
    read_systems: list[list[tuple[float, float, float, float]]],
    read_line: Callable[[str], str],
    work_dir: str,
    progress: Callable[[str], None] = lambda _: None,
    placed: list[Box] | None = None,
    boxes: list[Box] | None = None,
) -> tuple[str, int]:
    """
    The page's reading with the lines homr missed or found in part read again.
    `read_systems` is homr's lines, each its staves as fractions of the page;
    `read_line` reads one image. Gives the reading and how many lines were read again.

    `placed` is where each of homr's lines sits on the page as sent, for the
    page map (`read_systems` are fractions of the image homr cropped, which
    can differ); `boxes` is filled with where each line of the result sits:
    homr's own, and for a line read again the one found here.
    """
    if boxes is not None:
        boxes[:] = placed or []
    page = Image.open(page_path).convert("RGB")
    lines = find_lines(np.asarray(page.convert("L")) < DARK)
    if not lines:
        return page_xml, 0
    read = [Box(min(s[0] for s in sys), min(s[1] for s in sys), max(s[2] for s in sys), max(s[3] for s in sys)) for sys in read_systems]
    try:
        part = piano_part(ET.fromstring(page_xml))
    except ET.ParseError:
        return page_xml, 0
    if part is None or len(_systems(part)) != len(read):
        return page_xml, 0
    bad = bad_lines(lines, read)
    if not bad:
        return page_xml, 0
    owners = [next(i for i, line in enumerate(lines) if line.box.y0 - 0.01 <= (b.y0 + b.y1) / 2 <= line.box.y1 + 0.01) for b in read]

    systems = _systems(part)
    replacements: dict[int, str] = {}
    progress(f"Reading {len(bad)} {'line' if len(bad) == 1 else 'lines'} again")
    for i in bad:
        # Judge a line by the time signature in force on the page there.
        earlier = [m for k, system in enumerate(systems) if owners[k] < i for m in system]
        time = _time_of(_state(earlier)) or _time_of(_state([m for s in systems for m in s]))
        best: tuple[int, str] | None = None
        for factor in (1.0, SQUEEZE):
            path = os.path.join(work_dir, f"{os.path.splitext(os.path.basename(page_path))[0]}-line-{i + 1}-{int(factor * 100)}.png")
            cut_line(page, lines, i, factor).save(path)
            try:
                xml = read_line(path)
            except Exception:  # noqa: BLE001 (a line homr can't read keeps what the page had)
                continue
            score = doubts(xml, time)
            if best is None or score < best[0]:
                best = (score, xml)
            if score == 0:
                break
        if best is not None and best[0] < 1000:
            replacements[i] = best[1]
    if not replacements:
        return page_xml, 0
    if boxes is not None and placed is not None and len(placed) == len(read):
        boxes[:] = [
            box
            for i in range(len(lines))
            for box in ([lines[i].box] if i in replacements else [placed[k] for k in range(len(read)) if owners[k] == i])
        ]
    return splice(page_xml, owners, replacements, len(lines)), len(replacements)
