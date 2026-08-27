"""Envelope framing: round-trip and every corruption path."""

from __future__ import annotations

import os
import unittest

from git_air_sync.core.envelope import (
    ChecksumError,
    Envelope,
    HeaderError,
    MagicError,
    VersionError,
    peek,
    suggested_filename,
    unwrap,
    wrap,
)


def make_meta(**overrides) -> Envelope:
    base = dict(
        project="alpha",
        source_branch="main",
        bundle_mode="incremental",
        base_sha="a" * 40,
        head_sha="b" * 40,
        commit_count=3,
        created_at="2026-08-26T15:00:00Z",
        created_by="hostA",
        payload_size=0,
        payload_sha256="",
        tool_version="0.1.0",
    )
    base.update(overrides)
    return Envelope(**base)


class RoundTrip(unittest.TestCase):
    def test_wrap_then_unwrap(self) -> None:
        bundle = os.urandom(4096)
        blob = wrap(bundle, make_meta())
        meta, recovered = unwrap(blob)
        self.assertEqual(recovered, bundle)
        self.assertEqual(meta.project, "alpha")
        self.assertEqual(meta.source_branch, "main")
        self.assertEqual(meta.commit_count, 3)
        self.assertEqual(meta.payload_size, len(bundle))

    def test_digest_is_computed_by_wrap(self) -> None:
        import hashlib

        bundle = os.urandom(1000)
        meta, _ = unwrap(wrap(bundle, make_meta()))
        self.assertEqual(meta.payload_sha256, hashlib.sha256(bundle).hexdigest())

    def test_serialisation_is_deterministic(self) -> None:
        bundle = b"identical payload"
        self.assertEqual(wrap(bundle, make_meta()), wrap(bundle, make_meta()))

    def test_peek_skips_payload_validation(self) -> None:
        blob = wrap(os.urandom(2048), make_meta())
        truncated = blob[: len(blob) // 2]
        # peek must still work on a partial blob; unwrap must not.
        self.assertEqual(peek(truncated).project, "alpha")
        with self.assertRaises(ChecksumError):
            unwrap(truncated)

    def test_full_mode_has_no_base(self) -> None:
        meta, _ = unwrap(wrap(b"x" * 100, make_meta(bundle_mode="full", base_sha=None)))
        self.assertIsNone(meta.base_sha)
        self.assertFalse(meta.is_incremental)

    def test_empty_bundle_is_refused(self) -> None:
        with self.assertRaises(Exception):
            wrap(b"", make_meta())

    def test_unknown_header_keys_are_preserved(self) -> None:
        meta = make_meta()
        object.__setattr__(meta, "extra", {"future_field": 42})
        recovered, _ = unwrap(wrap(b"payload", meta))
        self.assertEqual(recovered.extra.get("future_field"), 42)


class Corruption(unittest.TestCase):
    def setUp(self) -> None:
        self.bundle = os.urandom(4096)
        self.blob = wrap(self.bundle, make_meta())

    def test_bad_magic(self) -> None:
        with self.assertRaises(MagicError):
            unwrap(b"NOTOURDATA" + self.blob[10:])

    def test_too_short(self) -> None:
        with self.assertRaises(MagicError):
            unwrap(b"GIT")

    def test_future_version_is_named(self) -> None:
        broken = bytearray(self.blob)
        broken[10:12] = (99).to_bytes(2, "big")
        with self.assertRaises(VersionError):
            unwrap(bytes(broken))

    def test_absurd_header_length(self) -> None:
        broken = bytearray(self.blob)
        broken[12:16] = (10_000_000).to_bytes(4, "big")
        with self.assertRaises(HeaderError):
            unwrap(bytes(broken))

    def test_unparseable_header(self) -> None:
        broken = bytearray(self.blob)
        broken[20] = broken[20] ^ 0xFF
        with self.assertRaises((HeaderError, ChecksumError)):
            unwrap(bytes(broken))

    def test_single_flipped_payload_byte(self) -> None:
        broken = bytearray(self.blob)
        broken[-1] ^= 0x01
        with self.assertRaises(ChecksumError) as caught:
            unwrap(bytes(broken))
        # The message must point at the real-world cause, not just say "bad hash".
        self.assertIn("Word", str(caught.exception))

    def test_truncated_payload(self) -> None:
        with self.assertRaises(ChecksumError):
            unwrap(self.blob[:-100])

    def test_appended_payload(self) -> None:
        with self.assertRaises(ChecksumError):
            unwrap(self.blob + b"extra")


class Filenames(unittest.TestCase):
    def test_incremental_filename(self) -> None:
        name = suggested_filename(make_meta())
        self.assertTrue(name.startswith("alpha__aaaaaaa-bbbbbbb__"))
        self.assertTrue(name.endswith(".docx"))

    def test_full_filename_says_full(self) -> None:
        name = suggested_filename(make_meta(bundle_mode="full", base_sha=None))
        self.assertIn("__full-", name)

    def test_awkward_project_name_is_made_safe(self) -> None:
        name = suggested_filename(make_meta(project="my/weird proj"))
        self.assertNotIn("/", name)
        self.assertNotIn(" ", name)


if __name__ == "__main__":
    unittest.main()
