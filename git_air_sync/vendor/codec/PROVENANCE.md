# Vendored: `txt-codec`

| | |
|---|---|
| **Upstream** | `/Users/yonatanperi/Desktop/projects/txt-codec` |
| **Upstream commit** | `4375893` — *fix: drive folder-encode exclusions from .gitignore instead of a hardcoded list* |
| **Vendored on** | 2026-08-26 |
| **Files taken** | `codec/__init__.py`, `codec/encoder.py`, `codec/decoder.py` — **verbatim** |
| **Files deliberately NOT taken** | `codec/__main__.py` |
| **Dependencies** | none (Python stdlib only: `os`, `re`, `io`, `zipfile`, `xml.etree`) |

## Why vendored

Upstream has no `pyproject.toml`, `setup.py`, or version string, so it cannot be
pip-installed. Computer B is air-gapped and cannot fetch it. The code is stdlib-only
and ~270 lines, so copying it makes this repo self-contained.

## Why `__main__.py` was excluded

It calls `main()` at module top level, outside any `if __name__ == "__main__"` guard.
Importing it would execute the CLI — including `sys.exit()` — as a side effect of the
import. It provides nothing we need.

## What we call, and what we avoid

`git_air_sync/core/codec.py` is the only module allowed to touch this package. It uses:

- `Encoder._encode_text(bytes) -> str`
- `Encoder._write_docx(text, file_like)` — accepts a `BytesIO`, not just a path
- `Decoder._docx_to_text(file_like) -> str` — likewise accepts a `BytesIO`

It deliberately does **not** call `Encoder.encode()` or `Decoder.decode()`:

- `encode()` writes both a `.txt` and a `.docx` next to the input, with no way to
  control the output path.
- `decode()` routes through `_decode_auto`, which calls `zipfile.is_zipfile()` on the
  decoded bytes and **extracts to a directory** if it matches. Our payload is a git
  bundle (not a zip), but by never entering that branch the behaviour is structurally
  unreachable rather than merely unlikely.
- Both report failure by `print()`ing and returning normally, so neither raises nor
  sets a non-zero exit status on a missing input path.

`core/codec.py` also reimplements the seven-line integer-parsing loop from
`Decoder._txt_to_bytes` rather than calling it, because that method requires a real
filesystem path with a `.txt`/`.docx` suffix and cannot report *which* paragraph failed.

## Wire format (do not change without a version bump)

Input bytes are split into 1024-byte chunks. Each chunk is prefixed with `\x01` — this
is what preserves leading zero bytes — read as a big-endian integer, rendered as
decimal digits, and grouped in fives separated by spaces. Each chunk becomes one
`<w:p>` paragraph in a hand-built three-entry OOXML zip.

`tests/test_codec.py` pins the exact SHA-256 of the `.docx` produced from a fixed
input. **If you re-vendor from upstream and that test fails, the format changed and
every previously produced `.docx` has become unreadable.** Treat it as a breaking
change, not a test to update.

## Known constraints

- A 1024-byte chunk yields 2467 decimal digits, just under CPython's default
  `sys.get_int_max_str_digits()` limit of 4300. Raising `CHUNK_SIZE` above ~1785 would
  start raising `ValueError`.
- An empty payload produces empty text, which fails on decode with
  `int('')`. `core/codec.py` guards this on both sides.
- Chunk boundaries are paragraph boundaries, so any editor or mail client that reflows,
  splits, or merges paragraphs corrupts the payload irrecoverably.
