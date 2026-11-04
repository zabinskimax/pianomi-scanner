# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tests for joining pages and reading uploads; they don't need homr.

    tools/homr/.venv/Scripts/python -m unittest discover -s tools/homr   (or any Python 3.11+)
"""

import contextlib
import io
import os
import sys
import time
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(__file__))

from join import JoinError, join_pages  # noqa: E402
from server import UNCOLLECTED_GRACE, Jobs, log, parse_multipart  # noqa: E402


def note(step: str, octave: int, dur: int, staff: int = 1, voice: int = 1) -> str:
    return (
        f"<note><pitch><step>{step}</step><octave>{octave}</octave></pitch><duration>{dur}</duration>"
        f"<voice>{voice}</voice><staff>{staff}</staff></note>"
    )


def page(measures: list[str], title: str = "", parts: list[tuple[str, list[str]]] | None = None) -> str:
    """A page the way homr writes it."""
    parts = parts or [("P1", measures)]
    plist = "".join(f'<score-part id="{pid}"><part-name>Piano</part-name></score-part>' for pid, _ in parts)
    body = "".join(f'<part id="{pid}">' + "".join(f'<measure number="{i + 1}">{m}</measure>' for i, m in enumerate(ms)) + "</part>" for pid, ms in parts)
    work = f"<work><work-title>{title}</work-title></work>" if title else ""
    return f'<?xml version="1.0"?><score-partwise version="4.0">{work}<part-list>{plist}</part-list>{body}</score-partwise>'


GRAND = (
    "<attributes><divisions>2</divisions><staves>2</staves><part-symbol>brace</part-symbol></attributes>"
    '<attributes><clef number="1"><sign>G</sign><line>2</line></clef><clef number="2"><sign>F</sign><line>4</line></clef>'
    "<key><fifths>1</fifths></key><time><beats>3</beats><beat-type>4</beat-type></time></attributes>"
)


def measures(xml: str) -> list[ET.Element]:
    return ET.fromstring(xml).find("part").findall("measure")


class JoinTest(unittest.TestCase):
    def test_numbers_bars_through_and_keeps_signatures_only_where_they_change(self) -> None:
        p1 = page([GRAND + note("D", 5, 6) + "<backup><duration>6</duration></backup>" + note("G", 3, 6, 2, 5), note("C", 5, 6)], title="Minuet in G")
        p2 = page([GRAND.replace("<divisions>2</divisions>", "<divisions>4</divisions>") + note("B", 4, 12), note("A", 4, 12)])
        p3 = page([GRAND.replace("<fifths>1</fifths>", "<fifths>2</fifths>") + note("D", 5, 6)])
        xml = join_pages([p1, p2, p3]).xml
        root = ET.fromstring(xml)
        self.assertEqual(root.findtext("work/work-title"), "Minuet in G")
        self.assertEqual(len(root.findall("part")), 1)
        bars = measures(xml)
        self.assertEqual([m.get("number") for m in bars], ["1", "2", "3", "4", "5"])

        first = bars[0].findall("attributes")
        self.assertEqual(len(first), 1, "the first bar's attributes are in one place")
        self.assertEqual([c.tag for c in first[0]], ["divisions", "key", "time", "staves", "part-symbol", "clef", "clef"])

        # Page 2 only changes the divisions; page 3 changes them back, and the key.
        self.assertEqual([c.tag for c in bars[2].find("attributes")], ["divisions"])
        self.assertEqual(bars[2].findtext("attributes/divisions"), "4")
        self.assertIsNone(bars[3].find("attributes"))
        self.assertEqual([c.tag for c in bars[4].find("attributes")], ["divisions", "key"])
        self.assertEqual(bars[4].findtext("attributes/key/fifths"), "2")

    def test_a_piece_with_two_staves_says_so_from_the_first_bar(self) -> None:
        treble_only = page(["<attributes><divisions>1</divisions><clef><sign>G</sign><line>2</line></clef></attributes>" + note("C", 5, 4)])
        grand = page([GRAND + note("D", 5, 6) + "<backup><duration>6</duration></backup>" + note("G", 3, 6, 2, 5)])
        attrs = measures(join_pages([treble_only, grand]).xml)[0].find("attributes")
        self.assertEqual(attrs.findtext("staves"), "2")
        clefs = {c.get("number", "1"): c.findtext("sign") for c in attrs.findall("clef")}
        self.assertEqual(clefs, {"1": "G", "2": "F"})

    def test_keeps_the_piano_from_a_page_with_another_part(self) -> None:
        voice = ["<attributes><divisions>1</divisions></attributes>" + note("E", 5, 4)]
        piano = [GRAND + note("C", 5, 6) + "<backup><duration>6</duration></backup>" + note("C", 3, 6, 2, 5)]
        xml = join_pages([page([], parts=[("P1", voice), ("P2", piano)])]).xml
        steps = [n.findtext("pitch/step") + n.findtext("pitch/octave") for n in ET.fromstring(xml).iter("note")]
        self.assertEqual(steps, ["C5", "C3"])

    def test_folds_the_empty_bar_homr_writes_at_the_end_of_an_ending(self) -> None:
        first_ending = '<barline location="left"><ending number="1" type="start"/></barline>' + note("E", 4, 8)
        first_ending += '<barline location="right"><repeat direction="backward"/></barline>'
        empty = '<barline location="right"><ending number="1" type="stop"/></barline>'
        second = '<barline location="left"><ending number="2" type="start"/></barline>' + note("C", 4, 8)
        bars = measures(join_pages([page([GRAND + note("D", 4, 8), first_ending, empty, second])]).xml)
        self.assertEqual([m.get("number") for m in bars], ["1", "2", "3"])
        right = [b for b in bars[1].findall("barline") if b.get("location") == "right"]
        self.assertEqual(len(right), 1)
        self.assertEqual([c.tag for c in right[0]], ["ending", "repeat"])
        self.assertEqual(right[0].find("ending").attrib, {"number": "1", "type": "stop"})
        self.assertEqual(bars[2].find("barline/ending").get("number"), "2")

    def test_leaves_out_pages_without_bars_and_fails_without_any(self) -> None:
        good = page([GRAND + note("D", 5, 6)])
        self.assertEqual(len(measures(join_pages(["not xml", page([]), good]).xml)), 1)
        with self.assertRaises(JoinError):
            join_pages(["<score-partwise/>", "oops"])


def tone(name: str, dur: int, staff: int = 1) -> str:
    """A note by name, "Eb4", spelled as homr writes it (no alter for a natural)."""
    step, octave = name[0], name[-1]
    alter = {"b": -1, "#": 1}.get(name[1:-1], 0)
    a = f"<alter>{alter}</alter>" if alter else ""
    return f"<note><pitch><step>{step}</step>{a}<octave>{octave}</octave></pitch><duration>{dur}</duration><voice>{staff}</voice><staff>{staff}</staff></note>"


def bar(right: list[str], left: list[str], dur: int, head: str = "") -> str:
    """A bar of single notes in each hand, each lasting `dur`."""
    back = f"<backup><duration>{dur * len(right)}</duration></backup>"
    return head + "".join(tone(n, dur) for n in right) + back + "".join(tone(n, dur, 2) for n in left)


def attrs(fifths: int | None = None, time: tuple[int, int] | None = None, clefs: tuple[str, str] | None = None, first: bool = False) -> str:
    """What homr writes at a line or page start; `first` adds the divisions and staves."""
    out = "<attributes>"
    if first:
        out += "<divisions>2</divisions><staves>2</staves>"
    if fifths is not None:
        out += f"<key><fifths>{fifths}</fifths></key>"
    if time:
        out += f"<time><beats>{time[0]}</beats><beat-type>{time[1]}</beat-type></time>"
    if clefs:
        out += "".join(f'<clef number="{i + 1}"><sign>{c}</sign><line>{"2" if c == "G" else "4"}</line></clef>' for i, c in enumerate(clefs))
    return out + "</attributes>"


LINE = '<print new-system="yes"/>'


def names(m: ET.Element, staff: int) -> list[str]:
    out = []
    for n in m.iter("note"):
        p = n.find("pitch")
        if p is not None and n.findtext("staff") == str(staff):
            out.append(p.findtext("step") + {"-1": "b", "1": "#", None: ""}[p.findtext("alter")] + p.findtext("octave"))
    return out


def signatures(m: ET.Element, tag: str) -> list[str]:
    return ["/".join(x.text for x in el) for a in m.findall("attributes") for el in a.findall(tag)]


class MisreadTest(unittest.TestCase):
    """homr misreads a key or a clef for a line now and then; the lines around it give it away."""

    def test_a_key_misread_for_a_line_is_spelled_again_in_the_key_around_it(self) -> None:
        start = attrs(-4, (3, 4), ("G", "F"), first=True)
        joined = join_pages(
            [
                page(
                    [
                        bar(["Ab4", "Bb4", "C5"], ["F2"], 2, start),
                        bar(["Db5", "C5", "Bb4"], ["Db3"], 2),
                        # homr saw no flats here: plain E, B, A, and the sharp it did see.
                        bar(["E4", "F#4", "A4"], ["B2"], 2, LINE + attrs(0)),
                        bar(["Ab4", "G4", "F4"], ["F2"], 2, LINE + attrs(-4)),
                    ]
                )
            ]
        )
        bars = measures(joined.xml)
        self.assertEqual(names(bars[2], 1), ["Eb4", "F#4", "Ab4"])
        self.assertEqual(names(bars[2], 2), ["Bb2"])
        self.assertEqual([signatures(b, "key") for b in bars], [["-4"], [], [], []])
        self.assertEqual(joined.check_bars, ["3"])

    def test_a_real_key_change_stays(self) -> None:
        start = attrs(-4, (3, 4), ("G", "F"), first=True)
        f_minor = bar(["Ab4", "Bb4", "C5"], ["F2"], 2)
        a_major = bar(["E5", "C#5", "A4"], ["A2"], 2)
        after_double_bar = page(
            [
                f_minor.replace("<note>", start + "<note>", 1),
                f_minor + '<barline location="right"><bar-style>light-light</bar-style></barline>',
                LINE + attrs(3) + a_major,
                LINE + attrs(-4) + f_minor,
            ]
        )
        joined = join_pages([after_double_bar])
        self.assertEqual(names(measures(joined.xml)[2], 1), ["E5", "C#5", "A4"])
        self.assertEqual(joined.check_bars, [])

        # Four lines in another key are a section in it, not a misreading.
        long = page([f_minor.replace("<note>", start + "<note>", 1)] + [LINE + (attrs(3) if i == 0 else "") + a_major for i in range(4)] + [LINE + attrs(-4) + f_minor])
        self.assertEqual(names(measures(join_pages([long]).xml)[4], 1), ["E5", "C#5", "A4"])

    def test_lines_read_right_between_misread_ones_stay_as_they_are(self) -> None:
        start = attrs(-4, (3, 4), ("G", "F"), first=True)
        right = bar(["Ab4", "Bb4", "C5"], ["F2"], 2)
        wrong = bar(["A4", "B4", "C5"], ["F2"], 2)
        lines = [start + right, right, LINE + attrs(0) + wrong, LINE + attrs(-4) + right, LINE + attrs(0) + wrong, LINE + attrs(-4) + right, right, right]
        bars = measures(join_pages([page(lines)]).xml)
        self.assertEqual({tuple(names(b, 1)) for b in bars}, {("Ab4", "Bb4", "C5")})

    def test_a_right_hand_misread_in_bass_clef_goes_back_up(self) -> None:
        start = attrs(-4, (3, 4), ("G", "F"), first=True)
        # Read on a bass clef, the right hand's A flat, B flat, C sit on the lines of C3, D3, E3.
        joined = join_pages(
            [
                page(
                    [
                        bar(["Ab4", "Bb4", "C5"], ["F2", "C3", "F3"], 2, start),
                        bar(["C3", "D3", "E3"], ["F2", "C3", "F3"], 2, LINE + attrs(clefs=("F", "F"))),
                        bar(["Ab4", "Bb4", "C5"], ["F2", "C3", "F3"], 2, LINE + attrs(clefs=("G", "F"))),
                    ]
                )
            ]
        )
        bars = measures(joined.xml)
        self.assertEqual(names(bars[1], 1), ["Ab4", "Bb4", "C5"])
        self.assertEqual(signatures(bars[1], "clef"), [])
        self.assertEqual(joined.check_bars, ["2"])

    def test_other_clef_changes_for_a_line_stay_but_are_to_check(self) -> None:
        start = attrs(-4, (3, 4), ("G", "F"), first=True)
        home = bar(["Ab4", "Bb4", "C5"], ["F2", "C3", "F3"], 2)
        # The right hand low in bass clef, still well above the left hand.
        low = bar(["F3", "Ab3", "C4"], ["F2"], 2, LINE + attrs(clefs=("F", "F")))
        # The left hand in treble clef, under the right hand: misread or not, it can't be told.
        high = bar(["C6", "Bb5", "Ab5"], ["F4", "Ab4", "C5"], 2, LINE + attrs(clefs=("G", "G")))
        joined = join_pages([page([start + home, low, LINE + attrs(clefs=("G", "F")) + home, high, LINE + attrs(clefs=("G", "F")) + home])])
        bars = measures(joined.xml)
        self.assertEqual(names(bars[1], 1), ["F3", "Ab3", "C4"])
        self.assertEqual(signatures(bars[1], "clef"), ["F/4"])
        self.assertEqual(names(bars[3], 2), ["F4", "Ab4", "C5"])
        self.assertEqual(joined.check_bars, ["2", "4"])


class TripletTest(unittest.TestCase):
    """homr reads unmarked triplets as plain eighths: the voice runs half as long again as the others."""

    def piece(self, right: str, melody: str = "") -> list[ET.Element]:
        start = attrs(4, (3, 2), ("G", "F"), first=True)  # homr fitted the time signature to the long voice
        left = '<backup><duration>12</duration></backup><note><pitch><step>C</step><octave>3</octave></pitch><duration>8</duration><voice>5</voice><staff>2</staff></note>'
        return measures(join_pages([page([start + right + left + melody] + [right + left + melody] * 3)]).xml)

    def eighths(self, steps: str) -> str:
        return "".join(tone(s, 1) for s in steps.split())  # at 2 divisions, 1 is an eighth

    def test_a_voice_of_threes_half_as_long_again_becomes_triplets(self) -> None:
        bars = self.piece(self.eighths("G#3 C#4 E4 " * 4))
        notes = [n for n in bars[0].iter("note") if n.findtext("staff") == "1"]
        self.assertEqual(bars[0].findtext("attributes/divisions"), "6")
        self.assertEqual({n.findtext("duration") for n in notes}, {"2"})  # a third of a quarter
        self.assertEqual({n.findtext("time-modification/actual-notes") for n in notes}, {"3"})
        self.assertEqual(signatures(bars[0], "time"), ["4/4"])  # the bars now fill four beats

    def test_a_melody_beside_the_triplets_keeps_its_rhythm(self) -> None:
        melody = '<backup><duration>8</duration></backup><note><pitch><step>B</step><octave>4</octave></pitch><duration>6</duration><voice>2</voice><staff>1</staff></note>'
        bars = self.piece(self.eighths("G#3 C#4 E4 " * 4), melody)
        tune = [n for n in bars[0].iter("note") if n.findtext("voice") == "2"]
        self.assertEqual([n.findtext("duration") for n in tune], ["18"])
        self.assertIsNone(tune[0].find("time-modification"))

    def test_a_run_shared_between_the_hands_is_not_triplets(self) -> None:
        # Chasing Kou in 6/8: each run starts in the left hand and goes on in the right, so the left hand's
        # few notes stop two thirds of the way through the bar while the right hand fills it.
        fwd = lambda d: f"<forward><duration>{d}</duration></forward>"  # noqa: E731
        right = fwd(1) + self.eighths("Ab3 C4 Db4 C4 Ab3") + fwd(2) + self.eighths("C4 Db4 Eb4 Ab4")
        left = '<backup><duration>12</duration></backup>' + tone("Gb2", 1, 2) + fwd(5) + tone("Db2", 1, 2) + tone("Db3", 1, 2)
        start = attrs(-5, (6, 4), ("G", "F"), first=True)
        bars = measures(join_pages([page([start + right + left] + [right + left] * 3)]).xml)
        self.assertIsNone(bars[1].find(".//time-modification"))
        self.assertEqual(bars[0].findtext("attributes/divisions"), "2")

    def test_a_long_voice_not_in_equal_threes_is_left_alone(self) -> None:
        bars = self.piece(tone("C5", 2) + self.eighths("D5 E5 F5 G5 A5 B5 C5 D5 E5 F5"))
        self.assertEqual(bars[0].findtext("attributes/divisions"), "2")
        self.assertIsNone(bars[0].find(".//time-modification"))


class TimeTest(unittest.TestCase):
    def test_time_signatures_follow_the_bars(self) -> None:
        def two(n: int) -> list[str]:
            return [bar(["C5"] * n, ["F2"], 2)]  # n quarters in the right hand

        page1 = page(
            [
                # homr read 4/4 as 6/4.
                bar(["C5"] * 4, ["F2"], 2, attrs(-4, (6, 4), ("G", "F"), first=True)),
                *two(4),
                *two(4),
                bar(["C5"] * 6, ["F2"], 2, attrs(time=(12, 8))),
                *two(6),
            ]
        )
        # Every page starts with a time signature, printed or not.
        page2 = page([bar(["C5"] * 6, ["F2"], 2, attrs(-4, (6, 4), ("G", "F"))), *two(6), bar(["C5"] * 6, ["F2"], 2, attrs(time=(24, 16))), *two(6)])
        bars = measures(join_pages([page1, page2]).xml)
        self.assertEqual([signatures(b, "time") for b in bars], [["4/4"], [], [], ["12/8"], [], [], [], [], []])

    def test_a_page_top_signature_of_another_length_stays(self) -> None:
        page1 = page([bar(["C5"] * 3, ["F2"], 2, attrs(0, (3, 4), ("G", "F"), first=True)), bar(["C5"] * 3, ["F2"], 2)])
        page2 = page([bar(["C5"] * 4, ["F2"], 2, attrs(0, (4, 4), ("G", "F"))), bar(["C5"] * 4, ["F2"], 2)])
        bars = measures(join_pages([page1, page2]).xml)
        self.assertEqual(signatures(bars[2], "time"), ["4/4"])


class CheckBarsTest(unittest.TestCase):
    def test_bars_that_dont_add_up_are_to_check_but_a_short_first_or_last_bar_is_fine(self) -> None:
        start = attrs(0, (3, 4), ("G", "F"), first=True)
        bars = [
            bar(["C5"], ["C3"], 2, start),  # a pickup
            bar(["C5"] * 3, ["C3"] * 3, 2),
            bar(["C5"] * 2, ["C3"] * 2, 2),  # a beat lost
            bar(["C5"] * 3, ["C3"] * 3, 2),
            bar(["C5"] * 4, ["C3"] * 3, 2),  # a beat too many
            bar(["C5"] * 3, ["C3"] * 3, 2),
            bar(["C5"] * 2, ["C3"] * 2, 2) + '<barline location="right"><repeat direction="backward"/></barline>',  # makes up the pickup
            bar(["C5"], ["C3"], 2),  # the next section's pickup
            bar(["C5"] * 3, ["C3"] * 3, 2),
            bar(["C5"] * 2, ["C3"] * 2, 2),  # the end, after the pickup
        ]
        self.assertEqual(join_pages([page(bars)]).check_bars, ["3", "5"])


class UploadTest(unittest.TestCase):
    def test_reads_the_files_of_a_form_in_order_with_their_bytes_intact(self) -> None:
        binary = bytes(range(256)) * 3 + b"\r\n--not-the-boundary\r\n"
        body = (
            b"--XyZ\r\n"
            b'Content-Disposition: form-data; name="note"\r\n\r\nhello\r\n'
            b"--XyZ\r\n"
            b'Content-Disposition: form-data; name="file"; filename="p1.png"\r\nContent-Type: image/png\r\n\r\n' + binary + b"\r\n"
            b"--XyZ\r\n"
            b'Content-Disposition: form-data; name="file"; filename="p2.jpg"\r\nContent-Type: image/jpeg\r\n\r\nJPEG\r\n'
            b"--XyZ--\r\n"
        )
        files = parse_multipart(body, "multipart/form-data; boundary=XyZ")
        self.assertEqual([(n, t) for n, t, _ in files], [("p1.png", "image/png"), ("p2.jpg", "image/jpeg")])
        self.assertEqual(files[0][2], binary)
        self.assertEqual(files[1][2], b"JPEG")

    def test_a_file_name_the_console_cant_spell_is_read_and_logged(self) -> None:
        name = "Comptine d'un autre été - L'après-midi.pdf"
        body = (
            b"--XyZ\r\n"
            + f'Content-Disposition: form-data; name="file"; filename="{name}"\r\nContent-Type: application/pdf\r\n\r\n'.encode()
            + b"%PDF-1.4\r\n--XyZ--\r\n"
        )
        files = parse_multipart(body, "multipart/form-data; boundary=XyZ")
        self.assertEqual(files[0][0], name)
        # A Central European Windows console: é is there, è isn't.
        out = io.TextIOWrapper(io.BytesIO(), encoding="cp1250")
        with contextlib.redirect_stdout(out):
            log(f"Scan 1: {name}, 1 page(s)")
        out.seek(0)
        self.assertIn("été - L'apr?s-midi", out.read())


class IdleTest(unittest.TestCase):
    def jobs(self) -> Jobs:
        jobs = Jobs(reader=None)  # type: ignore[arg-type]
        jobs.last_active -= 3600
        return jobs

    def test_idle_only_with_nothing_to_read_or_collect(self) -> None:
        jobs = self.jobs()
        self.assertTrue(jobs.idle(1200))
        job = jobs.add("a.pdf", "unused", ["p1.png"])
        self.assertFalse(jobs.idle(1200))
        jobs.last_active -= 3600
        self.assertFalse(jobs.idle(1200), "a scan waiting keeps it going")
        jobs.update(job, status="done", finished=time.time())
        jobs.last_active -= 3600
        self.assertFalse(jobs.idle(1200), "a finished scan nobody collected keeps it going")
        job.finished = time.time() - UNCOLLECTED_GRACE - 1
        self.assertTrue(jobs.idle(1200))
        jobs.touch()
        self.assertFalse(jobs.idle(1200), "asking about a scan counts")


if __name__ == "__main__":
    unittest.main()
