// The Session card: what this run is recorded on, its CSV files, and the
// sessions before it.
//
// A session is one run of the worker. Starting a new one re-detects the
// camera and the AI HAT+, so it is also how to switch between them after
// plugging hardware in or out.

import { fetchJson, postJson } from './bus.js';
import { num } from './fmt.js';

const TABLE_ROWS = 12;

export class SessionPanel {
  constructor(root, { onToast } = {}) {
    this.root = root;
    this.onToast = onToast || (() => {});
    this.meta = null;
    this.bind();
  }

  bind() {
    const form = this.root.querySelector('[data-session-new]');
    if (!form) return;
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const button = form.querySelector('button');
      const input = form.querySelector('input');
      button.disabled = true;
      try {
        await postJson('/api/session/new', { label: input.value.trim() });
        this.onToast('Starting a new session: the camera and AI HAT+ are detected again');
        input.value = '';
        // The new session needs a few seconds to open the hardware.
        setTimeout(() => this.loadTable(), 8000);
      } catch (error) {
        this.onToast(`Could not start a new session: ${error.message}`, 'error');
      } finally {
        setTimeout(() => {
          button.disabled = false;
        }, 5000);
      }
    });
  }

  render(meta) {
    if (!meta || !meta.session_id) return;
    const changed = !this.meta || this.meta.session_id !== meta.session_id;
    this.meta = meta;

    const accel = meta.accelerator || {};
    const detector = meta.detector || {};

    this.set('session-id', meta.label ? `${meta.label} · ${meta.session_id}` : meta.session_id);
    const model = detector.model_file
      ? `${detector.model_file}, ${detector.input_size} px`
      : 'none';
    this.set('session-model', detector.synthetic ? `${model} (heuristic stub, not a trained model)` : model);

    this.root.querySelectorAll('[data-session-file]').forEach((link) => {
      const name = link.dataset.sessionFile;
      link.href = `/api/sessions/${encodeURIComponent(meta.session_id)}/${name}`;
      link.removeAttribute('aria-disabled');
    });

    const chip = document.querySelector('[data-accelerator-chip]');
    if (chip) {
      chip.classList.toggle('is-live', accel.kind === 'hailo');
      const label = chip.querySelector('[data-accelerator-label]');
      if (label) label.textContent = accel.name || '—';
    }

    if (changed) this.loadTable();
    this.tick();
  }

  // Called every second, so "Running for" counts without a round trip.
  tick() {
    if (!this.meta || !this.meta.started_at) return;
    const seconds = (Date.now() - new Date(this.meta.started_at).getTime()) / 1000;
    this.set('session-duration', duration(seconds));
  }

  async loadTable() {
    const body = this.root.querySelector('[data-session-rows]');
    if (!body) return;
    const data = await fetchJson('/api/sessions').catch(() => null);
    const sessions = (data && data.sessions) || [];
    body.replaceChildren();

    if (!sessions.length) {
      const row = body.insertRow();
      const cell = row.insertCell();
      cell.colSpan = 8;
      cell.className = 'empty';
      cell.textContent = 'No sessions recorded yet';
      return;
    }

    sessions.slice(0, TABLE_ROWS).forEach((session) => {
      const row = body.insertRow();
      if (session.current) row.className = 'is-current';
      const started = shortStamp(session.started_at);
      cellText(row, session.current ? `${started} (now)` : started, session.label || '');
      cellText(row, session.accelerator || '—');
      cellText(row, duration(session.duration_s), '', 'num');
      cellText(row, num(session.inference_fps_mean, 1), '', 'num');
      cellText(row, num(session.total_ms_p95, 1), '', 'num');
      cellText(row, num(session.power_mean_w, 2), session.power_source || '', 'num');
      cellText(row, num(session.energy_wh, 3), '', 'num');

      const files = row.insertCell();
      (session.files || []).filter((name) => name.endsWith('.csv')).forEach((name) => {
        const link = document.createElement('a');
        link.href = `/api/sessions/${encodeURIComponent(session.session_id)}/${name}`;
        link.download = '';
        link.textContent = name.replace('.csv', '');
        files.append(link);
      });
    });
  }

  set(key, value) {
    const el = this.root.querySelector(`[data-${key}]`);
    if (el) el.textContent = value;
  }
}

function cellText(row, text, title = '', className = '') {
  const cell = row.insertCell();
  cell.textContent = text;
  if (title) cell.title = title;
  if (className) cell.className = className;
  return cell;
}

// "Oct 29, 10:54": the year adds width to every row and nothing to a table
// of recent runs.
function shortStamp(iso) {
  const date = new Date(iso);
  if (!iso || Number.isNaN(date.getTime())) return '—';
  return date.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function duration(seconds) {
  if (seconds == null || Number.isNaN(seconds)) return '—';
  const total = Math.max(0, Math.round(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h) return `${h}h ${String(m).padStart(2, '0')}m`;
  if (m) return `${m}m ${String(s).padStart(2, '0')}s`;
  return `${s}s`;
}
