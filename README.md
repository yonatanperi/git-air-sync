# git-air-sync

Move **real git history** onto an air-gapped machine inside a `.docx` file.

Copying files by hand loses commit history, authorship, and branch structure.
`git-air-sync` bundles commits with native `git bundle`, encodes the bundle into a Word
document, and on the other side decodes it and merges with native `git merge` — so
history, authors, dates, and conflict resolution all behave exactly as git normally does.

```
Computer A (online)                          Computer B (air-gapped)
─────────────────────                        ───────────────────────
git bundle create        ──┐
        ↓                  │  alpha__a1b2c3d-e4f5g6h__20260826.docx
   envelope + sha256       ├──────────────────────────►  decode + verify
        ↓                  │                                   ↓
   encode to .docx       ──┘                            git fetch → git merge
```

## The one rule

> **Never open the `.docx`.** Transfer it, don't inspect it.

The payload is stored as one paragraph per 1 KB chunk. Anything that reflows, splits,
or merges paragraphs — Word saving the file, a mail client "cleaning up" an attachment —
destroys it irrecoverably. Every package carries a SHA-256, so a corrupted file is
*detected* with a clear message rather than silently producing a broken repo. It cannot
be repaired.

## Install

**On Computer A (has internet):**

```bash
pip install -e .
pip install -r requirements-optional.txt   # rich + questionary, for the nice UI
bash vendor/fetch_wheels.sh                # download wheels for the air-gapped machine
```

**On Computer B (air-gapped):** copy the whole repository across, then:

```bash
bash vendor/install_offline.sh
```

That installs from `vendor/wheels/` with `--no-index`, so it never reaches for the
network. If the optional packages can't be installed, the tool still runs — it falls
back to plain-text output and numbered menus. `git-air-sync doctor` reports what's
available.

Requires Python 3.10+ and git 2.38+ (older git works, but loses merge previews).

## Use

```bash
git-air-sync                 # interactive menu
git-air-sync export          # A: bundle new commits into a .docx
git-air-sync import          # B: decode a .docx and merge it
git-air-sync status          # what has crossed the gap
git-air-sync resolve         # finish an import that hit conflicts
git-air-sync config          # settings and sync positions
git-air-sync doctor          # check this machine
```

First run walks you through a short setup wizard.

### Export (Computer A)

Picks a project, works out which commits are new since the last export, warns about
uncommitted work, shows you the commits, and writes the document:

```
  ✓ [1/4] Scanning repository
  ✓ [2/4] Creating bundle
  ✓ [3/4] Encoding payload
  ✓ [4/4] Writing document

╭──────────────────────────────────────────────────────────╮
│ [SUCCESS] Export complete                                │
│                                                          │
│ Project        alpha                                     │
│ Branch         main                                      │
│ Commits        7                                         │
│ Range          a1b2c3d → e4f5g6h                         │
│ Bundle         412 KB                                    │
│ Document       698 KB  (1.69x)                           │
╰──────────────────────────────────────────────────────────╯
```

Useful flags: `--full` (whole history), `--base <sha>` (start elsewhere),
`--out <dir>`, `--yes` (no prompts, for scripts).

### Import (Computer B)

Finds packages in your drop folder, verifies the checksum, checks the bundle applies to
your repo, previews the merge *before* touching your working tree, then merges.

On conflict it stops, tells you exactly which files and what to do, and — importantly —
**does not advance your recorded sync position**, so nothing is lost. Finish with
`git-air-sync resolve` once you've committed the merge.

## Size, speed, and memory

Measured end to end, not estimated:

| Bundle | Document | Ratio | Export | Import |
|--------|----------|-------|--------|--------|
| 386 B | 2.2 KB | 5.8× | instant | instant |
| 45 KB | 64 KB | 1.42× | instant | instant |
| 39 MB | 53 MB | **1.36×** | 29 s | 6 s |

A git bundle is already-compressed packfile data, so the decimal-digit encoding doesn't
compress away — the ratio converges to **1.36×**. Small payloads look far worse only
because ~900 bytes of fixed Word boilerplate dominates them.

**Memory is the real constraint.** Encoding holds the payload in several
representations at once: peak RSS was ~620 MB for a 39 MB bundle, roughly **16×**.
Budget accordingly before syncing a very large repository — a 200 MB bundle would want
well over 3 GB of RAM.

Export warns above 25 MB (configurable via `max_payload_mb`) and checks free disk
space before starting. If your transfer channel caps attachment size, export from a
more recent base commit.

## Things worth knowing

- **Uncommitted changes never travel.** A bundle carries commits. Commit first.
- **Only the current branch syncs** by default. Set `export_refs` to `all` in config to
  include every branch and tag.
- **Rebasing or amending on A invalidates the recorded position**, and recovery is a
  full resync — which for a large repo means a large document. The tool detects this and
  offers you the choice rather than producing a broken package.
- **Export records the sync position optimistically**, the moment the file is written —
  before anyone confirms it reached B. If a package is lost in transit, the next export
  starts *after* the lost commits. The export summary prints the hash it recorded;
  `git-air-sync config` → *Inspect / override sync positions* is the recovery path.
- **B is import-only.** Committing on B is not prevented, but those commits can't travel
  back and will cause conflicts on the next import. `git-air-sync status` warns when it
  spots them.

## Testing

124 tests, stdlib `unittest`, no test dependencies. They pass in four environments —
run at least the first two:

```bash
python3 -m unittest discover -s tests -t .                    # rich + questionary
AIR_SYNC_PLAIN=1 python3 -m unittest discover -s tests -t .   # forced plain path

# and, to prove the real fallback rather than a simulated one:
python3 -m venv /tmp/bare && /tmp/bare/bin/pip install click
PYTHONPATH=. /tmp/bare/bin/python -m unittest discover -s tests -t .
```

`tests/format_pin.txt` records the SHA-256 of a document built from a fixed input. If
`test_digest_matches_the_recorded_pin` ever fails, the codec's wire format changed and
every `.docx` produced before that change is unreadable — treat it as a breaking
change to be versioned, not a file to update.

`tests/test_roundtrip_e2e.py` does a genuine A→B round trip on one machine — two config
files via `AIR_SYNC_CONFIG` stand in for two computers — and covers the failure paths
that matter: corruption, missing prerequisites, conflicts, and bootstrap.

`AIR_SYNC_CONFIG` is a supported feature, not a test hack:

```bash
AIR_SYNC_CONFIG=~/a.json git-air-sync export
AIR_SYNC_CONFIG=~/b.json git-air-sync import
```

## Layout

```
git_air_sync/
├── cli/          rich/questionary live here and nowhere else
│   ├── theme.py      capability detection, palette, icons
│   ├── displays.py   six primitives; everything else composes them
│   ├── prompts.py    questionary wrappers + stdlib fallbacks
│   └── reporter.py   bridges the UI to the UI-free core
├── core/         stdlib only — no click, no rich, no questionary
│   ├── git_ops.py    native git wrapper
│   ├── codec.py      bytes <-> .docx
│   ├── envelope.py   magic + JSON header + sha256
│   └── sync.py       export/import orchestration
├── vendor/codec/ copied from txt-codec (see PROVENANCE.md)
├── config.py
└── main.py
```

`core/` importing nothing beyond the stdlib is what makes the whole pipeline testable
headlessly, and what lets the tool degrade gracefully when optional packages are missing.

## Credits

The `.docx` encoding is [`txt-codec`](../txt-codec), vendored here because it has no
packaging and Computer B can't fetch it. See
`git_air_sync/vendor/codec/PROVENANCE.md` for the exact source commit and the reasons
behind how it's driven.
