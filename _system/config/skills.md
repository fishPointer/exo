# Adding and editing skills

A **skill** is a reusable command available to both the human (typed as `/name`) and the
agent (invoked when the task matches). It's just a Markdown file with frontmatter.

## Layout

```
.claude/skills/<name>/SKILL.md
```

```markdown
---
name: <name>
description: <one line — what it does and WHEN to use it. This is how the agent decides
             whether to reach for it, so lead with the trigger.>
---

# /<name>

Body: the instructions. Be concrete. Show the exact commands. Assume the reader is
competent but has no context. A skill that wraps a tool should show the tool's invocation
verbatim, not describe it.
```

That's the whole contract: a directory under `.claude/skills/`, a `SKILL.md`, frontmatter
with `name` + `description`. No registration step, no manifest.

## The shipped example

`.claude/skills/stream/SKILL.md` wraps `_system/stream.py`. Copy it as a starting point.

## Good skills

- **One job.** If the description needs "and", it's probably two skills.
- **The description is load-bearing.** It's the only thing the agent sees when deciding to
  use the skill. Write the trigger condition first ("Use when the user wants to …").
- **Show, don't tell.** Paste the real command. The reader will run it, not paraphrase it.
- **Accessible to a human too.** Someone reading the SKILL.md should be able to do the thing
  by hand without the agent.

## Removing a skill

Delete its directory. That's it.
