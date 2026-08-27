"""Bytes <-> .docx, and nothing else.

This is the only module permitted to import ``git_air_sync.vendor.codec``. It drives
the vendored encoder/decoder entirely in memory so that the output path is exact, no
stray ``.txt`` is produced, and the vendored ``decode()`` zip-auto-extract branch is
never reachable. See ``vendor/codec/PROVENANCE.md`` for the reasoning.
"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

from ..errors import PayloadError
from ..vendor.codec.decoder import Decoder
from ..vendor.codec.encoder import Encoder

# Measured against this exact code path on incompressible input (a git bundle is a
# zlib packfile, so the decimal digit stream does not deflate away).
#
#     1 KB -> 2.42x    16 KB -> 1.49x    256 KB -> 1.37x    4 MB -> 1.36x
#
# The ratio converges to ~1.36 once the fixed OOXML boilerplate stops dominating, so
# a single multiplier badly underestimates small payloads. Modelling the constant term
# keeps the estimate within ~3.5% across the whole range.
SIZE_RATIO = 1.40
SIZE_OVERHEAD = 1024

_DOCUMENT_PART = "word/document.xml"


class CodecError(PayloadError):
    """The payload could not be encoded to, or recovered from, a .docx."""


def estimate_docx_size(payload_len: int) -> int:
    """Predicted .docx size, for warning before doing the work."""
    return int(payload_len * SIZE_RATIO) + SIZE_OVERHEAD


def encode_bytes_to_docx(payload: bytes, out_path: Path) -> int:
    """Encode ``payload`` into a .docx at ``out_path``. Returns bytes written.

    The file is written to a sibling ``.part`` and then atomically renamed, so an
    interrupted export never leaves behind a truncated file that still looks importable.
    """
    if not payload:
        raise CodecError("refusing to encode an empty payload")

    encoder = Encoder("")  # the constructor only stores the string; nothing touches disk
    text = encoder._encode_text(payload)

    buf = io.BytesIO()
    encoder._write_docx(text, buf)
    data = buf.getvalue()

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".part")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, out_path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise CodecError(f"could not write {out_path}: {exc}") from exc

    return len(data)


def decode_docx_to_bytes(docx_path: Path) -> bytes:
    """Recover the original payload bytes from a .docx produced by this tool."""
    docx_path = Path(docx_path)
    try:
        data = docx_path.read_bytes()
    except OSError as exc:
        raise CodecError(f"could not read {docx_path}: {exc}") from exc

    if not data:
        raise CodecError(f"{docx_path.name} is empty")

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
    except zipfile.BadZipFile as exc:
        raise CodecError(
            f"{docx_path.name} is not a valid .docx file (not a zip archive). "
            "It may have been truncated in transit or renamed from something else."
        ) from exc

    if _DOCUMENT_PART not in names:
        raise CodecError(
            f"{docx_path.name} is a zip but contains no {_DOCUMENT_PART}, so it is "
            "not a Word document produced by git-air-sync."
        )

    # Decoder's constructor arg is unused on this path; _docx_to_text accepts a
    # file-like object, so no temporary file and no filename-suffix requirement.
    text = Decoder("")._docx_to_text(io.BytesIO(data))
    return _text_to_bytes(text, source=docx_path.name)


def _text_to_bytes(text: str, *, source: str) -> bytes:
    """Reverse the chunk encoding, reporting *which* paragraph went wrong.

    Mirrors the inner loop of the vendored ``Decoder._txt_to_bytes``, which we cannot
    call directly because it insists on a real path with a ``.txt``/``.docx`` suffix
    and reports nothing useful when a chunk is malformed.
    """
    paragraphs = text.split("\n")
    usable = [(i, p.strip()) for i, p in enumerate(paragraphs, start=1) if p.strip()]

    if not usable:
        raise CodecError(f"{source} contains no encoded data")

    out = bytearray()
    for index, para in usable:
        try:
            number = int(para.replace(" ", ""))
        except ValueError:
            raise CodecError(
                f"paragraph {index} of {len(paragraphs)} in {source} is not a valid "
                "encoded chunk. The document was almost certainly opened and re-saved "
                "by Word, or reflowed by a mail client — that rewrites the paragraphs "
                "this encoding depends on. Re-export on Computer A and transfer the "
                "file without opening it."
            ) from None
        raw = number.to_bytes((number.bit_length() + 7) // 8, byteorder="big")
        out.extend(raw[1:])  # drop the \x01 sentinel that preserves leading zeros

    return bytes(out)
