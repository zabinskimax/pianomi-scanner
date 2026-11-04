# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Joins the MusicXML homr makes of each page into one score: one piano part
on one or two staves, its bars numbered through. A page's key, time
signature, clefs and divisions are kept only where they change, so the
page turns don't show up as new signatures, and the empty bar homr writes
at the end of a first or second ending is folded into the bar before it.

It also puts right what homr misreads in a way the score itself gives away:
- a key that changes for a line or three and then changes back, with no
  double barline before it, is a misreading: the notes are spelled again in
  the key around it. So is the right hand read in bass clef that way, when
  it then sits down among the left hand's notes: they're moved back up.
  Other clef changes of that shape are only flagged;
- a voice lasting half as long again as the bar's other voices, in equal
  notes by threes, is triplets homr read as plain notes, and so is a bar whose
  notes are mostly of one short length, in threes, reaching half as far again
  as a voice of longer notes lasts: the bar's notes go back to two thirds of
  where homr put them;
- a time signature homr writes at the top of a page that only restates the
  bar length, or that the bars after it don't fill, is replaced by the one
  they do fill.
The bars it changed, and bars that don't add up to their time signature,
come back as the bars to check.
Standard library only, so it can be tested without homr.
"""

from __future__ import annotations

import copy
import statistics
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from fractions import Fraction

# The order MusicXML wants inside <attributes>.
ATTRIBUTE_ORDER = [
    "footnote",
    "level",
    "divisions",
    "key",
    "time",
    "staves",
    "part-symbol",
    "instruments",
    "clef",
    "staff-details",
    "transpose",
    "for-part",
    "directive",
    "measure-style",
]
# The order MusicXML wants inside <barline>.
BARLINE_ORDER = ["bar-style", "footnote", "level", "wavy-line", "segno", "coda", "fermata", "ending", "repeat"]


LETTERS = "CDEFGAB"
SHARP_ORDER = "FCGDAEB"
# The line a clef's bottom staff line stands for, in steps from C0 (C4 = 28).
CLEF_BOTTOM = {("G", "2"): 4 * 7 + 2, ("F", "4"): 2 * 7 + 4}
# A misread key or clef lasts at most this many lines.
MISREAD_LINES = 3
# A right hand less than a fourth above the left (middle notes, in steps) was read on the wrong clef.
HANDS_APART = 3
DOUBLE_BARS = ("light-light", "light-heavy", "heavy-heavy", "heavy-light")


class JoinError(Exception):
    """None of the pages had any bars in them."""


@dataclass
class Joined:
    xml: str
    # Bar numbers to check against the sheet music: changed here, or not adding up.
    check_bars: list[str] = field(default_factory=list)


@dataclass
class Bar:
    el: ET.Element
    page_start: bool = False
    line_start: bool = False


def _staves(part: ET.Element) -> int:
    counts = [int(s.text) for s in part.iter("staves") if (s.text or "").strip().isdigit()]
    staffs = [int(s.text) for s in part.iter("staff") if (s.text or "").strip().isdigit()]
    return max(counts + staffs + [1])


def _notes(part: ET.Element) -> int:
    return sum(1 for n in part.iter("note") if n.find("pitch") is not None)


def piano_part(root: ET.Element) -> ET.Element | None:
    """The part to keep from a page: two staves first, then the most notes."""
    parts = [p for p in root.findall("part") if p.find("measure") is not None]
    if not parts:
        return None
    grand = [p for p in parts if _staves(p) >= 2]
    return max(grand or parts, key=_notes)


def _same(a: ET.Element, b: ET.Element | None) -> bool:
    """Same element, ignoring layout attributes like print-object."""
    if b is None:
        return False
    flat = lambda e: [(c.tag, (c.text or "").strip()) for c in e.iter()]  # noqa: E731
    return flat(a) == flat(b)


def _sort(parent: ET.Element, order: list[str]) -> None:
    rank = {tag: i for i, tag in enumerate(order)}
    children = list(parent)
    children.sort(key=lambda c: rank.get(c.tag, len(rank)))
    for c in list(parent):
        parent.remove(c)
    parent.extend(children)


def _sort_attributes(attrs: ET.Element) -> None:
    _sort(attrs, ATTRIBUTE_ORDER)


def fold_empty_bars(bars: list[Bar]) -> list[Bar]:
    """
    homr closes a first or second ending with a bar of its own holding nothing
    but that barline, which would play as a bar of silence. Its barline
    belongs at the end of the bar before.
    """
    out: list[Bar] = []
    page_start = line_start = False  # carried over from a bar folded away
    for bar in bars:
        m = bar.el
        if out and m.find("note") is None and len(m) > 0 and all(c.tag in ("barline", "print") for c in m):
            prev = out[-1].el
            right = next((b for b in prev.findall("barline") if b.get("location", "right") == "right"), None)
            if right is None:
                right = ET.SubElement(prev, "barline", location="right")
            for barline in m.findall("barline"):
                for c in barline:
                    if c.tag == "ending" or right.find(c.tag) is None:
                        right.append(c)
            _sort(right, BARLINE_ORDER)
            # A page or line starting with it starts with the next bar.
            page_start |= bar.page_start
            line_start |= bar.line_start or m.find("print[@new-system='yes']") is not None
            continue
        bar.page_start |= page_start
        bar.line_start |= line_start
        page_start = line_start = False
        out.append(bar)
    return out


# ---------------------------------------------------------------------------
# What each bar says and holds


@dataclass
class State:
    """The signatures in force in a bar (stated there or carried from before)."""

    divisions: int
    fifths: int
    time: tuple[int, int] | None
    clefs: dict[str, tuple[str, str]]


def _clef_of(clef: ET.Element) -> tuple[str, str]:
    return ((clef.findtext("sign") or "").strip(), (clef.findtext("line") or "").strip())


def _states(bars: list[Bar]) -> list[State]:
    out: list[State] = []
    state = State(1, 0, None, {})
    for bar in bars:
        state = State(state.divisions, state.fifths, state.time, dict(state.clefs))
        for attrs in bar.el.iter("attributes"):
            if (attrs.findtext("divisions") or "").strip().isdigit():
                state.divisions = int(attrs.findtext("divisions"))
            fifths = (attrs.findtext("key/fifths") or "").strip()
            if fifths.lstrip("-").isdigit():
                state.fifths = int(fifths)
            time = attrs.find("time")
            if time is not None and (time.findtext("beats") or "").strip().isdigit() and (time.findtext("beat-type") or "").strip().isdigit():
                state.time = (int(time.findtext("beats")), int(time.findtext("beat-type")))
            for clef in attrs.findall("clef"):
                state.clefs[clef.get("number", "1")] = _clef_of(clef)
        out.append(state)
    return out


def _filled(m: ET.Element) -> int:
    """How long the bar's music lasts, in its divisions: the furthest any voice reaches."""
    pos = longest = 0
    for el in m:
        if el.tag == "backup":
            pos -= int(el.findtext("duration") or 0)
        elif el.tag == "forward":
            pos += int(el.findtext("duration") or 0)
            longest = max(longest, pos)
        elif el.tag == "note" and el.find("chord") is None and el.find("grace") is None:
            pos += int(el.findtext("duration") or 0)
            longest = max(longest, pos)
    return longest


def _length(time: tuple[int, int] | None) -> Fraction | None:
    """A time signature's bar length in quarter notes."""
    return Fraction(4 * time[0], time[1]) if time else None


def _alter(fifths: int, letter: str) -> int:
    """What a key signature does to a letter: -1, 0 or 1."""
    if fifths > 0:
        return 1 if letter in SHARP_ORDER[:fifths] else 0
    if fifths < 0:
        return -1 if letter in SHARP_ORDER[::-1][: -fifths] else 0
    return 0


def _pitches(bars: list[Bar], indexes: range, staff: str) -> list[int]:
    """The notes of one staff in some bars, in steps from C0."""
    steps = []
    for i in indexes:
        for n in bars[i].el.iter("note"):
            p = n.find("pitch")
            if p is not None and n.findtext("staff", "1").strip() == staff and n.find("grace") is None:
                steps.append(int(p.findtext("octave") or 4) * 7 + LETTERS.index((p.findtext("step") or "C").strip()))
    return steps


def _set_pitch(pitch: ET.Element, step: int, alter: int) -> None:
    for tag in ("step", "alter", "octave"):
        for el in pitch.findall(tag):
            pitch.remove(el)
    ET.SubElement(pitch, "step").text = LETTERS[step % 7]
    if alter:
        ET.SubElement(pitch, "alter").text = str(alter)
    ET.SubElement(pitch, "octave").text = str(step // 7)


def _double_bar_before(bars: list[Bar], i: int) -> bool:
    """A double barline (or a repeat) ends the bar before bar i, or opens bar i."""
    styles = [(b.findtext("bar-style") or "").strip() for b in bars[i - 1].el.findall("barline") if b.get("location", "right") == "right"]
    opening = [b for b in bars[i].el.findall("barline") if b.get("location") == "left"]
    return any(s in DOUBLE_BARS for s in styles) or any(b.find("repeat") is not None or (b.findtext("bar-style") or "").strip() in DOUBLE_BARS for b in opening)


def _runs(values: list) -> list[tuple[int, int]]:
    """Stretches of equal values, as (first, last) indexes."""
    runs: list[tuple[int, int]] = []
    for i, v in enumerate(values):
        if runs and values[runs[-1][0]] == v:
            runs[-1] = (runs[-1][0], i)
        else:
            runs.append((i, i))
    return runs


def _misread_runs(bars: list[Bar], values: list) -> list[tuple[int, int, object]]:
    """
    Stretches whose value differs from the piece's usual one for a whole line
    or three, with the same value before and after and no double barline
    where it starts: homr misread it. Each comes with the value it should have.
    (Only the usual value counts as right, or a line read right between two
    misread ones would look misread too.)
    """
    lines = [i for i, b in enumerate(bars) if b.line_start or i == 0]
    usual = Counter(values).most_common(1)[0][0]
    out = []
    runs = _runs(values)
    for k in range(1, len(runs) - 1):
        first, last = runs[k]
        before, after = values[runs[k - 1][1]], values[runs[k + 1][0]]
        whole_lines = first in lines and last + 1 in lines
        count = sum(1 for i in lines if first <= i <= last)
        if values[first] != usual and before == after and whole_lines and count <= MISREAD_LINES and not _double_bar_before(bars, first):
            out.append((first, last, before))
    return out


# ---------------------------------------------------------------------------
# A. Keys and clefs


def fix_keys_and_clefs(bars: list[Bar], staves: int) -> tuple[set[int], set[int]]:
    """
    Put right keys and clefs misread for a line or three. Gives the bars
    changed, and the bars of clef changes that look misread but can't be told
    from real ones (left as they are, to check).
    """
    states = _states(bars)
    true_key = [s.fifths for s in states]
    key_fixed: set[int] = set()
    for first, last, key in _misread_runs(bars, [s.fifths for s in states]):
        for i in range(first, last + 1):
            true_key[i] = key  # type: ignore[assignment]
            key_fixed.add(i)

    # The right hand read in bass clef sits down among the left hand's notes; in a real
    # bass-clef passage it stays above them. Any other clef change of that shape can't
    # be told from a real one: a left hand misread in treble clef sits under the right
    # hand just as a real treble passage does.
    moved: dict[tuple[int, str], int] = {}  # (bar, staff): steps to move its notes
    unsure: set[int] = set()
    for staff in ("1", "2")[:staves]:
        for first, last, clef in _misread_runs(bars, [s.clefs.get(staff) for s in states]):
            read = states[first].clefs.get(staff)
            stretch = range(first, last + 1)
            right, left = _pitches(bars, stretch, "1"), _pitches(bars, stretch, "2")
            if (staff, clef, read) == ("1", ("G", "2"), ("F", "4")) and right and left and statistics.median(right) - statistics.median(left) < HANDS_APART:
                for i in stretch:
                    moved[(i, staff)] = CLEF_BOTTOM[clef] - CLEF_BOTTOM[read]
            else:
                unsure.update(stretch)

    changed = key_fixed | {i for i, _ in moved}
    for i in sorted(changed):
        m = bars[i].el
        for attrs in m.iter("attributes"):
            for c in list(attrs):
                if (c.tag == "key" and i in key_fixed) or (c.tag == "clef" and (i, c.get("number", "1")) in moved):
                    attrs.remove(c)
        for n in m.iter("note"):
            p = n.find("pitch")
            if p is None:
                continue
            letter = (p.findtext("step") or "C").strip()
            step = int(p.findtext("octave") or 4) * 7 + LETTERS.index(letter)
            alter = int(p.findtext("alter") or 0)
            shift = moved.get((i, n.findtext("staff", "1").strip()))
            if shift is not None:
                # Read on the wrong clef, the accidental was guessed for another note: the key's is the best there is.
                step += shift
                _set_pitch(p, step, _alter(true_key[i], LETTERS[step % 7]))
            elif i in key_fixed and alter == _alter(states[i].fifths, letter):
                _set_pitch(p, step, _alter(true_key[i], letter))
    # The bar after a fixed stretch restates what's in force again; joining drops that.
    return changed, unsure - changed


# ---------------------------------------------------------------------------
# Triplets


def _timed(m: ET.Element) -> list[tuple[int, ET.Element]]:
    """Each note of the bar with where it starts, in the bar's divisions."""
    out: list[tuple[int, ET.Element]] = []
    pos = last = 0
    for el in m:
        if el.tag == "backup":
            pos -= int(el.findtext("duration") or 0)
        elif el.tag == "forward":
            pos += int(el.findtext("duration") or 0)
        elif el.tag == "note":
            at = last if el.find("chord") is not None else pos
            out.append((at, el))
            if el.find("chord") is None and el.find("grace") is None:
                last, pos = pos, pos + int(el.findtext("duration") or 0)
    return out


def _voice(n: ET.Element) -> tuple[str, str]:
    return (n.findtext("staff", "1").strip(), n.findtext("voice", "1").strip())


def fix_triplets(bars: list[Bar]) -> set[int]:
    """
    homr reads unmarked triplets as plain notes, so their voice lasts half as
    long again as the bar's other voices. A voice like that, of equal notes in
    threes, is made triplets. Gives the bars changed.

    Only a voice that runs without gaps (notes and rests from the start) says
    how long the bar is: a hand whose notes are a few of a run shared with the
    other hand stops early without the run being triplets.

    homr also puts the bar's other notes where they stand under the plain
    notes it misread, half as far again into the bar, so they're moved back
    (`_squeeze`). And it shares a run of triplets out among voices of both
    staves (the Moonlight Sonata): then every note of the run's length is
    made triplets (`_triplet_bar`).
    """
    found: dict[int, tuple[int, int, set[tuple[str, str]] | None]] = {}
    for i, bar in enumerate(bars):
        spans: dict[tuple[str, str], int] = {}
        played: dict[tuple[str, str], int] = {}
        lengths: dict[tuple[str, str], list[int]] = {}
        for at, n in _timed(bar.el):
            if n.find("grace") is not None:
                continue
            key, dur = _voice(n), int(n.findtext("duration") or 0)
            spans[key] = max(spans.get(key, 0), at + dur)
            if n.find("chord") is None:
                played[key] = played.get(key, 0) + dur
            if n.find("chord") is None and n.find("rest") is None:
                lengths.setdefault(key, []).append(dur)
        voices: set[tuple[str, str]] = set()
        length = short = 0
        for key, span in spans.items():
            # Against any other voice: homr's other voices don't always fill the bar either.
            others = {s for k, s in spans.items() if k != key and s and played.get(k) == s}
            durs = lengths.get(key, [])
            if any(2 * span == 3 * s for s in others) and durs and len(set(durs)) == 1 and len(durs) % 3 == 0:
                voices.add(key)
                length, short = max(length, 2 * span // 3), durs[0]
        if voices:
            found[i] = (length, short, voices)
        else:
            whole = _triplet_bar(bar.el)
            if whole:
                found[i] = (*whole, None)
    if not found:
        return set()
    # Three times the divisions everywhere, so two thirds of any length is whole.
    for bar in bars:
        for el in bar.el.iter("divisions"):
            el.text = str(int(el.text) * 3)
        for el in bar.el.iter("duration"):
            el.text = str(int(el.text) * 3)
    for i, (length, short, voices) in found.items():
        _squeeze(bars[i].el, length * 3, short * 3, voices)
    return set(found)


def _steady(notes: list[tuple[int, ET.Element]], scale: Fraction) -> int | None:
    """How long a voice lasts when its notes and rests, placed at `scale` of where they stand, follow one another from the bar's start without gaps."""
    end = 0
    for at, n in notes:
        if n.find("chord") is not None or n.find("grace") is not None:
            continue
        if at * scale != end:
            return None
        end += int(n.findtext("duration") or 0)
    return end or None


def _triplet_bar(m: ET.Element) -> tuple[int, int] | None:
    """
    A bar of triplets homr read as plain notes and spread over several voices:
    its notes are mostly of one short length, in threes, and they reach half
    as far again as the bar lasts. How long the bar lasts is said by a voice
    without that length that runs without gaps, as it stands or (homr having
    placed it under the stretched notes) with its notes at two thirds of where
    they stand: the bar's length and the short length, or None.
    """
    timed = [(at, n) for at, n in _timed(m) if n.find("grace") is None]
    heads = [(at, n) for at, n in timed if n.find("chord") is None]
    durs = [int(n.findtext("duration") or 0) for _, n in heads if n.find("rest") is None]
    if not durs:
        return None
    short, count = Counter(durs).most_common(1)[0]
    if count < 6 or count % 3 or 2 * count < len(durs) or short <= 0:
        return None
    reach = max(at + int(n.findtext("duration") or 0) for at, n in heads)
    by_voice: dict[tuple[str, str], list[tuple[int, ET.Element]]] = {}
    for at, n in timed:
        by_voice.setdefault(_voice(n), []).append((at, n))
    lengths = set()
    for notes in by_voice.values():
        if any(int(n.findtext("duration") or 0) == short for _, n in notes if n.find("chord") is None):
            continue
        for scale in (Fraction(1), Fraction(2, 3)):
            length = _steady(notes, scale)
            # The last of the short notes may be missing (homr leaves out a note now and then).
            if length and 3 * length - 2 * short <= 2 * reach <= 3 * length:
                lengths.add(length)
    return (max(lengths), short) if lengths else None


def _squeeze(m: ET.Element, length: int, short: int, voices: set[tuple[str, str]] | None) -> None:
    """
    The bar written again voice by voice, every note and rest at two thirds of
    where it stands, except in a voice that already runs without gaps from the
    bar's start; the notes of `voices`, and the notes `short` long of a voice
    holding notes of the run, made triplets, on the triplets' beat; the others
    keep their length, up to the barline. (homr puts notes of the run in the
    melody's voice, so a melody note can't end where the next of its voice
    starts.)
    """
    timed = _timed(m)
    first = next(i for i, c in enumerate(m) if c.tag in ("note", "backup", "forward"))
    lead, rest = list(m)[:first], [c for c in list(m)[first:] if c.tag not in ("note", "backup", "forward")]
    by_voice: dict[tuple[str, str], list[tuple[int, int, ET.Element]]] = {}
    for order, (at, n) in enumerate(timed):
        by_voice.setdefault(_voice(n), []).append((at, order, n))
    # A voice whose short notes stand on the misread notes' beat holds notes of the run; plain
    # notes against the triplets (two against three) stand between them as often as on them.
    run = set(voices or ())
    for key, notes in by_voice.items():
        starts = [at for at, _, n in notes if n.find("chord") is None and int(n.findtext("duration") or 0) == short]
        if starts and 2 * sum(1 for at in starts if at % short == 0) > len(starts):
            run.add(key)

    def triplet(key: tuple[str, str], n: ET.Element) -> bool:
        return key in run and (voices is not None and key in voices or int(n.findtext("duration") or 0) == short)

    for c in list(m):
        m.remove(c)
    m.extend(lead)
    cursor = 0
    for k, (key, notes) in enumerate(by_voice.items()):
        if k and cursor:
            ET.SubElement(ET.SubElement(m, "backup"), "duration").text = str(cursor)
        cursor = 0
        kept = not any(triplet(key, n) for _, _, n in notes) and _steady([(at, n) for at, _, n in notes], Fraction(1)) is not None
        placed = []
        for at, order, n in notes:
            if not kept:
                at = at * 2 // 3
                step = int(n.findtext("duration") or 0) * 2 // 3
                if triplet(key, n) and step and at % step:
                    # A note of the run homr put after a dotted one in the melody's voice: on to the next of its beat.
                    at += step - at % step
            placed.append((at, order, n))
        head, made = 0, False
        for at, _, n in sorted(placed, key=lambda x: (x[0], x[1])):
            d = n.find("duration")
            if n.find("grace") is not None:
                m.append(n)
                continue
            if n.find("chord") is None:
                head = int(d.text) if d is not None else 0
                made = triplet(key, n)
                head = head * 2 // 3 if made else max(1, min(head, length - at))
                if at > cursor:
                    ET.SubElement(ET.SubElement(m, "forward"), "duration").text = str(at - cursor)
                elif at < cursor:
                    ET.SubElement(ET.SubElement(m, "backup"), "duration").text = str(cursor - at)
                cursor = at + head
            if made:
                _mark_triplet(n)
            if d is not None:
                d.text = str(head)  # a chord's notes last as long as its first
            m.append(n)
    m.extend(rest)


def _mark_triplet(n: ET.Element) -> None:
    if n.find("time-modification") is not None:
        return
    tm = ET.Element("time-modification")
    ET.SubElement(tm, "actual-notes").text = "3"
    ET.SubElement(tm, "normal-notes").text = "2"
    later = ("stem", "notehead", "notehead-text", "staff", "beam", "notations", "lyric", "play")
    at = next((i for i, c in enumerate(n) if c.tag in later), len(n))
    n.insert(at, tm)


# ---------------------------------------------------------------------------
# B. Time signatures


def _fitting_time(length: Fraction, like: tuple[int, int]) -> tuple[int, int]:
    """A time signature whose bars last `length` quarters, in eighths when `like` is."""
    eighths = length * 2
    compound = like[1] >= 8 and like[0] % 3 == 0
    if eighths.denominator == 1 and (compound and eighths % 3 == 0 or length.denominator != 1):
        return (int(eighths), 8)
    if length.denominator == 1:
        return (int(length), 4)
    return like


def fix_times(bars: list[Bar]) -> None:
    """Time signatures that restate the bar length at a page top, or that the bars don't fill."""
    states = _states(bars)
    # A page top restating the bar length isn't a change.
    running: tuple[int, int] | None = None
    for i, bar in enumerate(bars):
        stated = [t for attrs in bar.el.iter("attributes") for t in attrs.findall("time")]
        if stated and bar.page_start and i > 0 and running and states[i].time != running and _length(states[i].time) == _length(running):
            for attrs in bar.el.iter("attributes"):
                for t in attrs.findall("time"):
                    attrs.remove(t)
        elif stated:
            running = states[i].time
    states = _states(bars)

    # The bars after a time signature fill another length: that's their time. (Each one
    # stated counts, even the same as before: a page can repeat the last page's misreading.)
    starts = [i for i, bar in enumerate(bars) if any(a.find("time") is not None for a in bar.el.iter("attributes"))]
    for k, first in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else len(bars)
        lengths = [
            Fraction(_filled(bars[i].el), states[i].divisions)
            for i in range(first, end)
            if i not in (0, len(bars) - 1)
        ]
        if len(lengths) < 2:
            continue
        common, count = Counter(lengths).most_common(1)[0]
        time = states[first].time
        if count >= 0.75 * len(lengths) and common > 0 and common != _length(time):
            _restate_time(bars[first].el, _fitting_time(common, time))  # type: ignore[arg-type]
    states = _states(bars)

    # homr reads 12/8 as 24/16 now and then: sixteenths for a bar length seen before are written as they were then.
    seen: dict[Fraction, tuple[int, int]] = {}
    for i, s in enumerate(states):
        if s.time and (i == 0 or s.time != states[i - 1].time):
            length = _length(s.time)
            if s.time[1] >= 16 and length in seen:
                _restate_time(bars[i].el, seen[length])
            else:
                seen.setdefault(length, s.time)  # type: ignore[arg-type]


def _restate_time(m: ET.Element, time: tuple[int, int]) -> None:
    for attrs in m.iter("attributes"):
        t = attrs.find("time")
        if t is not None:
            for c in list(t):
                t.remove(c)
            ET.SubElement(t, "beats").text = str(time[0])
            ET.SubElement(t, "beat-type").text = str(time[1])
            return


# ---------------------------------------------------------------------------
# C. Bars to check


def _ends_section(m: ET.Element) -> bool:
    """A repeat, an ending or a double barline: where a short bar is often printed."""
    return any(
        b.find("repeat") is not None or b.find("ending") is not None or (b.findtext("bar-style") or "").strip() in DOUBLE_BARS
        for b in m.findall("barline")
    )


def bars_to_check(bars: list[Bar], doubtful: set[int]) -> list[int]:
    """
    Bars found doubtful already, and bars that don't add up to their time
    signature. A short bar is fine first or last, and at a repeat, an ending or
    a double barline (a section's pickup, and the bar that makes it up).
    """
    states = _states(bars)
    out = set(doubtful)
    for i, bar in enumerate(bars):
        length = _length(states[i].time)
        if length is None:
            continue
        filled = Fraction(_filled(bar.el), states[i].divisions)
        edge = i in (0, len(bars) - 1) or _ends_section(bar.el) or _ends_section(bars[i - 1].el)
        if filled > length or (filled < length and not edge):
            out.add(i)
    return sorted(out)


# ---------------------------------------------------------------------------


def join_pages(pages: list[str], title: str | None = None) -> Joined:
    """One score from the pages' MusicXML, in order. Pages without bars are left out."""
    parts: list[ET.Element] = []
    first_root: ET.Element | None = None
    for xml in pages:
        try:
            root = ET.fromstring(xml)  # noqa: S314 (homr's own output)
        except ET.ParseError:
            continue
        part = piano_part(root)
        if part is None:
            continue
        if first_root is None:
            first_root = root
        parts.append(part)
    if first_root is None:
        raise JoinError("No bars were found on any page.")

    staves = max(_staves(p) for p in parts)
    score = ET.Element("score-partwise", version="4.0")
    work_title = title if title is not None else (first_root.findtext("work/work-title") or "").strip()
    if work_title:
        ET.SubElement(ET.SubElement(score, "work"), "work-title").text = work_title
    ident = ET.SubElement(score, "identification")
    ET.SubElement(ET.SubElement(ident, "encoding"), "software").text = "homr"
    part_list = ET.SubElement(score, "part-list")
    score_part = ET.SubElement(part_list, "score-part", id="P1")
    ET.SubElement(score_part, "part-name").text = "Piano"
    out = ET.SubElement(score, "part", id="P1")

    bars: list[Bar] = []
    for part in parts:
        for i, measure in enumerate(part.findall("measure")):
            m = copy.deepcopy(measure)
            _merge_leading_attributes(m)
            bars.append(Bar(m, page_start=i == 0, line_start=i == 0 or m.find("print[@new-system='yes']") is not None))
    bars = fold_empty_bars(bars)
    changed, unsure = fix_keys_and_clefs(bars, staves)
    changed |= fix_triplets(bars)  # before the time signatures: homr fits them to the triplets it misread
    fix_times(bars)
    check = bars_to_check(bars, changed | unsure)

    divisions: str | None = None
    key: ET.Element | None = None
    time: ET.Element | None = None
    clefs: dict[str, ET.Element] = {}
    number = 0

    for m in (b.el for b in bars):
        number += 1
        m.set("number", str(number))
        for attrs in m.findall("attributes"):
            for child in list(attrs):
                if child.tag in ("staves", "part-symbol"):
                    attrs.remove(child)
                elif child.tag == "divisions":
                    if child.text == divisions:
                        attrs.remove(child)
                    divisions = child.text
                elif child.tag == "key":
                    if _same(child, key):
                        attrs.remove(child)
                    key = child
                elif child.tag == "time":
                    if _same(child, time):
                        attrs.remove(child)
                    time = child
                elif child.tag == "clef":
                    staff = child.get("number", "1")
                    if _same(child, clefs.get(staff)):
                        attrs.remove(child)
                    clefs[staff] = child
            if len(attrs) == 0:
                m.remove(attrs)
            else:
                _sort_attributes(attrs)
        if number == 1:
            _start(m, staves)
        out.append(m)

    return Joined(ET.tostring(score, encoding="unicode", xml_declaration=True), [str(i + 1) for i in check])


def _merge_leading_attributes(m: ET.Element) -> None:
    """homr starts a bar with two <attributes>; before the first note they're one."""
    first: ET.Element | None = None
    for child in list(m):
        if child.tag in ("note", "backup", "forward"):
            break
        if child.tag != "attributes":
            continue
        if first is None:
            first = child
        else:
            first.extend(list(child))
            m.remove(child)


def _start(m: ET.Element, staves: int) -> None:
    """The first bar says everything: divisions, the staves and a clef for each."""
    attrs = m.find("attributes")
    if attrs is None:
        attrs = ET.Element("attributes")
        m.insert(0, attrs)
    if attrs.find("divisions") is None:
        ET.SubElement(attrs, "divisions").text = "1"
    if staves > 1:
        ET.SubElement(attrs, "staves").text = str(staves)
        ET.SubElement(attrs, "part-symbol").text = "brace"
    have = {c.get("number", "1") for c in attrs.findall("clef")}
    for staff in range(1, staves + 1):
        if str(staff) not in have:
            clef = ET.SubElement(attrs, "clef", number=str(staff)) if staves > 1 else ET.SubElement(attrs, "clef")
            ET.SubElement(clef, "sign").text = "G" if staff == 1 else "F"
            ET.SubElement(clef, "line").text = "2" if staff == 1 else "4"
    _sort_attributes(attrs)
