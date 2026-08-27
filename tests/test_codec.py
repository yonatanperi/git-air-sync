"""Codec round-trip and corruption detection. Zero third-party dependencies."""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from git_air_sync.core.codec import (
    CodecError,
    decode_docx_to_bytes,
    encode_bytes_to_docx,
    estimate_docx_size,
)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


class CodecRoundTrip(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _round_trip(self, payload: bytes) -> None:
        out = self.tmp / "package.docx"
        written = encode_bytes_to_docx(payload, out)
        self.assertGreater(written, 0)
        self.assertTrue(out.is_file())
        self.assertEqual(decode_docx_to_bytes(out), payload)

    def test_sizes_around_the_chunk_boundary(self) -> None:
        # 1024 is the chunk size; the boundaries are where an off-by-one would show.
        for size in (1, 2, 1023, 1024, 1025, 2048, 2049, 5000):
            with self.subTest(size=size):
                self._round_trip(os.urandom(size))

    def test_leading_zero_bytes_survive(self) -> None:
        # This is exactly what the \x01 sentinel in the encoding exists to protect.
        self._round_trip(b"\x00\x00\x00hello")
        self._round_trip(b"\x00" * 3000)

    def test_trailing_zero_bytes_survive(self) -> None:
        self._round_trip(b"hello\x00\x00\x00")

    def test_git_bundle_header_shape(self) -> None:
        self._round_trip(b"# v2 git bundle\n" + os.urandom(4096))

    def test_no_stray_txt_file_is_written(self) -> None:
        # The vendored Encoder.encode() writes a .txt sibling; we must not.
        out = self.tmp / "package.docx"
        encode_bytes_to_docx(b"payload" * 100, out)
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["package.docx"])

    def test_output_path_is_exact(self) -> None:
        out = self.tmp / "nested" / "chosen-name.docx"
        encode_bytes_to_docx(b"x" * 500, out)
        self.assertTrue(out.is_file())

    def test_no_part_file_survives(self) -> None:
        out = self.tmp / "package.docx"
        encode_bytes_to_docx(b"x" * 500, out)
        self.assertFalse((self.tmp / "package.docx.part").exists())

    def test_docx_is_a_readable_zip(self) -> None:
        out = self.tmp / "package.docx"
        encode_bytes_to_docx(os.urandom(2048), out)
        with zipfile.ZipFile(out) as zf:
            self.assertIn("word/document.xml", zf.namelist())
            self.assertIn("[Content_Types].xml", zf.namelist())
            self.assertIn("_rels/.rels", zf.namelist())


class CodecGuards(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_empty_payload_is_refused(self) -> None:
        # Without this guard the vendored decoder dies on int('').
        with self.assertRaises(CodecError):
            encode_bytes_to_docx(b"", self.tmp / "empty.docx")

    def test_non_zip_input_is_reported_clearly(self) -> None:
        bogus = self.tmp / "bogus.docx"
        bogus.write_bytes(b"this is not a zip file at all")
        with self.assertRaises(CodecError) as caught:
            decode_docx_to_bytes(bogus)
        self.assertIn("not a valid .docx", str(caught.exception))

    def test_zip_without_document_part_is_reported(self) -> None:
        odd = self.tmp / "odd.docx"
        with zipfile.ZipFile(odd, "w") as zf:
            zf.writestr("hello.txt", "hi")
        with self.assertRaises(CodecError) as caught:
            decode_docx_to_bytes(odd)
        self.assertIn("word/document.xml", str(caught.exception))

    def test_empty_file_is_reported(self) -> None:
        empty = self.tmp / "empty.docx"
        empty.write_bytes(b"")
        with self.assertRaises(CodecError):
            decode_docx_to_bytes(empty)

    def test_reflowed_paragraph_names_the_cause(self) -> None:
        """Simulate Word re-saving the document and mangling a paragraph."""
        out = self.tmp / "package.docx"
        encode_bytes_to_docx(os.urandom(3000), out)

        with zipfile.ZipFile(out) as zf:
            xml = zf.read("word/document.xml").decode()
            others = {
                n: zf.read(n) for n in zf.namelist() if n != "word/document.xml"
            }

        # Inject a word into the first paragraph, as an editor's autocorrect might.
        broken = xml.replace(
            'xml:space="preserve">', 'xml:space="preserve">oops ', 1
        )

        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in others.items():
                zf.writestr(name, data)
            zf.writestr("word/document.xml", broken)

        with self.assertRaises(CodecError) as caught:
            decode_docx_to_bytes(out)
        message = str(caught.exception)
        self.assertIn("paragraph", message)
        self.assertIn("Word", message)


PIN_FILE = Path(__file__).with_name("format_pin.txt")


class FormatPin(unittest.TestCase):
    """Pins the on-the-wire format produced by the vendored codec.

    The expected digest is recorded on first run into ``tests/format_pin.txt``, which
    is committed. If this test fails afterwards, the encoding changed and every .docx
    produced before the change has become unreadable — that is a breaking change to be
    versioned, not a test to update.
    """

    PAYLOAD = bytes(range(256)) * 8  # deterministic 2048 bytes

    def _document_xml(self) -> bytes:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "pin.docx"
            encode_bytes_to_docx(self.PAYLOAD, out)
            self.assertEqual(decode_docx_to_bytes(out), self.PAYLOAD)
            with zipfile.ZipFile(out) as zf:
                return zf.read("word/document.xml")

    def test_structure_is_as_expected(self) -> None:
        document = self._document_xml()
        self.assertTrue(document.startswith(b"<?xml"))
        # 2048 bytes at a 1024-byte chunk size must produce exactly two paragraphs.
        self.assertEqual(document.count(b"<w:p>"), 2)

    def test_digest_matches_the_recorded_pin(self) -> None:
        digest = hashlib.sha256(self._document_xml()).hexdigest()

        if not PIN_FILE.exists():
            PIN_FILE.write_text(digest + "\n", encoding="utf-8")
            self.skipTest(
                f"Recorded the format pin as {digest[:16]}… in {PIN_FILE.name}. "
                "Commit that file; subsequent runs will compare against it."
            )

        expected = PIN_FILE.read_text(encoding="utf-8").strip()
        self.assertEqual(
            digest,
            expected,
            "\n\nThe codec wire format CHANGED.\n"
            "Every .docx produced before this change is now unreadable.\n"
            "If this followed a re-vendor from txt-codec, revert it or bump the\n"
            "envelope version deliberately — do not just update this file.\n",
        )


class SizeEstimate(unittest.TestCase):
    """The estimate drives the size warning and the disk-space pre-flight.

    A single multiplier is wrong here: the fixed OOXML boilerplate dominates small
    payloads (a 1 KB payload becomes a 2.4 KB document) while the ratio converges to
    about 1.36 for large ones. These cases pin both ends of that curve.
    """

    def _actual(self, payload: bytes) -> int:
        with tempfile.TemporaryDirectory() as tmp:
            return encode_bytes_to_docx(payload, Path(tmp) / "sized.docx")

    def test_estimate_tracks_reality_across_the_whole_range(self) -> None:
        for size in (1024, 16 * 1024, 256 * 1024, 1024 * 1024):
            with self.subTest(size=size):
                payload = os.urandom(size)
                actual = self._actual(payload)
                estimated = estimate_docx_size(size)
                self.assertLess(
                    abs(actual - estimated) / actual,
                    0.10,
                    f"{size} bytes: estimated {estimated}, actual {actual}",
                )

    def test_small_payloads_are_not_underestimated(self) -> None:
        # Underestimating here would let the disk-space pre-flight pass wrongly.
        for size in (100, 512):
            with self.subTest(size=size):
                self.assertGreaterEqual(
                    estimate_docx_size(size) * 1.1, self._actual(os.urandom(size))
                )

    def test_large_payload_ratio_is_near_the_documented_figure(self) -> None:
        payload = os.urandom(1024 * 1024)
        ratio = self._actual(payload) / len(payload)
        self.assertAlmostEqual(ratio, 1.36, delta=0.06)


if __name__ == "__main__":
    unittest.main()
