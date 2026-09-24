// The Settings page: detection thresholds, locale, and the control PIN.
//
// The confidence slider matters more than it looks. Figure 3.3 specifies a
// fixed "Confidence Filter (80)", but 0.80 on small aphid clusters would
// discard most true detections, and §4 counts False Negatives -- infestations
// missed entirely -- as a headline metric. Exposing the threshold lets the
// value be chosen from a measured precision/recall sweep instead of asserted,
// which is a stronger result to defend.

import { fetchJson, patchJson } from './bus.js';
import { num } from './fmt.js';

export async function initSettings(root) {
  const config = await fetchJson('/api/config');
  if (!config) return;

  bindSlider(root, 'conf', config.inference.conf_threshold);
  bindSlider(root, 'iou', config.inference.iou_threshold);

  const site = root.querySelector('[data-setting-site]');
  if (site) site.textContent = config.site.name;

  const model = root.querySelector('[data-setting-model]');
  if (model) {
    model.textContent = `${config.inference.input_size}px · ${config.inference.class_names.join(', ')}`;
  }

  const advisory = root.querySelector('[data-setting-advisory]');
  if (advisory) {
    const meta = config.advisory || {};
    advisory.textContent = meta.status || 'unknown';
    advisory.className = String(meta.status || '').toLowerCase().startsWith('validated')
      ? 'sev-none'
      : 'sev-moderate';
  }

  const pinField = root.querySelector('[data-setting-pin]');
  const pinRow = root.querySelector('[data-setting-pin-row]');
  if (pinRow) pinRow.hidden = !config.web.pin_required;
  if (pinField) {
    pinField.value = sessionStorage.getItem('pw-pin') || '';
    // sessionStorage, not localStorage: the PIN should not outlive the tab on a
    // phone that gets handed around a farm.
    pinField.addEventListener('change', () => {
      if (pinField.value) sessionStorage.setItem('pw-pin', pinField.value);
      else sessionStorage.removeItem('pw-pin');
    });
  }

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

function bindSlider(root, key, initial) {
  const slider = root.querySelector(`[data-slider-${key}]`);
  const readout = root.querySelector(`[data-slider-${key}-value]`);
  const status = root.querySelector('[data-settings-status]');
  if (!slider) return;

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
    try {
      const result = await patchJson('/api/config', { [field]: Number(slider.value) });
      if (status) {
        status.textContent = result.forwarded_to_worker
          ? 'Applied and sent to the detector.'
          : 'Applied here; the worker is offline and will pick it up on restart.';
        status.className = 'note';
      }
    } catch (error) {
      if (status) {
        status.textContent = `Could not apply: ${error.message}`;
        status.className = 'note sev-high';
      }
    }
  });
}
