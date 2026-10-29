// Performance & Power: the System Health Monitor of Table 3.4, cut down to
// the figures the results chapter reports.
//
// Inference rate is drawn against the §3.2 target on a meter scaled to twice
// it, so the tick at the midpoint is the 17 fps line. The thermal backoff
// notice stays: when the enclosure heats up the worker stretches its
// inference interval on purpose, and without it the rate would sag on screen
// for no visible reason.

import { num, temp } from './fmt.js';

// The Pi 5 soft-throttles at 80 °C. The governor starts backing off at 75,
// so warn a little before that.
const TEMP_WARN = 70;
const TEMP_DANGER = 80;
// Display bands for the Hailo die, on the same 0-100 °C meter.
const HAILO_WARN = 80;
const HAILO_DANGER = 90;

const POWER_SOURCES = {
  ina219: 'INA219, whole device',
  pmic: 'PMIC estimate, board rails only',
};

export function renderTelemetry(root, telemetry) {
  if (!telemetry) return;

  setMeter(root, 'health-temp', temp(telemetry.cpu_temp_c), telemetry.cpu_temp_c, TEMP_WARN, TEMP_DANGER, 100);
  setText(root, 'health-cpu', telemetry.cpu_percent == null ? '—' : `${num(telemetry.cpu_percent, 0)}%`);

  const hailo = root.querySelector('[data-health-hailo]');
  if (hailo) {
    hailo.hidden = telemetry.hailo_temp_c == null;
    setMeter(root, 'health-hailo-temp', temp(telemetry.hailo_temp_c), telemetry.hailo_temp_c,
      HAILO_WARN, HAILO_DANGER, 100);
  }

  const watts = telemetry.best_power_w ?? telemetry.power_w;
  setText(root, 'health-power', watts == null ? '—' : `${num(watts, 2)} W`);
  setText(root, 'health-power-source', POWER_SOURCES[telemetry.power_source] || 'no power sensor');

  const backoff = root.querySelector('[data-health-backoff]');
  if (backoff) {
    const level = telemetry.thermal_level || 0;
    backoff.hidden = level === 0;
    if (level > 0) {
      const multiplier = telemetry.thermal_multiplier || 2;
      backoff.textContent =
        `Thermal backoff level ${level}: inference slowed ${multiplier}x at ` +
        `${temp(telemetry.cpu_temp_c)} to protect the processor. The live preview is unaffected.`;
    }
  }

  const throttled = root.querySelector('[data-health-throttled]');
  if (throttled) {
    throttled.hidden = telemetry.throttled !== true;
    setText(root, 'health-throttle-detail', telemetry.throttle_detail ? `${telemetry.throttle_detail}.` : '');
  }
}

export function renderPipeline(root, pipeline) {
  if (!pipeline) return;

  // The chips over the live feed.
  setText(root, 'pipeline-preview-fps', `${num(pipeline.preview_fps, 1)} fps`);
  setText(root, 'pipeline-inference-fps', `${num(pipeline.inference_fps, 1)} fps`);

  setText(root, 'pipeline-inference-rate', `${num(pipeline.inference_fps, 1)} fps`);
  setText(root, 'pipeline-preview-rate', `${num(pipeline.preview_fps, 1)} fps`);
  setText(root, 'pipeline-latency', pipeline.inference_ms == null ? '—' : `${num(pipeline.inference_ms, 1)} ms`);
  setText(root, 'pipeline-dropped', String(pipeline.dropped ?? 0));

  const target = pipeline.target_fps;
  const fps = pipeline.inference_fps || 0;
  const meter = root.querySelector('[data-pipeline-inference-meter]');
  if (meter && target) {
    const bar = meter.querySelector('i');
    if (bar) bar.style.width = `${(Math.min(fps / (2 * target), 1) * 100).toFixed(1)}%`;
    meter.classList.toggle('is-short', fps < target);
  }

  // Reported against the target rather than hidden: an honest shortfall is
  // more useful than a number that looks like it passed.
  const note = root.querySelector('[data-pipeline-target]');
  if (note && target) {
    note.textContent = fps >= target ? `meets the ${target} fps target` : `below the ${target} fps target`;
  }

  const paused = root.querySelector('[data-pipeline-paused]');
  if (paused) paused.hidden = pipeline.paused !== true;
}

export function renderSummary(root, summary) {
  if (!summary) return;
  setText(root, 'session-energy', summary.energy_wh == null ? '—' : `${num(summary.energy_wh, 3)} Wh`);
  setText(root, 'session-efficiency',
    summary.fps_per_watt == null ? 'fps per watt after the first minute' : `${num(summary.fps_per_watt, 2)} fps per watt`);
  setText(root, 'session-p95', summary.total_ms_p95 == null ? '—' : `${num(summary.total_ms_p95, 1)} ms`);
}

function setText(root, key, value) {
  const el = root.querySelector(`[data-${key}]`);
  if (el) el.textContent = value;
}

function setMeter(root, key, text, value, warn, danger, max) {
  setText(root, key, text);
  const meter = root.querySelector(`[data-${key}-meter]`);
  if (!meter) return;

  const bar = meter.querySelector('i');
  const ratio = value == null ? 0 : Math.max(0, Math.min(value / max, 1));
  if (bar) bar.style.width = `${(ratio * 100).toFixed(1)}%`;

  meter.classList.toggle('is-warn', value != null && value >= warn && value < danger);
  meter.classList.toggle('is-danger', value != null && value >= danger);
}
