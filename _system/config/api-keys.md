# API keys

The vault runs **without any key** for everything except one feature: the **Summon** button
(`⚡`), which fires a single Anthropic API call to draft a reply. Capture, Run, Validate,
Restore, the daemon, Obsidian Sync — none of them need a key.

## Where the key lives

A plain `.env` file, **outside the vault**, never committed:

```
~/.config/exo/.env
```

```
ANTHROPIC_API_KEY=sk-ant-...
```

Lock it down:

```
mkdir -p ~/.config/exo
chmod 600 ~/.config/exo/.env
```

Override the location with the `EXO_ENV` environment variable if you keep secrets elsewhere.

## Why outside the vault

The vault is a shared git repo and a synced Obsidian folder. A key inside it would leak to
every teammate and every device on the first sync. Keeping it in `~/.config/exo/` means each
person/machine holds their own key and it never travels with the content.

The `.gitignore` also blocks `.env`, `*.key`, `*.pem`, and `secrets/` inside the vault as a
backstop — but the rule is simply: **keys never go in the vault.**

## How Summon uses it

`_system/watch.py` reads `ANTHROPIC_API_KEY` from the env file at call time, passes it to a
headless `claude -p` run in the vault (so the agent has this vault's CLAUDE.md + skills),
records the reply as a `claude-api` card, and logs the call to `.stream/api-log.jsonl`.
No key → Summon fails cleanly with a message; nothing else is affected.

## The interactive CLI is different

If you use the Claude Code CLI interactively, it authenticates with your own login — it does
**not** read this file and does not need `ANTHROPIC_API_KEY` exported. Keep the key out of
your shell profile; it's only for the Summon path.
