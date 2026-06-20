---
name: latex-suite
description: The vault's house style for writing equations in cards — the suneater "formulary" entry format, named agnostically. Every equation entry is a boxed `[!latex]` callout carrying a four-column terms table (Symbol | Name | Units | Typical Value) that is a SUPERSET of every symbol in the equation. Use whenever authoring a card that states a LaTeX relation, or when the user asks for a /latex suite entry.
---

# /latex suite

The house format for any equation that enters a card. **Lineage:** the suneater **`/formulary`** skill
(`/home/fish/suneater/.claude/skills/formulary/SKILL.md`), reconciled into exo and named agnostically so
exo stays portable. The load-bearing contract is the suneater **iron rule**, verbatim:

> **The parameter table is a SUPERSET of every symbol that appears in the equation.** Every symbol on
> the LHS and RHS, every meaningful subscript, every physical constant ($q$, $k_B$, $\sigma_{SB}$) →
> one row. If the equation has a symbol the table doesn't define, the entry is broken. No exceptions,
> no "obvious from context." A reader at the bench decodes the equation without leaving the page.

**`latex` is a render class, not a persona.** exo keeps the formulary **modular, not fused**: producing
an entry is a *capability every card-author has*, not a bridge bolted between two subsystems. Any voice —
shizu, renka, claude-tui, a persona invented tomorrow — may carry a `/latex suite` entry, wrapped in the
orientation-first voice ([[exo-voice-orientation-first]]). The entry is authored as a nested `[!latex]`
callout, so it renders in its own boxed formulary panel (the `latex` class in
`.obsidian/snippets/persona-cards.css`) *inside* the speaking card — the persona keeps the voice, the
equation wears the box. (This is exo's one divergence from suneater, where formulary output is clean and
the personas live only in `/chatter`, kept apart. The operator settled on this **"B"** form to ship;
a *bare* `[!latex]` card with no persona voice — a canonical, citable formulary object — is the parked
**"A"** extension, cheap and purely additive whenever wanted, since content-addressing already makes every
card citable.)

## The entry — mandatory shape

An entry is authored as a **`[!latex]` callout**: every line carries a leading `> `, so the whole entry
is one self-contained formulary box. When it sits inside a persona/claude card, `record`/`render` prefix
the card's own `> ` rail and it nests as a boxed `[!latex]` panel under the speaker. The **callout title
is the quantity name.**

````markdown
> [!latex] {Quantity name}
> **Symbol:** {LaTeX symbol}
> **Meaning:** {one line — the physical assertion: what it is, where it applies}
>
> $$ {LaTeX equation} $$
>
> | Symbol | Name | Units | Typical Value |
> |--------|------|-------|---------------|
> | $E$    | Incident-ion kinetic energy          | eV              | 100 – 1000 |
> | $U_s$  | Surface binding energy of the target | eV              | 3 – 9      |
> | $Y$    | Sputter yield (atoms per ion)        | [dimensionless] | 0.5 – 5    |
>
> **Operating envelope:** {p / T_e / n_e / bias / geometry — whichever apply; omit the irrelevant}
> **Assumptions:** 1. … 2. … 3. …
````

## Rules (what makes a valid entry)

1. **Iron rule (above):** the table is a superset of the equation's symbols — LHS, RHS, subscripts,
   constants. Extra context rows are fine; missing rows are not.
2. **Columns are EXACTLY `| Symbol | Name | Units | Typical Value |`** — these four, this order, these
   names. Don't add, reorder, or rename. (This is the suneater C3 CTQ; it's what makes entries
   greppable across the vault.)
3. **Dimensionless is explicit: `[dimensionless]`** in the Units column — never blank, never `—`, never
   `1`. A blank unit is ambiguous.
4. **One relation per entry** (the granularity rule: one *derived quantity*, not one topic — if you
   write "this covers $A$, $B$, and their ratio," that's three entries). Define a symbol before reuse.
5. **Order inside the box:** the `[!latex]` title is the quantity name → **Symbol**/**Meaning** →
   equation → table → operating envelope → assumptions → optional extras.
6. **Optional extras** (none required): *Scaling cheat* (a one-line numerical anchor), *Regime table*
   (a second table splitting the quantity by regime), `> [!note]`/`> [!warning]` callouts, a one-line
   citation (`— Lieberman & Lichtenberg §6.5`), 1–3 sentences of commentary.
7. **Short-answer edge case:** a genuinely qualitative entry (a definition, an empirical heuristic with
   no closed form) MAY omit the equation and collapse to **Meaning** + an optional table. This is the
   exception, not the default — never invent a fake equation to force rigor.
8. **Keep the rail unbroken.** Every line of the entry carries `> ` so it stays one `[!latex]` callout;
   a blank line inside the box is a lone `>`. A bare (un-railed) line would split the box and fall out
   of the formulary styling. The `> ` rail is presentation — it nests cleanly because `record`/`render`
   never escape a leading `>` ([[exo-pool-manifest-model]]); the entry's text is what hashes, the box
   is how it renders.

What an entry does NOT carry (suneater retired these): no confidence tags
(`[TEXTBOOK]`/`[SPECULATION]`), no verdict/gate/receipt machinery, no entry IDs (`F-1.A.2`), no
linked-entries graph. Cross-reference by quantity name in prose.

## Worked example — sputter yield (Sigmund)

> [!latex] Sputter yield (Sigmund)
> **Symbol:** $Y(E)$
> **Meaning:** target atoms ejected per incident ion, in the linear-cascade regime.
>
> $$ Y(E) = \frac{3}{4\pi^2}\,\frac{\alpha\,\gamma_E\,E}{U_s}, \qquad \gamma_E = \frac{4\,M_i M_t}{(M_i+M_t)^2} $$
>
> | Symbol     | Name                                        | Units           | Typical Value      |
> |------------|---------------------------------------------|-----------------|--------------------|
> | $Y$        | Sputter yield (atoms per incident ion)      | [dimensionless] | 0.5 – 5            |
> | $E$        | Incident-ion kinetic energy                 | eV              | 100 – 1000         |
> | $U_s$      | Surface binding energy of the target        | eV              | 3 – 9              |
> | $\gamma_E$ | Elastic energy-transfer factor (ion→target) | [dimensionless] | 0.5 – 1            |
> | $M_i$      | Incident-ion mass                           | u               | 40 (Ar)            |
> | $M_t$      | Target-atom mass                            | u               | 48 (Ti), 64 (Cu)   |
> | $\alpha$   | Cascade factor (mass ratio, geometry)       | [dimensionless] | 0.2 – 0.5          |
>
> **Operating envelope:** linear-cascade regime; $E$ from the tens-of-eV threshold up to a few keV; near-normal incidence.
> **Assumptions:** 1. amorphous/random target (no channeling). 2. single-cascade, not thermal-spike, regime. 3. $E$ well above the sputter threshold.
>
> *Scaling cheat: an atom leaves only when the cascade delivers more than $U_s$ — match the masses ($M_i \approx M_t$) and $\gamma_E \to 1$, the most efficient transfer.*

## How to invoke

No new verb — this is the authoring standard the agent applies. When the user says "**/latex suite**"
(or "make that a formulary entry", "add the terms table"), reformat the equation(s) into the boxed
`[!latex]` entry shape above, then `record` the card as usual (contract rule #2). The whole entry —
including the `> [!latex]` rail — is part of the card body, so it is hashed into the id like any other
content; the formulary box is pure presentation (the `latex` CSS class), and the persona's own callout
class stays non-hashed. Suneater's fuller `/formulary` also has a multi-chapter "atlas" mode (design-point
anchors → chapters → entries); exo carries the entry unit, which is the everyday 10:1 case — reach for the
full atlas only if the user explicitly wants a multi-chapter compendium.
