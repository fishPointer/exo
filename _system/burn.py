#!/usr/bin/env python3
"""
burn.py — factory-reset the stream vault back to its empty seed scaffold.

Wipes ALL content — every card under `notes/records/` and every thread view
under `notes/threads/` — and resets the local `.stream/` drift state and the
dashboard. It touches NOTHING else: the apparatus (`_system/`, `.claude/`,
`.obsidian/`) and every per-machine config are left exactly as they are. This is
the de-expression of the vault back to a fresh clone's content-state.

WHY THIS EXISTS, GIVEN ARCHITECTURE §6 ("no record-level deletion verb; removal
is a deliberate manual act"). burn is not a casual verb on the spine — it is a
gated, deliberate operator act (the `/burn` skill, `--yes` required), with a
local backup taken first. The spine (`stream.py`) stays append-only and pure;
this is a separate tool, the way `/initialize` is `doctor.py`, not a stream verb.

THE SAFETY NET IS A LOCAL BACKUP, NOT GIT. exo's content lives in Obsidian Sync,
not git (`.gitignore` keeps `notes/records/*` + `notes/threads/*` out; only the
seed `main.md` is tracked) — so there is nothing in git to recover. Before
wiping, burn copies records + threads into `.stream/burns/<ts>/` (gitignored,
never synced, untouched by future burns). Restore is a plain `cp` back.

Usage:
  burn.py                 # report what would be wiped; do nothing (= --dry-run)
  burn.py --dry-run       # same
  burn.py --yes           # back up, then wipe to the seed scaffold
  burn.py --yes --no-backup    # wipe with NO local backup (recovery is your problem)
"""
from __future__ import annotations

import argparse
import pathlib
import shutil
import sys
from datetime import datetime

# stream.py is the spine — reuse its path constants and pure renderers rather
# than re-deriving them (single source of truth for where things live).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import stream  # noqa: E402

ROOT = stream.ROOT
RECORDS_ROOT = stream.RECORDS_ROOT          # notes/records/<thread>/
THREAD_DIR = stream.THREAD_DIR              # notes/threads/<thread>.md
DEFAULT_VIEW = stream.DEFAULT_VIEW          # notes/threads/main.md  (the tracked seed)
STREAM_DIR = stream.STREAM_DIR              # .stream/  (local, gitignored, unsynced)
CHANGESETS_DIR = stream.CHANGESETS_DIR
DIRTY_INDEX = stream.DIRTY_INDEX
DASHBOARD = stream.DASHBOARD
BURNS_DIR = STREAM_DIR / "burns"

GITKEEP = ".gitkeep"
SEED_VIEW_NAME = "main.md"                  # the one thread a fresh clone ships


# ── survey ────────────────────────────────────────────────────────────────────

def _record_dirs() -> list[pathlib.Path]:
    return sorted(d for d in RECORDS_ROOT.glob("*") if d.is_dir()) if RECORDS_ROOT.exists() else []


def _record_count() -> int:
    return sum(len(list(d.glob("*.md"))) for d in _record_dirs())


def _thread_views() -> list[pathlib.Path]:
    return sorted(p for p in THREAD_DIR.glob("*.md")) if THREAD_DIR.exists() else []


def _survey() -> dict:
    views = _thread_views()
    return {
        "threads": len(_record_dirs()),
        "records": _record_count(),
        "views": views,
        "extra_views": [v for v in views if v.name != SEED_VIEW_NAME],
    }


def _is_empty(s: dict) -> bool:
    # Empty = a fresh clone: no records, no thread views beyond the seed main.md.
    return s["records"] == 0 and not s["extra_views"]


# ── backup + wipe ─────────────────────────────────────────────────────────────

def _backup(ts: str) -> pathlib.Path:
    dest = BURNS_DIR / ts
    dest.mkdir(parents=True, exist_ok=True)
    if RECORDS_ROOT.exists():
        shutil.copytree(RECORDS_ROOT, dest / "records", dirs_exist_ok=True)
    if THREAD_DIR.exists():
        shutil.copytree(THREAD_DIR, dest / "threads", dirs_exist_ok=True)
    return dest


def _wipe_contents(root: pathlib.Path, keep: tuple[str, ...] = (GITKEEP,)) -> int:
    root.mkdir(parents=True, exist_ok=True)
    n = 0
    for child in sorted(root.iterdir()):
        if child.name in keep:
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
        n += 1
    (root / GITKEEP).touch()
    return n


def _reset_to_seed() -> None:
    """Leave the content trees as a fresh clone would: records empty (.gitkeep
    only); threads = .gitkeep + an EMPTY seed main.md (its frontmatter kept, body
    rebuilt from the now-empty record set via the spine's own renderer)."""
    main_fm = stream._clean_fm_block(DEFAULT_VIEW)          # capture before wiping
    _wipe_contents(RECORDS_ROOT)                            # all cards gone
    for v in _thread_views():                              # all views but main + .gitkeep
        if v.name != SEED_VIEW_NAME:
            v.unlink()
    (THREAD_DIR / GITKEEP).touch()
    DEFAULT_VIEW.write_text(stream.render_view({}, main_fm), encoding="utf-8")

    # local drift state that indexed the (now-deleted) threads
    if CHANGESETS_DIR.exists():
        shutil.rmtree(CHANGESETS_DIR)
    DIRTY_INDEX.unlink(missing_ok=True)
    (STREAM_DIR / "result.json").unlink(missing_ok=True)
    (STREAM_DIR / "summon-inflight.json").unlink(missing_ok=True)

    DASHBOARD.write_text(stream.render_dashboard(), encoding="utf-8")


# ── command ───────────────────────────────────────────────────────────────────

def _rel(p: pathlib.Path) -> str:
    return str(p.relative_to(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="burn.py",
        description="Factory-reset the stream vault: wipe all cards + threads "
                    "back to the empty seed scaffold. Apparatus + local config "
                    "untouched. Safety net: a local backup under .stream/burns/.",
    )
    ap.add_argument("--yes", action="store_true", help="actually wipe (default is a dry-run)")
    ap.add_argument("--dry-run", action="store_true", help="report only; the default")
    ap.add_argument("--no-backup", action="store_true",
                    help="skip the local pre-burn backup (no recovery point)")
    args = ap.parse_args()

    s = _survey()
    print(f"burn — stream vault reset @ {_rel(RECORDS_ROOT)} + {_rel(THREAD_DIR)}")
    print(f"  cards   : {s['records']} record(s) across {s['threads']} thread(s)")
    print(f"  views   : {len(s['views'])} thread view(s)"
          + (f" ({len(s['extra_views'])} beyond the seed main.md)" if s['extra_views'] else ""))

    if _is_empty(s):
        print("\nalready at the empty seed scaffold — nothing to burn.")
        return 0

    if not args.yes:
        print("\n[dry-run] would wipe both trees back to the seed scaffold "
              "(empty records; threads = .gitkeep + an empty main.md).")
        if not args.no_backup:
            print(f"          a local backup would be taken first → {_rel(BURNS_DIR)}/<ts>/")
        print("\nre-run with --yes to commit the burn.")
        return 0

    backup = None
    if not args.no_backup:
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        backup = _backup(ts)
        print(f"\nbacked up records + threads → {_rel(backup)}")
    else:
        print("\n--no-backup: skipping the local backup (no recovery point).")

    _reset_to_seed()
    print(f"wiped all cards → {_rel(RECORDS_ROOT)}/.gitkeep")
    print(f"reset threads  → {_rel(THREAD_DIR)} (.gitkeep + empty {SEED_VIEW_NAME})")
    print("cleared .stream drift state; regenerated DASHBOARD.md")
    print("\nvault reset to the empty seed scaffold.")
    if backup:
        print("recover the prior content with:")
        print(f"  cp -r {_rel(backup)}/records/* {_rel(RECORDS_ROOT)}/ && "
              f"cp -r {_rel(backup)}/threads/* {_rel(THREAD_DIR)}/ && "
              f"python3 _system/stream.py scan")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
