# `_system/config/` — the self-management surface

Short guides for changing the apparatus. Written for both the human operator and the
agent. Each one is the authoritative how-to for one thing you might want to change.

| file | when you reach for it |
|---|---|
| [`settings.md`](settings.md) | change Claude Code behaviour — the capture hook, permissions |
| [`skills.md`](skills.md) | add or edit a `/skill` (a reusable command for humans + agents) |
| [`api-keys.md`](api-keys.md) | set the API key the `Summon` button uses; key hygiene |
| [`css.md`](css.md) | restyle thread cards / add an author colour (Obsidian CSS snippets) |

Ground rules:

- **The code is the spec.** These docs describe `_system/stream.py`, `_system/watch.py`,
  `.claude/`, and `.obsidian/`. If a doc and the code disagree, the code wins — fix the doc.
- **Apparatus changes are git changes.** Editing tooling/config means a commit other people
  pull. Editing thread *content* does not — that flows over Obsidian Sync (see the root
  `README.md`). Keep the two straight.
- **Nothing here needs a build step.** Pure stdlib Python + plain JSON/CSS/Markdown.
