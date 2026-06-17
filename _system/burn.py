#!/usr/bin/env python3
"""
burn.py — factory-reset the stream vault back to its empty seed scaffold.

Wipes ALL content — every card under `_system/records/` and every thread view
under `notes/` — and resets the local `.stream/` drift state and the
dashboard. It touches NOTHING else: the apparatus (`_system/`, `.claude/`,
`.obsidian/`) and every per-machine config are left exactly as they are. This is
the de-expression of the vault back to a fresh clone's content-state.

WHY THIS EXISTS, GIVEN ARCHITECTURE §6 ("no record-level deletion verb; removal
is a deliberate manual act"). burn is not a casual verb on the spine — it is a
gated, deliberate operator act (the `/burn` skill, `--yes` required), with a
local backup taken first. The spine (`stream.py`) stays append-only and pure;
this is a separate tool, the way `/initialize` is `doctor.py`, not a stream verb.

THE SAFETY NET IS A LOCAL BACKUP, NOT GIT. exo's content lives in Obsidian Sync,
not git (`.gitignore` keeps `_system/records/*` + `notes/*` out; only the seed
`notes/main.md` is tracked) — so there is nothing in git to recover. Before
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
RECORDS_ROOT = stream.RECORDS_ROOT          # _system/records/<thread>/  (LEGACY store; migrated -> pool)
CARDS_DIR = stream.CARDS_DIR                # _system/data/cards/  (the global card POOL — the truth)
THREADS_DIR = stream.THREADS_DIR            # _system/data/threads/  (thread MANIFESTS: inclusion + order)
THREAD_DIR = stream.THREAD_DIR              # notes/  (the thread views, flat)
DEFAULT_VIEW = stream.DEFAULT_VIEW          # notes/main.md  (the tracked seed thread)
STREAM_DIR = stream.STREAM_DIR              # .stream/  (local, gitignored, unsynced)
CHANGESETS_DIR = stream.CHANGESETS_DIR
DIRTY_INDEX = stream.DIRTY_INDEX
DASHBOARD = stream.DASHBOARD
BURNS_DIR = STREAM_DIR / "burns"

GITKEEP = ".gitkeep"
SEED_VIEW_NAME = "main.md"                  # the one thread a fresh clone ships


# ── survey ────────────────────────────────────────────────────────────────────

def _pool_count() -> int:
    return len(list(CARDS_DIR.glob("*.md"))) if CARDS_DIR.exists() else 0


def _manifests() -> list[pathlib.Path]:
    return sorted(THREADS_DIR.glob("*.md")) if THREADS_DIR.exists() else []


def _legacy_dirs() -> list[pathlib.Path]:
    return sorted(d for d in RECORDS_ROOT.glob("*") if d.is_dir()) if RECORDS_ROOT.exists() else []


def _legacy_count() -> int:
    return sum(len(list(d.glob("*.md"))) for d in _legacy_dirs())


def _thread_views() -> list[pathlib.Path]:
    return sorted(p for p in THREAD_DIR.glob("*.md")) if THREAD_DIR.exists() else []


def _survey() -> dict:
    views = _thread_views()
    return {
        "cards": _pool_count(),                       # the global pool — the source of truth
        "manifests": len(_manifests()),               # thread manifests (inclusion + order)
        "legacy": _legacy_count(),                    # pre-migration records/<thread>/ backup
        "views": views,
        "extra_views": [v for v in views if v.name != SEED_VIEW_NAME],
    }


def _is_empty(s: dict) -> bool:
    # Empty = a fresh clone: empty pool, no manifests, no legacy records, no views beyond the seed.
    return s["cards"] == 0 and s["manifests"] == 0 and s["legacy"] == 0 and not s["extra_views"]


# ── backup + wipe ─────────────────────────────────────────────────────────────

def _backup(ts: str) -> pathlib.Path:
    dest = BURNS_DIR / ts
    dest.mkdir(parents=True, exist_ok=True)
    for src, name in ((CARDS_DIR, "cards"), (THREADS_DIR, "threads"),
                      (RECORDS_ROOT, "records"), (THREAD_DIR, "views")):
        if src.exists():
            shutil.copytree(src, dest / name, dirs_exist_ok=True)
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
    """Leave the content trees as a fresh clone would: an empty pool and no manifests (.gitkeep only),
    the legacy record store empty, and threads = .gitkeep + an EMPTY seed main.md (its frontmatter kept,
    body rebuilt from the now-empty record set via the spine's own renderer)."""
    main_fm = stream._clean_fm_block(DEFAULT_VIEW)          # capture before wiping
    _wipe_contents(CARDS_DIR)                               # the pool — every card gone
    _wipe_contents(THREADS_DIR)                             # every thread manifest gone
    _wipe_contents(RECORDS_ROOT)                            # legacy backup store gone
    for v in _thread_views():                              # delete every view but the seed main.md
        if v.name != SEED_VIEW_NAME:
            v.unlink()
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
    print(f"burn — stream vault reset @ {_rel(CARDS_DIR)} + {_rel(THREADS_DIR)} + {_rel(THREAD_DIR)}")
    print(f"  pool    : {s['cards']} card(s) in the global pool"
          + (f"  (+ {s['legacy']} legacy record(s))" if s['legacy'] else ""))
    print(f"  threads : {s['manifests']} manifest(s)")
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
    print(f"wiped the pool   → {_rel(CARDS_DIR)}/.gitkeep")
    print(f"wiped manifests  → {_rel(THREADS_DIR)}/.gitkeep")
    print(f"wiped legacy     → {_rel(RECORDS_ROOT)}/.gitkeep")
    print(f"reset threads    → {_rel(THREAD_DIR)}/ (empty {SEED_VIEW_NAME})")
    print("cleared .stream drift state; regenerated DASHBOARD.md")
    print("\nvault reset to the empty seed scaffold.")
    if backup:
        print("recover the prior content with:")
        print(f"  cp -r {_rel(backup)}/cards/* {_rel(CARDS_DIR)}/ && "
              f"cp -r {_rel(backup)}/threads/* {_rel(THREADS_DIR)}/ && "
              f"python3 _system/stream.py validate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
