// The Settings page: the simple or full view, display preferences, detection
// thresholds, locale, and clearing stored detections.
//
// The confidence slider matters more than it looks. Figure 3.3 specifies a
// fixed "Confidence Filter (80)", but 0.80 on small aphid clusters would
// discard most true detections, and §4 counts False Negatives -- infestations
// missed entirely -- as a headline metric. Exposing the threshold lets the
// value be chosen from a measured precision/recall sweep instead of asserted,
// which is a stronger result to defend.

import { fetchJson, patchJson, postJson } from './bus.js';
import { num } from './fmt.js';
import { displayPrefs, setDisplayPref } from './liveview.js';

// The admin PIN, for this tab only: thresholds and clearing data need it.
function adminHeaders() {
  let pin = '';
  try {
    pin = window.sessionStorage.getItem('pw-admin-pin') || '';
  } catch (error) {
    // Blocked storage: read it from the field instead.
  }
  const field = document.querySelector('[data-admin-pin]');
  if (!pin && field) pin = field.value;
  return pin ? { 'X-Admin-Pin': pin } : {};
}

function bindPin(root) {
  const field = root.querySelector('[data-admin-pin]');
  if (!field) return;
  try {
    field.value = window.sessionStorage.getItem('pw-admin-pin') || '';
  } catch (error) {
    // Not kept; typed again after a reload.
  }
  field.addEventListener('input', () => {
    try {
      window.sessionStorage.setItem('pw-admin-pin', field.value.trim());
    } catch (error) {
      // Still sent from the field.
    }
  });
}

export async function initSettings(root) {
  bindDisplayPrefs(root);
  bindPin(root);
  bindClear(root);

  const config = await fetchJson('/api/config');
  if (!config) return;

  bindSlider(root, 'conf', config.inference.conf_threshold);
  bindSlider(root, 'iou', config.inference.iou_threshold);

  const site = root.querySelector('[data-setting-site]');
  if (site) site.textContent = config.site.name;

  const localeSelect = root.querySelector('[data-setting-locale]');
  if (localeSelect) {
    localeSelect.innerHTML = (config.locales || ['en'])
      .map((code) => `<option value="${code}">${code === 'ceb' ? 'Cebuano' : 'English'}</option>`)
      .join('');
    localeSelect.value = localStorage.getItem('pw-locale') || 'en';
    localeSelect.addEventListener('change', () => {
      localStorage.setItem('pw-locale', localeSelect.value);
      window.location.reload();
    });
  }
}

// Saved in this browser and read by the dashboard when it next opens.
function bindDisplayPrefs(root) {
  const themeSelect = root.querySelector('[data-setting-theme]');
  if (themeSelect && window.pwTheme) {
    themeSelect.value = window.pwTheme.get();
    themeSelect.addEventListener('change', () => window.pwTheme.set(themeSelect.value));
  }

  // Simple or full view: applied at once, so the cards below show or hide.
  const view = root.querySelector('[data-setting-view]');
  if (view && window.pwView) {
    view.querySelectorAll('input[name="view"]').forEach((input) => {
      input.checked = input.value === window.pwView.get();
      input.addEventListener('change', () => {
        const note = root.querySelector('[data-view-note]');
        if (note) note.hidden = window.pwView.set(input.value);
      });
    });
  }

  const prefs = displayPrefs();
  root.querySelectorAll('[data-pref]').forEach((input) => {
    input.checked = prefs[input.dataset.pref];
    input.addEventListener('change', () => setDisplayPref(input.dataset.pref, input.checked));
  });
}

// Two steps, inline: a pop-up confirm is easy to dismiss by reflex on a phone.
function bindClear(root) {
  const start = root.querySelector('[data-clear-start]');
  const confirm = root.querySelector('[data-clear-confirm]');
  const status = root.querySelector('[data-clear-status]');
  if (!start || !confirm) return;

  start.addEventListener('click', () => {
    confirm.hidden = false;
    start.hidden = true;
  });
  root.querySelector('[data-clear-no]').addEventListener('click', () => {
    confirm.hidden = true;
    start.hidden = false;
  });
  root.querySelector('[data-clear-yes]').addEventListener('click', async (event) => {
    event.target.disabled = true;
    try {
      const result = await postJson('/api/data/clear', { confirm: 'DELETE' }, adminHeaders());
      status.textContent = `Deleted ${result.deleted_events} detections and ${result.deleted_files} image files.`;
    } catch (error) {
      status.textContent = `Could not clear: ${error.message}`;
    } finally {
      event.target.disabled = false;
      confirm.hidden = true;
      start.hidden = false;
    }
  });
}

function bindSlider(root, key, initial) {
  const slider = root.querySelector(`[data-slider-${key}]`);
  const readout = root.querySelector(`[data-slider-${key}-value]`);
  const status = root.querySelector('[data-settings-status]');
  if (!slider) return;

  let applied = initial;
  slider.value = String(initial);
  if (readout) readout.textContent = num(initial, 2);

  slider.addEventListener('input', () => {
    if (readout) readout.textContent = num(Number(slider.value), 2);
  });

  // Commit on release rather than on every input event: dragging the slider
  // would otherwise fire a PATCH per pixel, and each one republishes to the
  // worker over MQTT.
  slider.addEventListener('change', async () => {
    const field = key === 'conf' ? 'conf_threshold' : 'iou_threshold';
    if (status) {
      status.textContent = 'Applying…';
      status.className = 'note';
    }
    try {
      // Answers only once the detector reports the new value, a second or two.
      await patchJson('/api/config', { [field]: Number(slider.value) }, adminHeaders());
      applied = Number(slider.value);
      if (status) status.textContent = 'Applied: the detector is using it now, and keeps it after restarts.';
    } catch (error) {
      // Show what the detector is really using, not the value that failed.
      slider.value = String(applied);
      if (readout) readout.textContent = num(applied, 2);
      if (status) {
        status.textContent = `Not applied: ${error.message}`;
        status.className = 'note sev-high';
      }
    }
  });
}
