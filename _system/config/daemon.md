# Running the watcher as an independent service

The watcher (`_system/watch.py`, Layer 1) is the process that turns Obsidian button clicks
(`Summon` et al.) into `stream.py` runs. It must be **running** for the buttons to do anything.
Capture (prompt + reply hooks) is separate and always-on inside Claude Code — it does **not**
need the daemon.

## The rule: never run it as a shell background job

`python3 _system/watch.py &` makes the daemon a **child of whatever shell launched it** (often a
Claude Code Bash call). When that shell exits — you close Claude Code, or it crashes on a bun bug
— the daemon dies with it. Use the manager instead:

```
_system/daemon.sh install     # write+enable the service and start it  (idempotent)
_system/daemon.sh status      # is it up? show the unit + .stream/daemon.json
_system/daemon.sh restart     # bounce it (after editing watch.py)
_system/daemon.sh stop        # take it down
_system/daemon.sh logs        # follow its output (journal, or .stream/daemon.log)
_system/daemon.sh uninstall   # remove the service (records/threads untouched)
```

`/initialize` runs `install` for you — invoking that skill is your approval to run the watcher.

## What `install` actually does

- **With a systemd user session** (the normal Linux desktop case): writes
  `~/.config/systemd/user/exo-watch.service`, `daemon-reload`, `enable --now`. The unit uses
  `Restart=on-failure` (auto-recovers from a crash) and sets `PATH` to include `~/.local/bin` so
  Summon can find the `claude` CLI. It also calls `loginctl enable-linger $USER` so the service
  keeps running across logout and starts on boot. Parent process is the systemd user manager, not
  a shell — so closing Claude Code can't take it down.
- **Without one** (no `XDG_RUNTIME_DIR` / `systemctl --user` bus — e.g. a bare SSH or container):
  falls back to `setsid nohup python3 watch.py`, which detaches the process from the controlling
  terminal and the shell's stdio. Still orphaned from the launching shell (survives its exit), but
  with no auto-restart or boot persistence — PID in `.stream/daemon.pid`, logs in
  `.stream/daemon.log`.

Either way the daemon is the same: it acts only on an explicit button click (a new nonce in
`.stream/trigger.json`). No autonomous loop, no timer — see the no-autonomous-loop rule.

## Verifying independence

```
systemctl --user status exo-watch.service     # Active: active (running), enabled
ps -o ppid= -p "$(systemctl --user show -p MainPID --value exo-watch.service)"   # → the systemd user manager
```

The watcher writes its live PID into `.stream/daemon.json`; `python3 _system/doctor.py` (and
`DASHBOARD.md`) read that to report 🟢/🔴 regardless of how it was launched.

## Machine-local, not synced, not committed

The generated unit lives under `~/.config/systemd/user/` — per-machine state, like everything in
`.stream/`. `daemon.sh` itself is committed apparatus (run `install` once per machine). Don't sync
the unit or `.stream/` across machines (see `ARCHITECTURE.md §5`).
