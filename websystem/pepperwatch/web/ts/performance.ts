// Performance & Power: the System Health Monitor of Table 3.4, cut down to
// the figures the results chapter reports.
//
// Inference rate is drawn against the §3.2 target on a meter scaled to twice
// it, so the tick at the midpoint is the 17 fps line. The thermal backoff
// notice stays: when the enclosure heats up the worker stretches its
// inference interval on purpose, and without it the rate would sag on screen
// for no visible reason.

import type { Pipeline, Summary, Telemetry } from './api.js';
import { num, temp } from './fmt.js';

// The Pi 5 soft-throttles at 80 °C. The governor starts backing off at 75,
// so warn a little before that.
const TEMP_WARN = 70;
const TEMP_DANGER = 80;
// Display bands for the Hailo die, on the same 0-100 °C meter.
const HAILO_WARN = 80;
const HAILO_DANGER = 90;
// Near 100% busy the HAT, not the camera or CPU, caps the frame rate.
const BUSY_WARN = 80;
const BUSY_DANGER = 95;

const POWER_SOURCES: Record<string, string> = {
  ina219: 'INA219, whole device',
  pmic: 'PMIC estimate, board rails only',
};

export function renderTelemetry(root: ParentNode, telemetry: Telemetry | null): void {
  if (!telemetry) return;

  // Figures go in as bare numbers: each tile renders its unit beside the
  // value in a lighter weight.
  setMeter(root, 'health-temp', num(telemetry.cpu_temp_c, 1), telemetry.cpu_temp_c, TEMP_WARN, TEMP_DANGER, 100);
  setText(root, 'health-cpu', telemetry.cpu_percent == null ? '—' : `${num(telemetry.cpu_percent, 0)}%`);

  const hat = root.querySelector<HTMLElement>('[data-hat-tile]');
  if (hat) {
    hat.hidden = telemetry.hailo_busy_pct == null && telemetry.hailo_temp_c == null;
    setText(root, 'hat-name', telemetry.accelerator || 'Hailo');
    setMeter(root, 'hat-busy', num(telemetry.hailo_busy_pct, 0), telemetry.hailo_busy_pct, BUSY_WARN, BUSY_DANGER, 100);
    setText(root, 'hat-infer', num(telemetry.hailo_infer_ms, 1));
    setMeter(root, 'health-hailo-temp', num(telemetry.hailo_temp_c, 1), telemetry.hailo_temp_c,
      HAILO_WARN, HAILO_DANGER, 100);
    const resting = root.querySelector<HTMLElement>('[data-hat-backoff]');
    if (resting) resting.hidden = !telemetry.thermal_level;
    // The HAT has no power sensor; only a meter on the supply counts its draw.
    setText(root, 'hat-power', telemetry.power_source === 'ina219'
      ? 'Its power is in the INA219 reading: the HAT has no sensor of its own.'
      : "Its power isn't in the PMIC estimate, which covers the Pi's own rails. "
        + 'An INA219 on the supply would include it.');
  }

  const watts = telemetry.best_power_w ?? telemetry.power_w;
  setText(root, 'health-power', num(watts, 2));
  setText(root, 'health-power-source', POWER_SOURCES[telemetry.power_source ?? ''] || 'no power sensor');

  const backoff = root.querySelector<HTMLElement>('[data-health-backoff]');
  if (backoff) {
    const level = telemetry.thermal_level || 0;
    backoff.hidden = level === 0;
    if (level > 0) {
      const multiplier = telemetry.thermal_multiplier || 2;
      const hat = telemetry.thermal_source === 'AI HAT+';
      backoff.textContent =
        `Thermal backoff level ${level}: inference slowed ${multiplier}x at ` +
        `${temp(hat ? telemetry.hailo_temp_c : telemetry.cpu_temp_c)} to protect the ` +
        `${hat ? 'AI HAT+' : 'processor'}. The live preview is unaffected.`;
    }
  }

  const throttled = root.querySelector<HTMLElement>('[data-health-throttled]');
  if (throttled) {
    throttled.hidden = telemetry.throttled !== true;
    // Under-voltage now: the fix is the supply, and the HAT is what drops out first.
    const sagging = ((telemetry.throttled_flags ?? 0) & 1) !== 0;
    const hat = telemetry.hailo_busy_pct != null || telemetry.hailo_temp_c != null;
    const supply = sagging
      ? `The power supply can't keep up${hat ? ', and the AI HAT+ may stop answering' : ''}: use the `
        + 'official 27 W supply. An INA219 wired into the supply line lowers the voltage further.'
      : '';
    setText(root, 'health-throttle-detail',
      [telemetry.throttle_detail ? `${telemetry.throttle_detail}.` : '', supply].filter(Boolean).join(' '));
  }
}

export function renderPipeline(root: ParentNode, pipeline: Pipeline | null): void {
  if (!pipeline) return;

  setText(root, 'pipeline-inference-rate', num(pipeline.inference_fps, 1));
  // One rate per camera that is on: "30.2 · 29.8".
  const on = pipeline.camera_on || [];
  const rates = (pipeline.camera_fps || [pipeline.preview_fps]).filter((fps, index) => on[index] !== false);
  setText(root, 'pipeline-preview-rate', rates.map((fps) => num(fps, 1)).join(' · '));
  setText(root, 'pipeline-latency', num(pipeline.inference_ms, 1));
  setText(root, 'pipeline-dropped', String(pipeline.dropped ?? 0));

  const target = pipeline.target_fps;
  const fps = pipeline.inference_fps || 0;
  const meter = root.querySelector('[data-pipeline-inference-meter]');
  if (meter && target) {
    const bar = meter.querySelector<HTMLElement>('i');
    if (bar) bar.style.width = `${(Math.min(fps / (2 * target), 1) * 100).toFixed(1)}%`;
    meter.classList.toggle('is-short', fps < target);
  }

  // Reported against the target rather than hidden: an honest shortfall is
  // more useful than a number that looks like it passed.
  const note = root.querySelector('[data-pipeline-target]');
  if (note && target) {
    note.textContent = fps >= target ? `meets the ${target} fps target` : `below the ${target} fps target`;
  }

  root.querySelectorAll<HTMLElement>('[data-pipeline-paused]').forEach((chip) => {
    chip.hidden = pipeline.paused !== true;
  });
}

export function renderSummary(root: ParentNode, summary: Summary | null | undefined): void {
  if (!summary) return;
  setText(root, 'session-energy', summary.energy_wh == null ? '—' : `${num(summary.energy_wh, 3)} Wh`);
  const hatUnmeasured = summary.detector === 'hailo' && summary.power_source !== 'ina219';
  setText(root, 'session-efficiency', summary.fps_per_watt != null
    ? `${num(summary.fps_per_watt, 2)} fps per watt`
    : hatUnmeasured ? 'no fps per watt: the PMIC misses the HAT' : 'fps per watt after the first minute');
  setText(root, 'session-p95', summary.total_ms_p95 == null ? '—' : `${num(summary.total_ms_p95, 1)} ms`);
}

function setText(root: ParentNode, key: string, value: string): void {
  const el = root.querySelector(`[data-${key}]`);
  if (el) el.textContent = value;
}

function setMeter(
  root: ParentNode, key: string, text: string, value: number | null | undefined, warn: number, danger: number, max: number,
): void {
  setText(root, key, text);
  const meter = root.querySelector(`[data-${key}-meter]`);
  if (!meter) return;

  const bar = meter.querySelector<HTMLElement>('i');
  const ratio = value == null ? 0 : Math.max(0, Math.min(value / max, 1));
  if (bar) bar.style.width = `${(ratio * 100).toFixed(1)}%`;

  meter.classList.toggle('is-warn', value != null && value >= warn && value < danger);
  meter.classList.toggle('is-danger', value != null && value >= danger);
}
