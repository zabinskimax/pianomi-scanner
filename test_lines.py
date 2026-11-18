# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tests for the second look at lines homr missed; they don't need homr, but
need numpy and Pillow (homr's environment):

    tools/homr/.venv/Scripts/python -m unittest discover -s tools/homr
"""

import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(__file__))

try:
    import numpy as np
    from PIL import Image, ImageDraw

    import lines
except ImportError:  # plain Python: these tests need homr's environment
    lines = None  # type: ignore[assignment]


W, H = 1200, 1000
STAFF_TOPS = [(100, 160), (400, 460), (700, 760)]  # each line's two staves


def page(joined: tuple[bool, ...] = (True, True, True)) -> "Image.Image":
    """Three lines of two staves, each line's staves joined by its opening barline unless not `joined`."""
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    for (top, bottom), join in zip(STAFF_TOPS, joined):
        for staff in (top, bottom):
            for k in range(5):
                d.line([(100, staff + 10 * k), (1100, staff + 10 * k)], fill="black", width=1)
        if join:
            d.line([(100, top), (100, bottom + 40)], fill="black", width=2)
        d.text((300, top + 50), "mp", fill="black")
    return img


LINE = '<print new-system="yes"/>'


def measure(notes: str = "", attrs: str = "", new_line: bool = False) -> str:
    return f"<measure>{LINE if new_line else ''}{attrs}{notes}</measure>"


def note(step: str, dur: int, staff: int = 1) -> str:
    return f"<note><pitch><step>{step}</step><octave>4</octave></pitch><duration>{dur}</duration><voice>1</voice><staff>{staff}</staff></note>"


def score(measures: list[str]) -> str:
    return (
        '<?xml version="1.0"?><score-partwise version="4.0"><part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>'
        f'<part id="P1">{"".join(measures)}</part></score-partwise>'
    )


PAGE_ATTRS = "<attributes><divisions>2</divisions><key><fifths>-4</fifths></key><time><beats>3</beats><beat-type>4</beat-type></time></attributes>"
FULL = note("C", 6)  # a full 3/4 bar at 2 divisions


def steps(xml: str) -> list[str]:
    return [n.findtext("pitch/step") for n in ET.fromstring(xml).iter("note")]


@unittest.skipIf(lines is None, "needs numpy and Pillow")
class FindLinesTest(unittest.TestCase):
    def test_finds_each_line_with_both_staves(self) -> None:
        found = lines.find_lines(np.asarray(page().convert("L")) < lines.DARK)
        self.assertEqual([len(line.staves) for line in found], [2, 2, 2])
        self.assertAlmostEqual(found[1].box.y0, 400 / H, places=2)
        self.assertAlmostEqual(found[1].box.x0, 100 / W, places=2)
        self.assertAlmostEqual(found[1].box.x1, 1100 / W, places=2)

    def test_staves_not_joined_by_a_barline_are_lines_of_their_own(self) -> None:
        found = lines.find_lines(np.asarray(page((True, False, True)).convert("L")) < lines.DARK)
        self.assertEqual([len(line.staves) for line in found], [2, 1, 1, 2])


@unittest.skipIf(lines is None, "needs numpy and Pillow")
class BadLinesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.lines = lines.find_lines(np.asarray(page().convert("L")) < lines.DARK)

    def box(self, i: int, x0: float = 100, x1: float = 1100) -> "lines.Box":
        top, bottom = STAFF_TOPS[i]
        return lines.Box(x0 / W, top / H, x1 / W, (bottom + 40) / H)

    def test_a_line_missed_or_found_in_part_is_bad(self) -> None:
        self.assertEqual(lines.bad_lines(self.lines, [self.box(0), self.box(1), self.box(2)]), [])
        self.assertEqual(lines.bad_lines(self.lines, [self.box(0), self.box(2)]), [1])
        self.assertEqual(lines.bad_lines(self.lines, [self.box(0), self.box(1, x0=600), self.box(2)]), [1])
        top_staff_only = lines.Box(100 / W, 400 / H, 1100 / W, 440 / H)
        self.assertEqual(lines.bad_lines(self.lines, [self.box(0), top_staff_only, self.box(2)]), [1])

    def test_lines_that_dont_match_ours_leave_the_page_alone(self) -> None:
        between = lines.Box(100 / W, 280 / H, 1100 / W, 320 / H)
        self.assertIsNone(lines.bad_lines(self.lines, [self.box(0), between, self.box(2)]))


@unittest.skipIf(lines is None, "needs numpy and Pillow")
class SpliceTest(unittest.TestCase):
    def test_a_line_read_again_takes_its_place(self) -> None:
        # homr read lines 1 and 3; line 2 comes from reading it alone (with a made-up time and the key missed).
        page_xml = score([measure(note("C", 6), PAGE_ATTRS), measure(FULL), measure(note("E", 6), new_line=True)])
        line = score([measure(note("D", 6), "<attributes><divisions>4</divisions><key><fifths>0</fifths></key><time><beats>6</beats><beat-type>8</beat-type></time></attributes>")])
        out = ET.fromstring(lines.splice(page_xml, [0, 2], {1: line}, 3)).find("part").findall("measure")
        self.assertEqual([m.findtext("note/pitch/step") for m in out], ["C", "C", "D", "E"])
        self.assertEqual([m.get("number") for m in out], ["1", "2", "3", "4"])
        self.assertEqual([m.find("print") is not None for m in out], [False, False, True, True])
        # The line keeps its own divisions and key, but the page's time.
        self.assertEqual(out[2].findtext("attributes/divisions"), "4")
        self.assertEqual(out[2].findtext("attributes/time/beats"), "3")
        # After it, the page's divisions, key and time are said again.
        self.assertEqual((out[3].findtext("attributes/divisions"), out[3].findtext("attributes/key/fifths")), ("2", "-4"))

    def test_bars_found_in_part_are_replaced_and_the_first_line_keeps_the_pages_time(self) -> None:
        page_xml = score([measure(note("G", 6), PAGE_ATTRS), measure(note("E", 6), new_line=True)])
        line = score([measure(note("C", 6), "<attributes><divisions>2</divisions></attributes>"), measure(FULL)])
        out = ET.fromstring(lines.splice(page_xml, [0, 1], {0: line}, 2)).find("part").findall("measure")
        self.assertEqual([m.findtext("note/pitch/step") for m in out], ["C", "C", "E"])
        self.assertEqual(out[0].findtext("attributes/time/beat-type"), "4")


@unittest.skipIf(lines is None, "needs numpy and Pillow")
class SecondLookTest(unittest.TestCase):
    def test_reads_the_missed_line_again_squeezed_when_its_bars_dont_add_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "page-1.png")
            page().save(path)
            # homr read lines 1 and 3 and missed line 2.
            systems = [[(100 / W, top / H, 1100 / W, (bottom + 40) / H)] for top, bottom in (STAFF_TOPS[0], STAFF_TOPS[2])]
            page_xml = score([measure(note("C", 6), PAGE_ATTRS), measure(note("E", 6), new_line=True)])
            asked: list[str] = []

            def read_line(image: str) -> str:
                asked.append(os.path.basename(image))
                lost = "-100" in image  # as printed, half the bar is lost; squeezed, it's all there
                return score([measure(note("D", 3 if lost else 6), "<attributes><divisions>2</divisions></attributes>")])

            xml, again = lines.second_look(path, page_xml, systems, read_line, tmp)
            self.assertEqual(again, 1)
            self.assertEqual(asked, ["page-1-line-2-100.png", "page-1-line-2-60.png"])
            self.assertEqual(steps(xml), ["C", "D", "E"])
            self.assertEqual(ET.fromstring(xml).find("part").findall("measure")[1].findtext("note/duration"), "6")

    def test_a_page_homr_read_whole_is_left_as_it_is(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "page-1.png")
            page().save(path)
            systems = [[(100 / W, top / H, 1100 / W, (bottom + 40) / H)] for top, bottom in STAFF_TOPS]
            page_xml = score([measure(FULL, PAGE_ATTRS), measure(FULL, new_line=True), measure(FULL, new_line=True)])
            xml, again = lines.second_look(path, page_xml, systems, lambda _: self.fail("nothing to read again"), tmp)
            self.assertEqual((xml, again), (page_xml, 0))

    def test_says_where_each_line_of_the_result_sits_for_the_page_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "page-1.png")
            page().save(path)
            systems = [[(100 / W, top / H, 1100 / W, (bottom + 40) / H)] for top, bottom in (STAFF_TOPS[0], STAFF_TOPS[2])]
            homr_boxes = [lines.Box(0.1, 0.1, 0.9, 0.3, [0.5]), lines.Box(0.1, 0.7, 0.9, 0.9, [0.4])]
            page_xml = score([measure(note("C", 6), PAGE_ATTRS), measure(note("E", 6), new_line=True)])
            read_line = lambda _: score([measure(note("D", 6), "<attributes><divisions>2</divisions></attributes>")])  # noqa: E731
            boxes: list[lines.Box] = []
            lines.second_look(path, page_xml, systems, read_line, tmp, placed=homr_boxes, boxes=boxes)
            # Three lines: homr's first, the one read again (found here), homr's last.
            self.assertEqual(len(boxes), 3)
            self.assertIs(boxes[0], homr_boxes[0])
            self.assertIs(boxes[2], homr_boxes[1])
            self.assertAlmostEqual(boxes[1].y0, 400 / H, places=2)
            self.assertEqual(boxes[1].barlines, [])


if __name__ == "__main__":
    unittest.main()
