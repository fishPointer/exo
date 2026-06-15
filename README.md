# exo — a shared, append-only thread vault

A small, robust system for a team to hold **conversation threads** that everyone appends to,
from any device, asynchronously, with no server. Cards are immutable and named by the hash of
their content, so concurrent edits from different people merge instead of conflicting.

It's an [Obsidian](https://obsidian.md) vault + a stdlib-only Python backend + a Claude Code
config, all in one repo. Pull it, do the one-time setup, and start posting.

- **The whole model in one screen:** [`_system/ARCHITECTURE.md`](_system/ARCHITECTURE.md)
- **Live status:** [`DASHBOARD.md`](DASHBOARD.md)
- **Changing the apparatus:** [`_system/config/`](_system/config/)

---

## The model in 60 seconds

```
notes/records/<thread>/<id>.md   ← the truth. immutable cards. id = hash(body).
notes/threads/<thread>.md        ← what you read & type in. a rendering of the cards.
```

You type into a **thread** note. You hit **Run**. Your text becomes immutable **cards**.
Obsidian Sync carries the cards to everyone else's device. Because a card's name is its
content hash, two people posting at once can never clobber each other — worst case you both
made new cards, and they both appear.

That's it. The rest is detail.

---

## Setup (once per person, per device)

You need: **Python 3.8+**, **Obsidian**, and — only if you want the AI **Summon** button — an
Anthropic API key and the `claude` CLI.

**1. Get the repo.**
```bash
git clone <this-repo-url> exo
cd exo
```

**2. Open it in Obsidian.** *Open folder as vault* → pick the `exo` folder.
- Enable the bundled plugin: Settings → Community plugins → turn off Restricted/Safe mode →
  enable **Exo Ribbon**. (Five buttons appear in the left ribbon: Run, Scan, Validate,
  Restore, Summon.)
- The card styling snippet (`stream-cards`) is enabled by default.

**3. Turn on sync.** This is how content reaches other devices.
- Settings → Sync → log in → connect to your team's shared remote vault.
- **Important:** in Sync settings, *exclude* the `.stream` folder. It's local daemon state;
  syncing it would make one person's button click fire on everyone's machine.

**4. Verify everything** (no install, no dependencies) — one command checks the whole vault:
```bash
python3 _system/doctor.py             # → ✓ all required checks pass — vault is healthy
```
It runs the golden tests, integrity checks, re-renders every thread, and audits the hook /
plugin / config wiring. Exit 0 = healthy. Add `--fix` (or run `/initialize`) to also **repair**
every safely-fixable problem and report the rest.

**5. (Optional) Start the daemon** so the Obsidian buttons do something:
```bash
python3 _system/watch.py              # leave running while you work; Ctrl-C to stop
```
The daemon only ever acts on an explicit button click. It runs no loop of its own.

**6. (Optional) AI replies — the Summon button.** Put your key outside the vault:
```bash
mkdir -p ~/.config/exo
printf 'ANTHROPIC_API_KEY=sk-ant-...\n' > ~/.config/exo/.env
chmod 600 ~/.config/exo/.env
```
Details and key hygiene: [`_system/config/api-keys.md`](_system/config/api-keys.md).

---

## Daily use

**From Obsidian (the normal way):**
1. Open a thread (start with `notes/threads/main.md`).
2. Type your message **between cards** or below the `---` separator at the bottom.
3. Click **Run** (▶). Your text becomes a card; the thread re-renders.

**The five buttons:**

| | button | does |
|---|---|---|
| ▶ | **Run** | reconcile *this* thread — your typed text → cards, edited card bodies restored, re-render |
| 🧠 | **Scan** | flag every thread that has unsaved drift (writes `DASHBOARD.md`'s worklist) |
| ⚛ | **Validate** | check every card still hashes to its name and reply links resolve |
| 🧪 | **Restore** | rebuild *this* view from its records — discards unsaved edits, fixes a sync conflict |
| ⚡ | **Summon** | fire **one** AI reply (your click only; needs an API key) |

**From the command line (works with no daemon, no Obsidian):**
```bash
# post a card
echo "hello team" | python3 _system/stream.py record --author fish --view notes/threads/main.md

# reconcile / inspect
python3 _system/stream.py run      --view notes/threads/main.md
python3 _system/stream.py validate
python3 _system/stream.py render   --view notes/threads/main.md --write   # = Restore
```

**A new thread:** create `notes/threads/<name>.md` with this frontmatter, then post to it
with `--view notes/threads/<name>.md`:
```yaml
---
type: stream
status: live
title: <name>
---
```

---

## How sync stays clean (the important part)

- **Git** ships the *apparatus* (code, configs, plugin). You only `git pull` when the tooling
  changes.
- **Obsidian Sync** ships the *content* (threads + records). This is the live, second-to-second
  channel. The repo itself stays empty — content is `.gitignore`d.
- **Conflicts can't lose data.** Cards are content-addressed, so concurrent posts merge as a
  set union. A thread *view* can conflict (two people typed into `main.md` at once) — but the
  view is derived, so just hit **Restore** and it rebuilds identically from the cards. You
  never lose a card to a view conflict.

Full reasoning: [`_system/ARCHITECTURE.md`](_system/ARCHITECTURE.md) §5.

---

## For the AI agent

If you're an agent working here, read [`.claude/CLAUDE.md`](.claude/CLAUDE.md). The short
version: **records are the truth; to post, pipe your reply through `stream.py record` once,
and the echoed card IS your message** — never hand-edit a card body, never write a prose copy
of what you just recorded.

---

## Layout

```
exo/
├── README.md              ← you are here (setup + daily use)
├── DASHBOARD.md           ← live status: daemon, dirty threads, reply debt (auto-generated)
├── .claude/               ← Claude Code config
│   ├── CLAUDE.md          ←   agent operating contract
│   ├── settings.json      ←   the capture hook
│   └── skills/stream/     ←   the /stream skill (+ template for more)
├── .obsidian/             ← editor config, the exo-ribbon plugin, the card-styling snippet
├── _system/               ← THE APPARATUS
│   ├── ARCHITECTURE.md    ←   the design, the contract, the failure modes
│   ├── stream.py          ←   the deterministic core (the product)
│   ├── watch.py           ←   the optional daemon (buttons → backend)
│   ├── capture_prompt.py  ←   the optional Claude Code capture hook
│   ├── doctor.py          ←   health check (verifies everything; the /initialize skill)
│   ├── test_golden.py     ←   the tests that must never go red
│   └── config/            ←   how to manage settings, skills, API keys, CSS
└── notes/                 ← CONTENT (synced via Obsidian, not git)
    ├── threads/main.md    ←   the seed thread (empty)
    └── records/           ←   the immutable card store, one subdir per thread
```

---

## Troubleshooting

- **First stop for anything:** `python3 _system/doctor.py --fix` (or `/initialize`). It repairs
  every safely-fixable fault (config wiring, a flipped safety flag, a drifted thread) and tells
  you exactly what's left and how to fix it.
- **Buttons do nothing.** The daemon isn't running. `python3 _system/watch.py`.
- **A card shows as a plain gray box.** The `stream-cards` CSS snippet is off, or the author
  has no colour — see [`_system/config/css.md`](_system/config/css.md).
- **`validate` says INVALID.** A card body was hand-edited. **Restore** the thread (it rebuilds
  from records), or fix/remove the offending record file under `notes/records/<thread>/`.
- **Thread looks wrong after a sync.** Hit **Restore** — it rebuilds the view from the merged
  records.
- **Summon fails.** No key (`~/.config/exo/.env`) or no `claude` CLI. Everything else still
  works without it.
