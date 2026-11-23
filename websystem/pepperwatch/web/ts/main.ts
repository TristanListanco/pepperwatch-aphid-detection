// Page bootstrap. Wires the transport to whichever panels this page contains.

import type { CameraMeta, Health, SessionMeta, SessionResponse, WeeklyStats, WorkerState } from './api.js';
import { Bus, poll, fetchJson, postJson } from './bus.js';
import { Controls, toast } from './controls.js';
import { DeviceList } from './devices.js';
import { renderFarm } from './farm.js';
import { History } from './history.js';
import { renderKpis } from './kpis.js';
import { LiveView, displayPrefs, type LiveMode } from './liveview.js';
import { renderPipeline, renderSummary, renderTelemetry } from './performance.js';
import { SessionPanel } from './session.js';
import { initSettings } from './settings.js';
import { errorText, relative } from './fmt.js';
import { applyWords, isSimple, locale, word } from './words.js';

// The weekly aggregate refreshes on a plain setInterval rather than over the
// push channel: it summarises seven days, so a 30-second cadence is ample, and
// §3.5.1 asks Selenium to track exactly this function.
const STATS_INTERVAL_MS = 30000;

function bootDashboard(): void {
  const bus = new Bus();
  const controls = new Controls(document, { onToast: toast });

  // Boxes show only while the detector is running and not paused; anything
  // else would leave the last ones frozen over the picture. With no model the
  // stand-in detector's boxes are made up, so they never show.
  let workerAlive = document.querySelector('[data-worker-chip].is-live') !== null;
  let paused = false;
  let noModel = false;
  let lastReadingAt: number | null = null;
  let lastFrameAt: number | null = null;

  // -- live feed: one view per camera ------------------------------------

  const prefs = displayPrefs();
  const liveMode: LiveMode = prefs.live ? 'mjpeg' : 'poll';
  const viewsRoot = document.querySelector<HTMLElement>('[data-viewports]');
  const views: LiveView[] = [];
  let pageLoaded = false;

  const overlayOn = () => workerAlive && !paused && !noModel;
  const overlayNote = () => (noModel ? word('live.no_model') : '');
  const showOverlay = () => views.forEach((view) => view.toggleOverlay(overlayOn(), overlayNote()));

  const addView = (index: number, element: HTMLElement) => {
    const view = new LiveView(element, index);
    view.setMirror(prefs.mirror);
    view.toggleOverlay(overlayOn(), overlayNote());
    if (pageLoaded && !document.hidden) view.setMode(liveMode);
    element.style.viewTransitionName = `camera-${index}`; // see focusCamera
    views[index] = view;
  };
  // The first camera's figure is in the page; the rest are clones of it.
  const template = viewsRoot?.querySelector<HTMLElement>('[data-camera-view]') ?? null;
  if (template) addView(0, template);

  // The session says how many cameras run; the first figure is in the page,
  // the rest are clones of it. Labels only appear when there is a choice.
  let cameraCount: number | null = null;
  const showCameras = (cameras: CameraMeta[] | undefined) => {
    if (!viewsRoot || !template) return;
    const list: CameraMeta[] = cameras && cameras.length ? cameras : [{}];
    // More than last time: a camera was plugged in and the session restarted.
    if (cameraCount !== null && cameras && cameras.length > cameraCount) toast(word('toast.added'));
    if (cameras && cameras.length) cameraCount = cameras.length;
    while (views.length > list.length) {
      const view = views.pop()!;
      view.destroy();
      view.root.remove();
    }
    for (let index = views.length; index < list.length; index += 1) {
      const element = template.cloneNode(true) as HTMLElement;
      viewsRoot.appendChild(element);
      addView(index, element);
    }
    list.forEach((camera, index) => {
      const name = word('camera.n', { n: index + 1 });
      views[index].name = camera.model && !isSimple() ? `${name} · ${camera.model}` : name;
      const label = views[index].root.querySelector<HTMLElement>('[data-camera-label]');
      if (label) label.hidden = list.length < 2;
      views[index].image.alt = list.length < 2
        ? 'Live camera feed of the bell pepper canopy'
        : `${name} live feed of the bell pepper canopy`;
    });
    showCamerasOn();
  };

  // Which cameras are on and plugged in, from the worker's pipeline telemetry.
  // An off camera keeps its label and a Turn on button; an unplugged one keeps
  // its frame, saying so. Neither fetches frames.
  let camerasOn: boolean[] = [];
  let camerasConnected: boolean[] = [];
  let camerasStalled: boolean[] = [];
  let camerasZoom: number[] = [];
  // The camera expanded to the whole card, or null. The others fetch no
  // frames meanwhile: the radio's bandwidth goes to the one being looked at.
  let focused: number | null = null;
  function showCamerasOn() {
    if (!viewsRoot) return;
    const unplugged = (index: number) => camerasConnected[index] === false;
    // Plugged in and on, but sending no pictures: shown like an unplugged one.
    const stalled = (index: number) => camerasStalled[index] === true && !unplugged(index);
    const active = (index: number) => camerasOn[index] !== false && !unplugged(index);
    const onCount = views.filter((view, index) => active(index)).length;
    const framed = views.filter((view, index) => active(index) || unplugged(index)).length;
    if (focused !== null && (framed < 2 || !active(focused))) focused = null;
    viewsRoot.classList.toggle('is-multi', framed > 1);
    viewsRoot.classList.toggle('has-focus', focused !== null);
    views.forEach((view, index) => {
      view.off = !active(index);
      view.idle = view.off || (focused !== null && index !== focused);
      view.root.classList.toggle('is-focused', index === focused);
      const expand = view.root.querySelector('[data-camera-expand]');
      if (expand) expand.setAttribute('aria-label', word('camera.expand', { n: index + 1 }));
      view.root.classList.toggle('is-off', !active(index) && !unplugged(index));
      view.root.classList.toggle('is-unplugged', unplugged(index));
      view.root.classList.toggle('is-stalled', stalled(index));
      const n = index + 1;
      const name = view.root.querySelector('[data-camera-name]');
      if (name) {
        if (unplugged(index)) name.textContent = word('camera.label_unplugged', { n });
        else if (stalled(index)) name.textContent = word('camera.label_stalled', { n });
        else name.textContent = view.name || word('camera.n', { n });
      }
      const note = view.root.querySelector('[data-camera-unplugged]');
      if (note) note.textContent = word(stalled(index) ? 'camera.stalled' : 'camera.unplugged', { n });
      const short = view.root.querySelector('[data-camera-short]');
      if (short) short.textContent = word(stalled(index) ? 'camera.stalled_short' : 'camera.plug_back');
      const button = view.root.querySelector<HTMLButtonElement>('[data-camera-switch]');
      if (button) {
        const action = view.off ? 'Turn on' : 'Turn off';
        button.hidden = unplugged(index);
        button.textContent = action;
        button.setAttribute('aria-label', `${action} camera ${index + 1}`);
        button.disabled = !view.off && onCount < 2; // the last camera on stays on
      }
      const zoomed = camerasZoom[index] ?? 1;
      const zoom = view.root.querySelector<HTMLElement>('[data-camera-zoom]');
      if (zoom) zoom.hidden = !active(index);
      view.root.querySelectorAll<HTMLButtonElement>('[data-zoom]').forEach((option) => {
        option.setAttribute('aria-pressed', String(Number(option.dataset.zoom) === zoomed));
      });
      if (view.idle) view.setMode('idle');
      else if (view.mode === 'idle' && pageLoaded && !document.hidden) view.setMode(liveMode);
    });
  }

  // Tap a camera to expand it; All cameras (or Escape) goes back to them all.
  // Where the browser can, the camera grows into the card and shrinks back to
  // its place (a view transition), so it is clear which one it was.
  const calm = window.matchMedia('(prefers-reduced-motion: reduce)');
  const focusCamera = (index: number | null) => {
    const change = () => {
      focused = index;
      showCamerasOn();
    };
    const moved = typeof document.startViewTransition === 'function' && !calm.matches
      ? document.startViewTransition(change).finished
      : Promise.resolve(change());
    // After the move, not during it: scrolling mid-transition would shift
    // the camera away from where its animation ends.
    moved.then(() => {
      if (index !== null && views[index]) {
        views[index].root.scrollIntoView({ block: 'nearest', behavior: calm.matches ? 'auto' : 'smooth' });
      }
    });
  };
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && focused !== null) focusCamera(null);
  });

  if (viewsRoot) {
    viewsRoot.addEventListener('click', async (event) => {
      const target = event.target as Element;
      const at = views.findIndex((view) => view.root.contains(target));
      // Before the picture's own click, which expands a camera.
      const zoomOption = target.closest<HTMLButtonElement>('[data-zoom]');
      if (zoomOption) {
        const zoom = Number(zoomOption.dataset.zoom);
        if (zoomOption.getAttribute('aria-pressed') === 'true') return;
        zoomOption.disabled = true; // until the worker's next report shows it
        try {
          await postJson(`/api/camera/${at}`, { zoom });
          toast(`Camera ${at + 1} zoomed to ${zoom}×`);
        } catch (error) {
          toast(`Could not zoom camera ${at + 1}: ${errorText(error)}`, 'error');
        } finally {
          zoomOption.disabled = false;
        }
        return;
      }
      if (target.closest('[data-camera-exit]')) {
        focusCamera(null);
        return;
      }
      const picture = target.closest('[data-camera-expand], [data-viewport]');
      if (picture && focused === null && viewsRoot.classList.contains('is-multi') && !views[at].off) {
        focusCamera(at);
        return;
      }
      const button = target.closest<HTMLButtonElement>('[data-camera-switch]');
      if (!button) return;
      const index = at;
      const enabled = camerasOn[index] === false;
      button.disabled = true; // until the worker's next report shows the switch
      try {
        await postJson(`/api/camera/${index}`, { enabled });
        toast(`Camera ${index + 1} ${enabled ? 'on' : 'off'}`);
      } catch (error) {
        toast(`Could not switch camera ${index + 1}: ${errorText(error)}`, 'error');
        button.disabled = false;
      }
    });
  }

  // Start the never-ending MJPEG response after the page has loaded, or the
  // browser shows the page as loading for as long as the stream runs.
  window.addEventListener('load', () => {
    pageLoaded = true;
    views.forEach((view) => view.setMode(view.idle ? 'idle' : liveMode));
  }, { once: true });
  // A background tab would otherwise hold MJPEG slots, and the radio's
  // bandwidth, for nobody.
  document.addEventListener('visibilitychange', () => {
    views.forEach((view) => view.setMode(document.hidden || view.idle ? 'idle' : liveMode));
  });
  bus.on('detections', (payload) => {
    if (payload.ts) lastFrameAt = payload.ts * 1000;
    const view = views[payload.camera || 0];
    if (view) view.update(payload);
  });
  const showSession = (meta: SessionMeta) => {
    showCameras(meta.cameras);
    noModel = meta.detector?.synthetic === true;
    showOverlay();
  };
  bus.on('session', showSession);
  controls.setAvailable(workerAlive);

  // -- performance, power and the session --------------------------------

  const deviceRoot = document.querySelector<HTMLElement>('[data-device-list]');
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
    camerasZoom = payload.camera_zoom || [];
    const connected = payload.camera_connected || [];
    connected.forEach((plugged, index) => {
      if (plugged === false && camerasConnected[index] === true) {
        toast(word('toast.unplugged', { n: index + 1 }), 'error');
      }
    });
    camerasConnected = connected;
    const stalledNow = payload.camera_stalled || [];
    stalledNow.forEach((quiet, index) => {
      if (quiet === true && camerasStalled[index] === false) toast(word('toast.stalled', { n: index + 1 }), 'error');
    });
    camerasStalled = stalledNow;
    showStorage(payload.storage, payload.disk_free_mb);
    showCamerasOn();
    if (devices) devices.setCameras(payload.camera_on, payload.camera_connected, payload.camera_stalled);
  });
  bus.on('summary', (payload) => renderSummary(document, payload));

  const sessionRoot = document.querySelector<HTMLElement>('[data-session]');
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
    fetchJson<SessionResponse>('/api/session').then((data) => {
      if (data && data.camera_on) camerasOn = data.camera_on;
      if (data && data.camera_connected) camerasConnected = data.camera_connected;
      if (data && data.session) {
        showSession(data.session);
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
  const historyRoot = document.querySelector<HTMLElement>('[data-history]');
  const history = historyRoot ? new History(historyRoot, { pageSize: isSimple() ? 4 : 8 }) : null;
  if (history) {
    history.load();
    bus.on('persisted', (event) => {
      history.load();
      // The worker saved it: confirmation that the snapshot really happened.
      if (event.trigger === 'manual') toast(isSimple() ? word('toast.saved') : 'Snapshot saved to the detection log');
    });
  }

  bus.on('flag', (payload) => {
    if (!noModel && payload.severity && payload.severity !== 'none') {
      const camera = views.length > 1 ? `${word('camera.n', { n: (payload.camera || 0) + 1 })}: ` : '';
      if (isSimple()) {
        toast(`${camera}${word(`sev.${payload.severity}`)}`);
      } else {
        const where = views.length > 1 ? `Camera ${(payload.camera || 0) + 1}: s` : 'S';
        toast(`${where}everity is now ${payload.severity} (${payload.count} clusters)`);
      }
    }
  });

  // -- weekly stats ------------------------------------------------------

  const refreshStats = async () => {
    const stats = await fetchJson<WeeklyStats>(`/api/stats/weekly?locale=${locale()}`);
    if (!stats) return;
    renderKpis(document, stats);
    renderFarm(document, stats);

    const stamp = document.querySelector('[data-updated]');
    if (stamp) stamp.textContent = `updated ${relative(new Date().toISOString())}`;
  };
  poll(refreshStats, STATS_INTERVAL_MS);

  // Data cleared from Settings, on this phone or another: drop what is shown.
  bus.on('cleared', () => {
    if (history) history.load();
    refreshStats();
  });

  fetchJson<Health>('/api/health').then(checkClock).catch(() => {});
  bus.start();
  window.addEventListener('beforeunload', () => {
    bus.stop();
    views.forEach((view) => view.destroy());
  });
}

// The status chip in the top bar, and the banner under it, on every page.
// Not running has three reasons: offline, no camera plugged in, or a restart
// on purpose (a new session, or a camera plugged in), which is no alarm.
function showWorker(state: WorkerState): void {
  const alive = state.alive === true;
  const chip = document.querySelector('[data-worker-chip]');
  if (chip) {
    chip.classList.toggle('is-live', alive);
    chip.classList.toggle('is-down', !alive);
    const label = chip.querySelector('[data-worker-label]');
    if (label) {
      if (isSimple()) label.textContent = word(alive ? 'chip.watching' : 'chip.not_watching');
      else label.textContent = alive ? 'Detector online' : 'Detector offline';
    }
  }

  const banner = document.querySelector<HTMLElement>('[data-worker-banner]');
  if (banner) {
    banner.hidden = alive;
    const status = state.status ?? '';
    const reason = ['no_camera', 'restarting', 'error'].includes(status) ? status : 'offline';
    banner.classList.toggle('is-offline', reason !== 'restarting');
    const text = banner.querySelector('[data-banner-text]');
    if (text) text.textContent = word(`banner.${reason}`);
    const ssh = banner.querySelector<HTMLElement>('[data-banner-ssh]');
    if (ssh) ssh.hidden = reason !== 'offline';
  }
}

// Low on SD card space: the worker has stopped saving photos (low) or
// everything (full). Said in both views; the full view adds the figure.
function showStorage(level: string | undefined, freeMb: number | null | undefined): void {
  const banner = document.querySelector<HTMLElement>('[data-storage-banner]');
  if (!banner) return;
  banner.hidden = !level || level === 'ok';
  if (banner.hidden) return;
  let text = word(level === 'full' ? 'banner.storage_full' : 'banner.storage_low');
  if (!isSimple() && freeMb != null) text += ` (${Math.round(freeMb)} MB free)`;
  banner.querySelector('[data-storage-text]')!.textContent = text;
}

// The Pi has no network time in the field; without its RTC battery the clock
// restarts from its last saved time after every power cut, and every photo is
// misdated. The phone's clock is the best check there is.
function checkClock(health: Health | null): void {
  const banner = document.querySelector<HTMLElement>('[data-clock-banner]');
  if (!banner || !health || !health.server_time) return;
  const skewMs = Date.parse(health.server_time) - Date.now();
  banner.hidden = Math.abs(skewMs) < 5 * 60 * 1000;
  if (banner.hidden) return;
  const minutes = Math.round(Math.abs(skewMs) / 60000);
  const span = minutes >= 120 ? `${Math.floor(minutes / 60)} h ${minutes % 60} min` : `${minutes} min`;
  banner.querySelector('[data-clock-text]')!.textContent =
    `The Pi's clock is ${span} ${skewMs < 0 ? 'behind' : 'ahead of'} this phone. ` +
    'Photos and records are being misdated. Fit the RTC battery (J5) so the clock survives power cuts.';
}

// With the detector down the live tiles and the camera hold their last
// values; dim them and say when those were, so they are not read as live.
function showStale(alive: boolean, readingAt: number | null, frameAt: number | null): void {
  const at = (ms: number | null) => (ms ? new Date(ms).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) : null);
  const reading = at(readingAt);
  document.querySelectorAll('[data-live-figure]').forEach((tile) => {
    tile.classList.toggle('is-stale', !alive);
    const note = tile.querySelector<HTMLElement>('[data-stale-note]');
    if (note) {
      note.hidden = alive;
      note.textContent = reading ? `Last reading ${reading}` : 'No live reading';
    }
  });

  document.querySelectorAll('[data-viewport]').forEach((viewport) => {
    viewport.classList.toggle('is-stale', !alive);
  });
  const frame = at(frameAt);
  document.querySelectorAll<HTMLElement>('[data-viewport-stale]').forEach((chip) => {
    chip.hidden = alive;
    const label = chip.querySelector('[data-viewport-stale-label]');
    if (!label) return;
    if (isSimple()) label.textContent = frame ? word('live.stale', { time: frame }) : word('live.stale_plain');
    else label.textContent = frame ? `Detector offline · frame from ${frame}` : 'Detector offline';
  });
}

// Pages without the live bus still ask once, so their chip tells the truth.
function checkWorker(): void {
  fetchJson<Health>('/api/health')
    .then((health) => {
      if (health && health.worker) showWorker(health.worker);
      checkClock(health);
    })
    .catch(() => {});
}

function bootSettings(): void {
  checkWorker();
  initSettings(document).catch((error) => console.error('[pw] settings failed', error));
}

// Once per phone: the PepperWatch Wi-Fi has no internet, and phones offer to
// leave such a network. Saying so up front keeps farmers connected.
function showWifiTip(): void {
  const tip = document.querySelector<HTMLElement>('[data-wifi-tip]');
  if (!tip) return;
  try {
    if (localStorage.getItem('pw-tip-wifi')) return;
  } catch (error) {
    return; // nowhere to remember it was read: don't nag on every page
  }
  tip.hidden = false;
  tip.querySelector('[data-wifi-tip-close]')?.addEventListener('click', () => {
    tip.hidden = true;
    try {
      localStorage.setItem('pw-tip-wifi', '1');
    } catch (error) {
      // Shown again next time.
    }
  });
}

applyWords(document);
showWifiTip();
const page = document.body.dataset.page;
if (page === 'dashboard') bootDashboard();
else if (page === 'settings') bootSettings();
