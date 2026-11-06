// Page bootstrap. Wires the transport to whichever panels this page contains.

import { Bus, poll, fetchJson, postJson } from './bus.js';
import { Controls, toast } from './controls.js';
import { DeviceList } from './devices.js';
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
  const controls = new Controls(document, { onToast: toast });

  // Boxes show only while the detector is running and not paused; anything
  // else would leave the last ones frozen over the picture.
  let workerAlive = document.querySelector('[data-worker-chip].is-live') !== null;
  let paused = false;
  let lastReadingAt = null;
  let lastFrameAt = null;

  // -- live feed: one view per camera ------------------------------------

  const prefs = displayPrefs();
  const liveMode = prefs.live ? 'mjpeg' : 'poll';
  const viewsRoot = document.querySelector('[data-viewports]');
  const views = [];
  let pageLoaded = false;

  const showOverlay = () => views.forEach((view) => view.toggleOverlay(workerAlive && !paused));

  const addView = (index, element) => {
    const view = new LiveView(element, index);
    view.setMirror(prefs.mirror);
    view.toggleOverlay(workerAlive && !paused);
    if (pageLoaded && !document.hidden) view.setMode(liveMode);
    views[index] = view;
  };
  if (viewsRoot) addView(0, viewsRoot.querySelector('[data-camera-view]'));

  // The session says how many cameras run; the first figure is in the page,
  // the rest are clones of it. Labels only appear when there is a choice.
  const showCameras = (cameras) => {
    if (!viewsRoot) return;
    const list = cameras && cameras.length ? cameras : [{}];
    while (views.length > list.length) {
      const view = views.pop();
      view.destroy();
      view.root.remove();
    }
    const template = viewsRoot.querySelector('[data-camera-view]');
    for (let index = views.length; index < list.length; index += 1) {
      const element = template.cloneNode(true);
      viewsRoot.appendChild(element);
      addView(index, element);
    }
    list.forEach((camera, index) => {
      const name = `Camera ${index + 1}`;
      const label = views[index].root.querySelector('[data-camera-label]');
      if (label) {
        label.hidden = list.length < 2;
        label.querySelector('[data-camera-name]').textContent = camera.model ? `${name} · ${camera.model}` : name;
      }
      views[index].image.alt = list.length < 2
        ? 'Live camera feed of the bell pepper canopy'
        : `${name} live feed of the bell pepper canopy`;
    });
    showCamerasOn();
  };

  // Which cameras are on, from the worker's pipeline telemetry. An off camera
  // keeps its label and a Turn on button, and fetches no frames.
  let camerasOn = [];
  function showCamerasOn() {
    if (!viewsRoot) return;
    const onCount = views.filter((view, index) => camerasOn[index] !== false).length;
    viewsRoot.classList.toggle('is-multi', onCount > 1);
    views.forEach((view, index) => {
      view.off = camerasOn[index] === false;
      view.root.classList.toggle('is-off', view.off);
      const button = view.root.querySelector('[data-camera-switch]');
      if (button) {
        const action = view.off ? 'Turn on' : 'Turn off';
        button.textContent = action;
        button.setAttribute('aria-label', `${action} camera ${index + 1}`);
        button.disabled = !view.off && onCount < 2; // the last camera on stays on
      }
      if (view.off) view.setMode('idle');
      else if (view.mode === 'idle' && pageLoaded && !document.hidden) view.setMode(liveMode);
    });
  }

  if (viewsRoot) {
    viewsRoot.addEventListener('click', async (event) => {
      const button = event.target.closest('[data-camera-switch]');
      if (!button) return;
      const index = views.findIndex((view) => view.root.contains(button));
      const enabled = camerasOn[index] === false;
      button.disabled = true; // until the worker's next report shows the switch
      try {
        await postJson(`/api/camera/${index}`, { enabled });
        toast(`Camera ${index + 1} ${enabled ? 'on' : 'off'}`);
      } catch (error) {
        toast(`Could not switch camera ${index + 1}: ${error.message}`, 'error');
        button.disabled = false;
      }
    });
  }

  // Start the never-ending MJPEG response after the page has loaded, or the
  // browser shows the page as loading for as long as the stream runs.
  window.addEventListener('load', () => {
    pageLoaded = true;
    views.forEach((view) => view.setMode(view.off ? 'idle' : liveMode));
  }, { once: true });
  // A background tab would otherwise hold MJPEG slots, and the radio's
  // bandwidth, for nobody.
  document.addEventListener('visibilitychange', () => {
    views.forEach((view) => view.setMode(document.hidden || view.off ? 'idle' : liveMode));
  });
  bus.on('detections', (payload) => {
    if (payload.ts) lastFrameAt = payload.ts * 1000;
    const view = views[payload.camera || 0];
    if (view) view.update(payload);
  });
  bus.on('session', (meta) => showCameras(meta.cameras));
  controls.setAvailable(workerAlive);

  // -- performance, power and the session --------------------------------

  const deviceRoot = document.querySelector('[data-device-list]');
  const devices = deviceRoot ? new DeviceList(deviceRoot) : null;

  bus.on('telemetry', (payload) => {
    if (payload.ts) lastReadingAt = payload.ts * 1000;
    renderTelemetry(document, payload);
    if (devices) devices.setTelemetry(payload);
  });
  bus.on('pipeline', (payload) => {
    renderPipeline(document, payload);
    controls.setPaused(payload.paused);
    paused = Boolean(payload.paused);
    showOverlay();
    camerasOn = payload.camera_on || [];
    showCamerasOn();
    if (devices) devices.setCamerasOn(payload.camera_on);
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
      if (data && data.camera_on) camerasOn = data.camera_on;
      if (data && data.session) {
        showCameras(data.session.cameras);
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
    workerAlive = state.alive === true;
    controls.setAvailable(workerAlive);
    showOverlay();
    showStale(workerAlive, lastReadingAt, lastFrameAt);
    // MJPEG falls back to polling while the worker is down; go live again
    // once it is back.
    if (state.alive && liveMode === 'mjpeg' && !document.hidden) {
      views.forEach((view) => {
        if (view.mode === 'poll') view.setMode('mjpeg');
      });
    }
  });

  // A new persisted event means the gallery is stale.
  const history = document.querySelector('[data-history]')
    ? new History(document.querySelector('[data-history]'), { pageSize: 8 })
    : null;
  if (history) {
    history.load();
    bus.on('persisted', (event) => {
      history.load();
      // The worker saved it: confirmation that the snapshot really happened.
      if (event.trigger === 'manual') toast('Snapshot saved to the detection log');
    });
  }

  bus.on('flag', (payload) => {
    if (payload.severity && payload.severity !== 'none') {
      const where = views.length > 1 ? `Camera ${(payload.camera || 0) + 1}: s` : 'S';
      toast(`${where}everity is now ${payload.severity} (${payload.count} clusters)`);
    }
  });

  // -- weekly stats ------------------------------------------------------

  const refreshStats = async () => {
    const stats = await fetchJson(`/api/stats/weekly?locale=${locale()}`);
    if (!stats) return;
    renderKpis(document, stats);

    const stamp = document.querySelector('[data-updated]');
    if (stamp) stamp.textContent = `updated ${relative(new Date().toISOString())}`;
  };
  poll(refreshStats, STATS_INTERVAL_MS);

  // Data cleared from Settings, on this phone or another: drop what is shown.
  bus.on('cleared', () => {
    if (history) history.load();
    refreshStats();
  });

  bus.start();
  window.addEventListener('beforeunload', () => {
    bus.stop();
    views.forEach((view) => view.destroy());
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

// With the detector down the live tiles and the camera hold their last
// values; dim them and say when those were, so they are not read as live.
function showStale(alive, readingAt, frameAt) {
  const at = (ms) => (ms ? new Date(ms).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) : null);
  const reading = at(readingAt);
  document.querySelectorAll('[data-live-figure]').forEach((tile) => {
    tile.classList.toggle('is-stale', !alive);
    const note = tile.querySelector('[data-stale-note]');
    if (note) {
      note.hidden = alive;
      note.textContent = reading ? `Last reading ${reading}` : 'No live reading';
    }
  });

  document.querySelectorAll('[data-viewport]').forEach((viewport) => {
    viewport.classList.toggle('is-stale', !alive);
  });
  const frame = at(frameAt);
  document.querySelectorAll('[data-viewport-stale]').forEach((chip) => {
    chip.hidden = alive;
    const label = chip.querySelector('[data-viewport-stale-label]');
    if (label) label.textContent = frame ? `Detector offline · frame from ${frame}` : 'Detector offline';
  });
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
