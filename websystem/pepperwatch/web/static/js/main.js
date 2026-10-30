// Page bootstrap. Wires the transport to whichever panels this page contains.

import { Bus, poll, fetchJson } from './bus.js';
import { Controls, toast } from './controls.js';
import { DeviceList } from './devices.js';
import { renderDonut } from './donut.js';
import { History } from './history.js';
import { renderKpis } from './kpis.js';
import { LiveView, displayPrefs } from './liveview.js';
import { renderPipeline, renderSummary, renderTelemetry } from './performance.js';
import { SessionPanel } from './session.js';
import { initSettings } from './settings.js';
import { relative } from './fmt.js';

// The weekly aggregate refreshes on a plain setInterval rather than over the
// push channel: it summarises seven days, so a 30-second cadence is ample, and
// §3.5.1 asks Selenium to track exactly this function.
const STATS_INTERVAL_MS = 30000;

function locale() {
  return localStorage.getItem('pw-locale') || 'en';
}

function bootDashboard() {
  const bus = new Bus();
  const liveRoot = document.querySelector('[data-live]');
  const live = liveRoot ? new LiveView(liveRoot) : null;
  const controls = new Controls(document, { onToast: toast });

  // -- live feed ---------------------------------------------------------

  const prefs = displayPrefs();
  const liveMode = prefs.live ? 'mjpeg' : 'poll';
  if (live) {
    live.toggleOverlay(prefs.boxes);
    live.setMirror(prefs.mirror);
    // Start the never-ending MJPEG response after the page has loaded, or the
    // browser shows the page as loading for as long as the stream runs.
    window.addEventListener('load', () => live.setMode(liveMode), { once: true });
    // A background tab would otherwise hold one of the few MJPEG slots, and
    // the radio's bandwidth, for nobody.
    document.addEventListener('visibilitychange', () => {
      live.setMode(document.hidden ? 'idle' : liveMode);
    });
    bus.on('detections', (payload) => live.update(payload));
  }

  // -- performance, power and the session --------------------------------

  const deviceRoot = document.querySelector('[data-device-list]');
  const devices = deviceRoot ? new DeviceList(deviceRoot) : null;

  bus.on('telemetry', (payload) => {
    renderTelemetry(document, payload);
    if (devices) devices.setTelemetry(payload);
  });
  bus.on('pipeline', (payload) => {
    renderPipeline(document, payload);
    controls.setPaused(payload.paused);
  });
  bus.on('summary', (payload) => renderSummary(document, payload));

  const sessionRoot = document.querySelector('[data-session]');
  if (sessionRoot) {
    const session = new SessionPanel(sessionRoot, { onToast: toast });
    bus.on('session', (meta) => {
      session.render(meta);
      if (devices) devices.setSession(meta);
    });
    bus.on('summary', () => session.loadTable());
    setInterval(() => session.tick(), 1000);
    // The retained MQTT message arrives with the SSE hello, but ask directly
    // too, so the card fills even before the stream connects.
    fetchJson('/api/session').then((data) => {
      if (data && data.session) {
        session.render(data.session);
        if (devices) devices.setSession(data.session);
      }
      if (data && data.summary) renderSummary(document, data.summary);
    }).catch(() => {});
    session.loadTable();
  }

  bus.on('worker', (state) => {
    if (devices) devices.setWorker(state);
    showWorker(state);
    // MJPEG falls back to polling while the worker is down; go live again
    // once it is back.
    if (live && state.alive && liveMode === 'mjpeg' && live.mode === 'poll' && !document.hidden) {
      live.setMode('mjpeg');
    }
  });

  bus.on('mode', (mode) => {
    const chip = document.querySelector('[data-transport-chip]');
    if (chip) {
      const label = chip.querySelector('[data-transport-label]');
      if (label) label.textContent = mode === 'sse' ? 'live' : mode;
      chip.classList.toggle('is-warn', mode === 'polling' || mode === 'reconnecting');
    }
  });

  // A new persisted event means the gallery is stale.
  const history = document.querySelector('[data-history]')
    ? new History(document.querySelector('[data-history]'), { pageSize: 8 })
    : null;
  if (history) {
    history.load();
    bus.on('persisted', () => history.load());
  }

  bus.on('flag', (payload) => {
    if (payload.severity && payload.severity !== 'none') {
      toast(`Severity is now ${payload.severity} (${payload.count} clusters)`);
    }
  });

  // -- weekly stats ------------------------------------------------------

  poll(async () => {
    const stats = await fetchJson(`/api/stats/weekly?locale=${locale()}`);
    if (!stats) return;
    renderKpis(document, stats);

    const donut = document.querySelector('[data-donut]');
    if (donut) renderDonut(donut, stats.distribution, document.querySelector('[data-legend]'));

    const stamp = document.querySelector('[data-updated]');
    if (stamp) stamp.textContent = `updated ${relative(new Date().toISOString())}`;
  }, STATS_INTERVAL_MS);

  bus.start();
  window.addEventListener('beforeunload', () => {
    bus.stop();
    if (live) live.destroy();
  });
}

// The status chip in the top bar, on every page.
function showWorker(state) {
  const chip = document.querySelector('[data-worker-chip]');
  if (chip) {
    chip.classList.toggle('is-live', state.alive === true);
    chip.classList.toggle('is-down', state.alive !== true);
    const label = chip.querySelector('[data-worker-label]');
    if (label) label.textContent = state.alive ? 'Detector online' : 'Detector offline';
  }

  const banner = document.querySelector('[data-worker-banner]');
  if (banner) banner.hidden = state.alive === true;
}

// Pages without the live bus still ask once, so their chip tells the truth.
function checkWorker() {
  fetchJson('/api/health')
    .then((health) => {
      if (health && health.worker) showWorker(health.worker);
    })
    .catch(() => {});
}

function bootSettings() {
  checkWorker();
  initSettings(document).catch((error) => console.error('[pw] settings failed', error));
}

const page = document.body.dataset.page;
if (page === 'dashboard') bootDashboard();
else if (page === 'settings') bootSettings();
