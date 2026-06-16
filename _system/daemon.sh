#!/usr/bin/env bash
# daemon.sh — install/run the stream-cards watcher (_system/watch.py) as an
# INDEPENDENT background service, not a child of whatever shell launched it.
#
# Why: launching `python3 _system/watch.py &` from Claude Code (or any shell)
# makes the daemon a child of that process — when Claude Code closes or crashes
# (bun bugs et al.), the watcher dies with it. This wraps the watcher in a
# systemd *user* service so it: survives the launching shell, restarts on crash,
# and (with linger) survives logout/reboot. Hosts without a systemd user session
# fall back to a setsid+nohup detached process — still orphaned from the shell.
#
# The watcher is the same Layer-1 daemon either way: it only acts on an explicit
# button click (a new nonce in .stream/trigger.json). No autonomous loop.
#
# Usage:  _system/daemon.sh {install|start|stop|restart|status|logs|uninstall}
#   install    write+enable the unit (or detached fallback) and start it  [idempotent]
#   start/stop/restart/status   manage the running daemon
#   logs       follow the daemon's output
#   uninstall  stop and remove the unit (records/threads untouched)
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
WATCH="$ROOT/_system/watch.py"
PYTHON="$(command -v python3 || true)"
UNIT="exo-watch.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_PATH="$UNIT_DIR/$UNIT"
PIDFILE="$ROOT/.stream/daemon.pid"        # only used by the non-systemd fallback
LOGFILE="$ROOT/.stream/daemon.log"        # only used by the non-systemd fallback

[ -n "$PYTHON" ] || { echo "✗ python3 not found on PATH" >&2; exit 1; }
[ -f "$WATCH" ]  || { echo "✗ $WATCH missing" >&2; exit 1; }

# A systemd *user* session is usable only if systemctl --user can reach its bus.
have_systemd() {
  command -v systemctl >/dev/null 2>&1 \
    && [ -n "${XDG_RUNTIME_DIR:-}" ] \
    && systemctl --user show-environment >/dev/null 2>&1
}

write_unit() {
  mkdir -p "$UNIT_DIR"
  # PATH must include ~/.local/bin so Summon can find the `claude` CLI; a bare
  # systemd PATH wouldn't have it. Restart=on-failure brings it back after a
  # crash but respects the watcher's own clean singleton-exit (exit 0).
  cat > "$UNIT_PATH" <<EOF
[Unit]
Description=exo stream-cards watcher (Summon + Obsidian-button daemon)
Documentation=file://$ROOT/_system/ARCHITECTURE.md
After=default.target

[Service]
Type=simple
ExecStart=$PYTHON $WATCH
WorkingDirectory=$ROOT
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
EOF
  echo "  wrote $UNIT_PATH"
}

cmd_install() {
  mkdir -p "$ROOT/.stream"
  if have_systemd; then
    write_unit
    systemctl --user daemon-reload
    # linger lets the user service keep running across logout / start on boot.
    if command -v loginctl >/dev/null 2>&1 && [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || echo no)" != "yes" ]; then
      loginctl enable-linger "$USER" 2>/dev/null && echo "  enabled linger (survives logout)" \
        || echo "  (could not enable linger — service still survives shell close, but not logout)"
    fi
    systemctl --user enable --now "$UNIT"
    echo "✓ installed + started via systemd user service ($UNIT)"
    cmd_status
  else
    echo "  no systemd user session — using detached (setsid+nohup) fallback"
    cmd_start
  fi
}

# ── non-systemd fallback: a process orphaned from the launching shell ─────────
fallback_running() {
  [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null
}

cmd_start() {
  if have_systemd && [ -f "$UNIT_PATH" ]; then
    systemctl --user start "$UNIT"; echo "✓ started ($UNIT)"; return
  fi
  if fallback_running; then echo "✓ already running (pid $(cat "$PIDFILE"))"; return; fi
  mkdir -p "$ROOT/.stream"
  # setsid detaches from the controlling terminal; nohup + redirect frees it from
  # the shell's stdio. The result outlives the launching Claude Code instance.
  setsid nohup "$PYTHON" "$WATCH" >>"$LOGFILE" 2>&1 < /dev/null &
  echo $! > "$PIDFILE"
  sleep 0.5
  fallback_running && echo "✓ started detached (pid $(cat "$PIDFILE"); logs: $LOGFILE)" \
    || { echo "✗ failed to start — see $LOGFILE" >&2; exit 1; }
}

cmd_stop() {
  if have_systemd && [ -f "$UNIT_PATH" ]; then
    systemctl --user stop "$UNIT"; echo "✓ stopped ($UNIT)"; return
  fi
  if fallback_running; then kill "$(cat "$PIDFILE")" && echo "✓ stopped (pid $(cat "$PIDFILE"))"; rm -f "$PIDFILE";
  else echo "· not running"; fi
}

cmd_restart() { cmd_stop || true; cmd_start; }

cmd_status() {
  if have_systemd && [ -f "$UNIT_PATH" ]; then
    systemctl --user --no-pager status "$UNIT" 2>&1 | head -12 || true
  elif fallback_running; then
    echo "● running (detached, pid $(cat "$PIDFILE"))"
  else
    echo "○ down"
  fi
  echo "--- .stream/daemon.json ---"; cat "$ROOT/.stream/daemon.json" 2>/dev/null || echo "(none)"
}

cmd_logs() {
  if have_systemd && [ -f "$UNIT_PATH" ]; then journalctl --user -u "$UNIT" -f
  else tail -f "$LOGFILE"; fi
}

cmd_uninstall() {
  if have_systemd && [ -f "$UNIT_PATH" ]; then
    systemctl --user disable --now "$UNIT" 2>/dev/null || true
    rm -f "$UNIT_PATH"; systemctl --user daemon-reload
    echo "✓ removed $UNIT (records/threads untouched)"
  else
    cmd_stop
  fi
}

case "${1:-}" in
  install)   cmd_install ;;
  start)     cmd_start ;;
  stop)      cmd_stop ;;
  restart)   cmd_restart ;;
  status)    cmd_status ;;
  logs)      cmd_logs ;;
  uninstall) cmd_uninstall ;;
  *) echo "usage: _system/daemon.sh {install|start|stop|restart|status|logs|uninstall}" >&2; exit 2 ;;
esac
