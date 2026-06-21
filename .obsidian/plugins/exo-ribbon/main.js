'use strict';

// exo-ribbon (stream-cards UI)
// ---------------------------------------------------------------------------
// Thin UI over the stream-cards Python backend. Each ribbon button is a
// COMMANDS-FIRST trigger: the logic lives in an addCommand (palette + hotkey-
// assignable + callable by id); the ribbon icon just executes the command.
// Backend coordination is a SENTINEL FILE — the command writes
// `.stream/trigger.json` via the Vault API and the running `watch.py` daemon
// picks it up. No child_process, no ports; cross-platform and decoupled.
//
// The primitives the stream-cards workflow invites:
//   📄 file+  New thread  create a fresh `type: stream` thread note + open it (LOCAL, no daemon)
//   ▶ play   Run       reconcile THIS thread: check -> fold -> restore -> render
//   🧠 brain  Scan      vault-wide dirty pass: flag every thread + write dirty.json
//   ⚛ atom   Validate  content-address integrity (every card hashes to its id)
//   🧪 flask  Restore   HARD reset THIS view: dissolve edits + the staged draft, rebuild from records
//                       (the deliberate wash; a plain/synced re-render keeps your draft, this drops it)
//   ⚡ zap    Summon    fire ONE API call (your click only) — headless claude runs
//                       in the vault (its skills+memories), reads the dashboard, replies
//   — minus  (spacer, inert) — caps the group above the core ribbon icons

const obsidian = require('obsidian');

const PLUGIN_ID = 'exo-ribbon';
const TRIGGER_PATH = '.stream/trigger.json';

// label, lucide icon, command id, and how it acts.
//  kind 'backend' => writes a sentinel with `action` for watch.py
//  kind 'local'   => handled in-plugin, no daemon (create+open a new thread note)
//  kind 'spacer'  => inert divider
const PRIMITIVES = [
  { id: 'newthread', label: 'New thread', icon: 'file-plus',    kind: 'local',   action: 'newthread' },
  { id: 'run',      label: 'Run',       icon: 'play',          kind: 'backend', action: 'run' },
  { id: 'annotate', label: 'Annotate',  icon: 'pencil',        kind: 'backend', action: 'annotate' },
  { id: 'pull',     label: 'Pull',      icon: 'scissors',      kind: 'backend', action: 'pull' },
  { id: 'scan',     label: 'Scan',      icon: 'brain',         kind: 'backend', action: 'scan' },
  { id: 'validate', label: 'Validate',  icon: 'atom',          kind: 'backend', action: 'validate' },
  { id: 'spacer',   label: 'spacer',    icon: 'minus',         kind: 'spacer' },
  { id: 'reset',    label: 'Restore',   icon: 'flask-conical', kind: 'backend', action: 'reset' },
  { id: 'summon',   label: 'Summon',    icon: 'zap',           kind: 'backend', action: 'summon' },
];

// Tiny prompt modal for the New-thread button: one text field, Enter or Create to
// submit, Esc to cancel. Pre-filled with a timestamp default so a single Enter spins
// up a thread (daily-note-style), or type a name over it.
class NewThreadModal extends obsidian.Modal {
  constructor(app, def, onSubmit) {
    super(app);
    this.def = def;
    this.onSubmit = onSubmit;
  }
  onOpen() {
    const { contentEl } = this;
    contentEl.createEl('h3', { text: 'New stream thread' });
    const input = contentEl.createEl('input', { type: 'text' });
    input.value = this.def;
    input.placeholder = 'thread name';
    input.style.width = '100%';
    const submit = () => { const v = input.value; this.close(); this.onSubmit(v); };
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); submit(); }
    });
    const row = contentEl.createDiv({ cls: 'modal-button-container' });
    const create = row.createEl('button', { text: 'Create', cls: 'mod-cta' });
    create.addEventListener('click', submit);
    window.setTimeout(() => { input.focus(); input.select(); }, 0);
  }
  onClose() { this.contentEl.empty(); }
}

module.exports = class ExoRibbon extends obsidian.Plugin {
  async onload() {
    let anchor = null;

    for (const p of PRIMITIVES) {
      if (p.kind === 'spacer') {
        const el = this.addRibbonIcon(p.icon, '', () => {});
        el.addClass('exo-ribbon-spacer');
        anchor = anchor || el;
        continue;
      }

      // Commands-first: register the logic as a command (palette + hotkey-
      // assignable + callable by id)... backend kinds drop a sentinel; local
      // kinds run in-plugin.
      const invoke = p.kind === 'local'
        ? () => this.local(p.action, p.label)
        : () => this.trigger(p.action, p.label);
      this.addCommand({
        id: p.id,
        name: `Stream: ${p.label.toLowerCase()}`,
        callback: invoke,
      });

      // ...and make the ribbon icon a thin trigger for it.
      const el = this.addRibbonIcon(p.icon, p.label, () =>
        this.app.commands.executeCommandById(`${PLUGIN_ID}:${p.id}`));
      anchor = anchor || el;
    }

    // Return-path: the backend fires obsidian://stream-reload?file=… after a
    // render so the note re-focuses (Obsidian already hot-reloads file contents;
    // this brings the reconciled thread back to the front).
    this.registerObsidianProtocolHandler('stream-reload', async (params) => {
      if (!params.file) return;
      const tf = this.app.vault.getFileByPath(obsidian.normalizePath(params.file));
      if (tf) await this.app.workspace.getLeaf(false).openFile(tf);
    });

    // #3 — live summon status chip in Obsidian's status bar. Polls the daemon's
    // in-flight marker (a LOCAL file, never synced) every second; shows the
    // elapsed churn where your eyes already are, clears when the call lands.
    const summonStatus = this.addStatusBarItem();
    summonStatus.addClass('exo-summon-status');
    this.registerInterval(window.setInterval(() => this.pollSummon(summonStatus), 1000));

    this.app.workspace.onLayoutReady(() => this.reorderRibbon(anchor));
  }

  async pollSummon(el) {
    try {
      const p = obsidian.normalizePath('.stream/summon-inflight.json');
      if (await this.app.vault.adapter.exists(p)) {
        const d = JSON.parse(await this.app.vault.adapter.read(p));
        // elapsed computed client-side from the start epoch — the daemon never
        // rewrites the marker mid-call, so nothing synced churns.
        const elapsed = d.started_epoch ? Math.max(0, Math.floor(Date.now() / 1000 - d.started_epoch)) : 0;
        el.setText(`⏳ summon ${elapsed}s · churning`);
      } else {
        el.setText('');
      }
    } catch (e) {
      el.setText('');
    }
  }

  // Drop a sentinel the watch.py daemon picks up. Vault adapter write +
  // normalizePath — no shelling out from the plugin.
  async trigger(action, label) {
    const path = obsidian.normalizePath(TRIGGER_PATH);
    const view = this.app.workspace.getActiveFile()?.path ?? null;
    const nonce = `${Date.now()}-${action}`;       // unique per click
    const payload = JSON.stringify({ action, view, nonce }, null, 2);
    try {
      await this.app.vault.adapter.mkdir(obsidian.normalizePath('.stream')).catch(() => {});
      await this.app.vault.adapter.write(path, payload);
      new obsidian.Notice(`stream: ${label} -> queued (watch.py)`);
    } catch (e) {
      new obsidian.Notice(`stream: ${label} failed — ${e.message}`);
    }
  }

  // Local (no-daemon) actions, dispatched from kind:'local' buttons.
  local(action, label) {
    if (action === 'newthread') return this.newThread();
    new obsidian.Notice(`stream: unknown local action — ${action}`);
  }

  // New-thread button: prompt for a name, scaffold notes/<slug>.md as a fresh
  // `type: stream` thread, and open it. A thread is just a note with that
  // frontmatter — its MANIFEST bootstraps on the first card — so this needs no
  // backend round-trip (unlike the sentinel buttons). Like the daily-note button,
  // but for a new stream thread beside main. Opening an existing slug just focuses it.
  async newThread() {
    const now = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    const def = `thread-${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}`;
    new NewThreadModal(this.app, def, async (raw) => {
      const name = (raw || '').trim() || def;
      const slug = this.threadSlug(name) || this.threadSlug(def);
      const path = obsidian.normalizePath(`notes/${slug}.md`);
      let tf = this.app.vault.getFileByPath(path);
      if (tf) {
        new obsidian.Notice(`stream: notes/${slug}.md exists — opening it`);
      } else {
        try {
          await this.app.vault.adapter.mkdir(obsidian.normalizePath('notes')).catch(() => {});
          tf = await this.app.vault.create(path, this.threadScaffold(name));
          new obsidian.Notice(`stream: new thread → notes/${slug}.md`);
        } catch (e) {
          new obsidian.Notice(`stream: new thread failed — ${e.message}`);
          return;
        }
      }
      await this.app.workspace.getLeaf(false).openFile(tf);
    }).open();
  }

  // flat, filesystem-safe slug (threads live flat in notes/, no subfolders)
  threadSlug(name) {
    return String(name).trim().toLowerCase()
      .replace(/[^\p{L}\p{N}]+/gu, '-')
      .replace(/^-+|-+$/g, '')
      .slice(0, 60);
  }

  // the empty-thread seed: `type: stream` frontmatter + a trailing `---` staging
  // separator, so you can type below it and hit Run. Title is quoted (arbitrary input).
  threadScaffold(title) {
    const safe = String(title).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
    return `---\ntype: stream\nstatus: live\ntitle: "${safe}"\n---\n\n\n---\n`;
  }

  reorderRibbon(anchor) {
    const container = anchor && anchor.parentElement;
    if (!container) return;
    const order = ['New thread', 'Run', 'Scan', 'Validate', 'Restore', 'Summon', '__spacer__'];
    const wanted = [];
    for (const key of order) {
      const el = key === '__spacer__'
        ? container.querySelector('.exo-ribbon-spacer')
        : container.querySelector(`:scope > [aria-label="${key}"]`);
      if (el) wanted.push(el);
    }
    for (let i = wanted.length - 1; i >= 0; i--) {
      container.insertBefore(wanted[i], container.firstChild);
    }
  }
};
