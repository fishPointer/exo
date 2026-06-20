# Authoring CSS profiles (thread card styling)

Thread cards are Obsidian **callouts** keyed by author: `> [!fish] …`, `> [!claude] …`,
`> [!claude-api] …`. Obsidian renders an unknown callout type as a plain gray box, so every
author you use needs a colour. That's what the CSS snippet does.

## The snippet

```
.obsidian/snippets/persona-cards.css
```

It's enabled in `.obsidian/appearance.json`:

```json
{ "enabledCssSnippets": ["persona-cards"] }
```

(Or toggle it in Settings → Appearance → CSS snippets.)

## Add an author colour

Copy a block in `persona-cards.css`, change the name and the hue:

```css
.callout[data-callout="renka"] {
  --callout-color: 210, 100, 150;   /* R, G, B — drives the accent + bar */
  background: rgba(210, 100, 150, 0.06);
}
```

Then add `renka` to the shared shape rules at the top (the comma-lists) so it gets the same
tight card geometry. Record cards with `--author renka` and they'll pick up the style.

## Add a whole new snippet (a "profile")

1. Drop `myprofile.css` in `.obsidian/snippets/`.
2. Add `"myprofile"` to `enabledCssSnippets` in `appearance.json`.
3. Reload snippets (Settings → Appearance → the reload icon) or restart Obsidian.

Snippets are checked in, so a styling change ships to the whole team. Keep them additive and
scoped to `.callout[data-callout="…"]` so they don't fight the user's chosen theme.

## Non-author classes (`nav`, `latex`)

Not every styled callout is a persona. Two **render classes** live in `persona-cards.css` and key off
the same `data-callout` hook, but no card is ever *authored* as one:

- **`nav`** — the chromeless "Jump to Bottom" thread control render emits at the top of each view.
- **`latex`** — the **formulary box** a `/latex suite` entry wears. An entry is authored as a nested
  `> [!latex]` callout inside a persona/claude card; the `latex` rules box it (blueprint-cyan ink panel,
  monospace title, tight terms table) so the equation reads as a technical reference while the card stays
  the speaker's. Author-agnostic by design (same look whoever speaks), and placed LAST in the file so it
  wins the host persona's `.callout-content` colour for the nested box. See `.claude/skills/latex-suite/`.
