// The Settings page: the simple or full view, display preferences, detection
// thresholds, how often photos are saved, locale, and clearing stored detections.
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

  const showConf = bindSlider(root, 'conf', config.inference.conf_threshold);
  const showIou = bindSlider(root, 'iou', config.inference.iou_threshold);
  bindReset(root, config.inference.defaults, () => {
    showConf(config.inference.defaults.conf_threshold);
    showIou(config.inference.defaults.iou_threshold);
  });
  bindIntervals(root, config.persist);

  bindSite(root, config.site.name);

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

// The farm's name: on every page and on each new photo. No PIN; anyone may.
function bindSite(root: ParentNode, initial: string): void {
  const form = root.querySelector<HTMLFormElement>('[data-site-form]');
  const input = root.querySelector<HTMLInputElement>('[data-setting-site]');
  const status = root.querySelector('[data-site-status]');
  if (!form || !input || !status) return;
  input.value = initial;
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = form.querySelector('button');
    if (button) button.disabled = true;
    status.className = 'note';
    try {
      const { name } = await patchJson<{ name: string }>('/api/site', { name: input.value });
      input.value = name;
      document.querySelectorAll('[data-site-name]').forEach((heading) => {
        heading.textContent = name;
      });
      document.title = `${document.title.split(' · ')[0]} · ${name}`;
      status.textContent = 'Saved. Other phones show the new name when they next open the page.';
    } catch (error) {
      status.textContent = `Not saved: ${errorText(error)}`;
      status.className = 'note sev-high';
    } finally {
      if (button) button.disabled = false;
    }
  });
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

// Returns how to show a value the detector has taken, set from elsewhere.
function bindSlider(root: ParentNode, key: 'conf' | 'iou', initial: number): (value: number) => void {
  const slider = root.querySelector<HTMLInputElement>(`[data-slider-${key}]`);
  const readout = root.querySelector(`[data-slider-${key}-value]`);
  const status = root.querySelector('[data-settings-status]');
  if (!slider) return () => {};

  let applied = initial;
  const show = (value: number) => {
    applied = value;
    slider.value = String(value);
    if (readout) readout.textContent = num(value, 2);
  };
  show(initial);

  slider.addEventListener('input', () => {
    if (readout) readout.textContent = num(Number(slider.value), 2);
  });

  // Commit on release rather than on every input event: dragging the slider
  // would otherwise fire a PATCH per pixel, and each one republishes to the
  // worker over MQTT.
  slider.addEventListener('change', async () => {
    const field = key === 'conf' ? 'conf_threshold' : 'iou_threshold';
    if (await applySetting({ [field]: Number(slider.value) }, status)) {
      applied = Number(slider.value);
    } else {
      show(applied); // what the detector is really using, not the value that failed
    }
  });
  return show;
}

// Both thresholds back to the values in the Pi's config files, in one change.
function bindReset(root: ParentNode, defaults: Config['inference']['defaults'], onApplied: () => void): void {
  const button = root.querySelector<HTMLButtonElement>('[data-thresholds-reset]');
  const note = root.querySelector('[data-thresholds-defaults]');
  if (!button) return;
  if (note) {
    note.textContent = `Confidence ${num(defaults.conf_threshold, 2)} and NMS IoU ${num(defaults.iou_threshold, 2)}, as set in the Pi's config files.`;
  }
  button.addEventListener('click', async () => {
    button.disabled = true;
    if (await applySetting(defaults, root.querySelector('[data-settings-status]'))) onApplied();
    button.disabled = false;
  });
}

function bindIntervals(root: ParentNode, initial: Config['persist']): void {
  const status = root.querySelector('[data-interval-status]');
  root.querySelectorAll<HTMLSelectElement>('[data-setting-interval]').forEach((select) => {
    const field = select.dataset.settingInterval as keyof Config['persist'];
    let applied = initial[field];
    // A value set in the config files may not be one of the choices; show it
    // anyway, so the page never claims an interval the Pi is not using.
    if (![...select.options].some((option) => Number(option.value) === applied)) {
      select.add(new Option(`${num(applied, 0)} seconds`, String(applied)));
    }
    select.value = String(applied);
    select.addEventListener('change', async () => {
      if (await applySetting({ [field]: Number(select.value) }, status)) applied = Number(select.value);
      else select.value = String(applied);
    });
  });
}

// Sends one change and says whether the worker took it.
async function applySetting(changes: Record<string, number>, status: Element | null): Promise<boolean> {
  if (status) {
    status.textContent = 'Applying…';
    status.className = 'note';
  }
  try {
    // Answers only once the worker reports the new value, a second or two.
    await patchJson('/api/config', changes, adminHeaders());
    if (status) status.textContent = 'Applied: the detector is using it now, and keeps it after restarts.';
    return true;
  } catch (error) {
    if (status) {
      status.textContent = `Not applied: ${errorText(error)}`;
      status.className = 'note sev-high';
    }
    return false;
  }
}
