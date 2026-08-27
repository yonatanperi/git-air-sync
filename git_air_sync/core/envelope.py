"""Framing for the payload carried inside the .docx.

The codec layer has no magic bytes, no version, and no checksum, so corruption there
is silent. This module wraps the git bundle in a header that makes every failure mode
detectable and nameable:

    offset 0    : 10 bytes   b"GITAIRSYNC"           magic
    offset 10   :  2 bytes   uint16 envelope version
    offset 12   :  4 bytes   uint32 header length N
    offset 16   :  N bytes   UTF-8 JSON header
    offset 16+N :  rest      raw git bundle bytes

The JSON is serialised deterministically (sorted keys, no whitespace) so identical
inputs produce byte-identical documents.
"""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import asdict, dataclass, field
from typing import Any

from ..errors import PayloadError

MAGIC = b"GITAIRSYNC"
ENVELOPE_VERSION = 1
_HEADER_STRUCT = struct.Struct(">HI")  # version, header length
_PREFIX_LEN = len(MAGIC) + _HEADER_STRUCT.size
_MAX_HEADER_LEN = 64 * 1024


class EnvelopeError(PayloadError):
    """Base for every framing failure."""


class MagicError(EnvelopeError):
    """The blob does not start with our magic bytes."""


class VersionError(EnvelopeError):
    """The envelope version is newer than this tool understands."""


class HeaderError(EnvelopeError):
    """The header is truncated, oversized, or not valid JSON."""


class ChecksumError(EnvelopeError):
    """The payload does not match the length or digest recorded in the header."""


CORRUPTION_ADVICE = (
    "The document was almost certainly opened and re-saved by Word, or reflowed by a "
    "mail client — that rewrites the paragraphs the encoding depends on. Re-export on "
    "Computer A and transfer the file without opening it."
)


@dataclass(frozen=True)
class Envelope:
    """Everything Computer B needs to know about a payload before touching git."""

    project: str
    source_branch: str
    bundle_mode: str  # "full" | "incremental"
    head_sha: str
    commit_count: int
    created_at: str
    created_by: str
    payload_size: int
    payload_sha256: str
    tool_version: str
    base_sha: str | None = None
    hash_algo: str = "sha1"
    envelope_version: int = ENVELOPE_VERSION
    # Reserved so multi-part splitting can ship later without a format break.
    part_index: int = 1
    part_count: int = 1
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_incremental(self) -> bool:
        return self.bundle_mode == "incremental"

    def short(self, sha: str | None) -> str:
        return sha[:7] if sha else "—"


_REQUIRED = (
    "project",
    "source_branch",
    "bundle_mode",
    "head_sha",
    "commit_count",
    "created_at",
    "created_by",
    "payload_size",
    "payload_sha256",
    "tool_version",
)


def wrap(bundle: bytes, meta: Envelope) -> bytes:
    """Frame ``bundle`` with ``meta``. The digest fields in ``meta`` are recomputed."""
    if not bundle:
        raise EnvelopeError("refusing to wrap an empty bundle")

    payload = dict(asdict(meta))
    payload.pop("extra", None)
    payload.update(meta.extra)
    payload["tool"] = "git-air-sync"
    payload["payload_size"] = len(bundle)
    payload["payload_sha256"] = hashlib.sha256(bundle).hexdigest()

    header = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")

    return (
        MAGIC
        + _HEADER_STRUCT.pack(meta.envelope_version, len(header))
        + header
        + bundle
    )


def peek(blob: bytes) -> Envelope:
    """Parse the header only — no payload hashing.

    Lets Computer B report "this needs base commit abc1234, which you don't have"
    without first paying to hash an 80 MB payload.
    """
    envelope, _ = _split(blob, verify=False)
    return envelope


def unwrap(blob: bytes) -> tuple[Envelope, bytes]:
    """Parse and fully validate. Returns ``(envelope, bundle_bytes)``."""
    return _split(blob, verify=True)


def _split(blob: bytes, *, verify: bool) -> tuple[Envelope, bytes]:
    if len(blob) < _PREFIX_LEN:
        raise MagicError(
            "This document is too small to be a git-air-sync package. " + CORRUPTION_ADVICE
        )

    if not blob.startswith(MAGIC):
        # Getting this far means every paragraph decoded as a valid numeric chunk, so
        # the file really was produced by this encoding. A corrupted chunk shifts all
        # subsequent bytes, which lands here rather than at the checksum — so
        # corruption is by far the likeliest cause, not a wrongly chosen file.
        raise MagicError(
            "This package's contents are corrupted.\n\n" + CORRUPTION_ADVICE + "\n\n"
            "(If the file was never opened, check you picked the right document — an "
            "unrelated file encoded with the same tool would also look like this.)"
        )

    version, header_len = _HEADER_STRUCT.unpack(
        blob[len(MAGIC) : _PREFIX_LEN]
    )

    if version > ENVELOPE_VERSION:
        raise VersionError(
            f"This package uses envelope format v{version}, but this copy of "
            f"git-air-sync only understands v{ENVELOPE_VERSION}. Update git-air-sync "
            "on this machine."
        )

    if header_len > _MAX_HEADER_LEN or _PREFIX_LEN + header_len > len(blob):
        raise HeaderError("The package header is truncated or corrupt. " + CORRUPTION_ADVICE)

    raw_header = blob[_PREFIX_LEN : _PREFIX_LEN + header_len]
    payload = blob[_PREFIX_LEN + header_len :]

    try:
        header = json.loads(raw_header.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HeaderError("The package header is not readable. " + CORRUPTION_ADVICE) from exc

    missing = [k for k in _REQUIRED if k not in header]
    if missing:
        raise HeaderError(
            "The package header is missing required fields: " + ", ".join(missing)
        )

    if verify:
        if len(payload) != header["payload_size"]:
            raise ChecksumError(
                f"The package is {len(payload):,} bytes but its header says it should "
                f"be {header['payload_size']:,}. " + CORRUPTION_ADVICE
            )
        actual = hashlib.sha256(payload).hexdigest()
        if actual != header["payload_sha256"]:
            raise ChecksumError(
                "The package failed its integrity check.\n"
                f"  expected sha256 {header['payload_sha256'][:16]}…\n"
                f"  actual   sha256 {actual[:16]}…\n" + CORRUPTION_ADVICE
            )

    known = {f for f in Envelope.__dataclass_fields__ if f != "extra"}
    extra = {k: v for k, v in header.items() if k not in known and k != "tool"}
    envelope = Envelope(
        **{k: v for k, v in header.items() if k in known},
        extra=extra,
    )
    return envelope, payload


def suggested_filename(meta: Envelope) -> str:
    """`alpha__a1b2c3d-e4f5g6h__20260826-153000.docx`

    Double underscores separate the fields so project names containing a single
    hyphen or underscore stay unambiguous. The filename is a convenience only — the
    envelope inside is the sole source of truth.
    """
    base = meta.base_sha[:7] if meta.base_sha else "full"
    stamp = meta.created_at.replace("-", "").replace(":", "")
    stamp = stamp.replace("T", "-").replace("Z", "")[:15]
    safe_project = "".join(c if c.isalnum() or c in "-_" else "_" for c in meta.project)
    return f"{safe_project}__{base}-{meta.head_sha[:7]}__{stamp}.docx"
