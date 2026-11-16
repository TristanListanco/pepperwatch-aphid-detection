// The Settings page: the simple or full view, display preferences, detection
// thresholds, locale, and clearing stored detections.
//
// The confidence slider matters more than it looks. Figure 3.3 specifies a
// fixed "Confidence Filter (80)", but 0.80 on small aphid clusters would
// discard most true detections, and §4 counts False Negatives -- infestations
// missed entirely -- as a headline metric. Exposing the threshold lets the
// value be chosen from a measured precision/recall sweep instead of asserted,
// which is a stronger result to defend.

import type { ClearResult, Config } from './api.js';
import { fetchJson, patchJson, postJson } from './bus.js';
import { errorText, num } from './fmt.js';
import { displayPrefs, setDisplayPref, type DisplayPrefs } from './liveview.js';

// The admin PIN, for this tab only: thresholds and clearing data need it.
function adminHeaders(): Record<string, string> {
  let pin = '';
  try {
    pin = window.sessionStorage.getItem('pw-admin-pin') || '';
  } catch (error) {
    // Blocked storage: read it from the field instead.
  }
  const field = document.querySelector<HTMLInputElement>('[data-admin-pin]');
  if (!pin && field) pin = field.value;
  return pin ? { 'X-Admin-Pin': pin } : {};
}

function bindPin(root: ParentNode): void {
  const field = root.querySelector<HTMLInputElement>('[data-admin-pin]');
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

export async function initSettings(root: ParentNode): Promise<void> {
  bindDisplayPrefs(root);
  bindPin(root);
  bindClear(root);

  const config = await fetchJson<Config>('/api/config');
  if (!config) return;

  bindSlider(root, 'conf', config.inference.conf_threshold);
  bindSlider(root, 'iou', config.inference.iou_threshold);

  const site = root.querySelector('[data-setting-site]');
  if (site) site.textContent = config.site.name;

  const localeSelect = root.querySelector<HTMLSelectElement>('[data-setting-locale]');
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
function bindDisplayPrefs(root: ParentNode): void {
  const themeSelect = root.querySelector<HTMLSelectElement>('[data-setting-theme]');
  const theme = window.pwTheme;
  if (themeSelect && theme) {
    themeSelect.value = theme.get();
    themeSelect.addEventListener('change', () => theme.set(themeSelect.value));
  }

  // Simple or full view: applied at once, so the cards below show or hide.
  const view = root.querySelector('[data-setting-view]');
  const pwView = window.pwView;
  if (view && pwView) {
    view.querySelectorAll<HTMLInputElement>('input[name="view"]').forEach((input) => {
      input.checked = input.value === pwView.get();
      input.addEventListener('change', () => {
        const note = root.querySelector<HTMLElement>('[data-view-note]');
        if (note) note.hidden = pwView.set(input.value);
      });
    });
  }

  const prefs = displayPrefs();
  root.querySelectorAll<HTMLInputElement>('[data-pref]').forEach((input) => {
    const key = input.dataset.pref ?? '';
    input.checked = prefs[key as keyof DisplayPrefs] === true;
    input.addEventListener('change', () => setDisplayPref(key, input.checked));
  });
}

// Two steps, inline: a pop-up confirm is easy to dismiss by reflex on a phone.
function bindClear(root: ParentNode): void {
  const start = root.querySelector<HTMLElement>('[data-clear-start]');
  const confirm = root.querySelector<HTMLElement>('[data-clear-confirm]');
  const status = root.querySelector('[data-clear-status]');
  const yes = root.querySelector<HTMLButtonElement>('[data-clear-yes]');
  if (!start || !confirm || !status || !yes) return;

  start.addEventListener('click', () => {
    confirm.hidden = false;
    start.hidden = true;
  });
  root.querySelector('[data-clear-no]')?.addEventListener('click', () => {
    confirm.hidden = true;
    start.hidden = false;
  });
  yes.addEventListener('click', async () => {
    yes.disabled = true;
    try {
      const result = await postJson<ClearResult>('/api/data/clear', { confirm: 'DELETE' }, adminHeaders());
      status.textContent = `Deleted ${result.deleted_events} detections and ${result.deleted_files} image files.`;
    } catch (error) {
      status.textContent = `Could not clear: ${errorText(error)}`;
    } finally {
      yes.disabled = false;
      confirm.hidden = true;
      start.hidden = false;
    }
  });
}

function bindSlider(root: ParentNode, key: 'conf' | 'iou', initial: number): void {
  const slider = root.querySelector<HTMLInputElement>(`[data-slider-${key}]`);
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
        status.textContent = `Not applied: ${errorText(error)}`;
        status.className = 'note sev-high';
      }
    }
  });
}
