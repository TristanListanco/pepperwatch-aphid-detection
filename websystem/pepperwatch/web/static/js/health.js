// The System Health Monitor (Table 3.4).
//
// More than decoration: this is where the thermal governor's behaviour becomes
// visible. When the enclosure gets hot the worker stretches its inference
// interval on purpose, and without somewhere to say so an observer would see
// the frame rate sag for no apparent reason. The backoff notice below is what
// turns "it got slow" into "it got hot, and here is what it did about it".
//
// A synthetic badge appears whenever the numbers are modelled rather than
// measured, so development data can never be mistaken for a field reading.

import { num, temp } from './fmt.js';

// The Pi 5 soft-throttles at 80 °C and harder at 85. The governor starts
// backing off at 75, so warn a little before that.
const TEMP_WARN = 70;
const TEMP_DANGER = 80;

export function renderHealth(root, telemetry) {
  if (!telemetry) return;

  setMetric(root, 'temp', temp(telemetry.cpu_temp_c), telemetry.cpu_temp_c, TEMP_WARN, TEMP_DANGER, 95);
  setMetric(root, 'cpu', telemetry.cpu_percent == null ? '—' : `${num(telemetry.cpu_percent, 0)}%`,
    telemetry.cpu_percent, 75, 92, 100);

  const memPct = telemetry.mem_percent;
  setMetric(
    root, 'mem',
    telemetry.mem_used_mb == null ? '—' : `${num(telemetry.mem_used_mb / 1024, 1)} GB`,
    memPct, 80, 92, 100,
  );

  setText(root, 'power', telemetry.power_w == null ? '—' : `${num(telemetry.power_w, 2)} W`);
  setText(root, 'voltage', telemetry.voltage_v == null ? '—' : `${num(telemetry.voltage_v, 2)} V`);
  setText(root, 'battery', telemetry.battery_pct == null ? '—' : `${num(telemetry.battery_pct, 0)}%`);
  setText(root, 'disk', telemetry.disk_free_mb == null ? '—' : `${num(telemetry.disk_free_mb / 1024, 1)} GB free`);

  const synthetic = root.querySelector('[data-health-synthetic]');
  if (synthetic) synthetic.hidden = telemetry.synthetic !== true;

  const backoff = root.querySelector('[data-health-backoff]');
  if (backoff) {
    const level = telemetry.thermal_level || 0;
    backoff.hidden = level === 0;
    if (level > 0) {
      const multiplier = telemetry.thermal_multiplier || 2;
      backoff.textContent =
        `Thermal backoff level ${level}: inference interval increased ${multiplier}x ` +
        `at ${temp(telemetry.cpu_temp_c)} to protect the processor. ` +
        'The live preview is unaffected.';
    }
  }

  const throttled = root.querySelector('[data-health-throttled]');
  if (throttled) throttled.hidden = telemetry.throttled !== true;
}

export function renderPipeline(root, pipeline) {
  if (!pipeline) return;

  setText(root, 'preview-fps', `${num(pipeline.preview_fps, 1)} fps`);
  setText(root, 'inference-fps', `${num(pipeline.inference_fps, 1)} fps`);
  setText(root, 'latency', pipeline.inference_ms == null ? '—' : `${num(pipeline.inference_ms, 0)} ms`);
  setText(root, 'dropped', String(pipeline.dropped ?? 0));

  // Reported against the §3.2 target rather than hidden: an honest shortfall is
  // more useful than a number that looks like it passed.
  const target = root.querySelector('[data-pipeline-target]');
  if (target && pipeline.target_fps) {
    const meets = (pipeline.inference_fps || 0) >= pipeline.target_fps;
    target.textContent = meets
      ? `meets the ${pipeline.target_fps} fps target`
      : `below the ${pipeline.target_fps} fps target`;
    // Inline, not .kpi-sub: that class is display:flex and would break this
    // out of the surrounding sentence onto its own line.
    target.className = meets ? 'inline-down' : 'inline-up';
  }

  const paused = root.querySelector('[data-pipeline-paused]');
  if (paused) paused.hidden = pipeline.paused !== true;
}

function setText(root, key, value) {
  const el = root.querySelector(`[data-health-${key}], [data-pipeline-${key}]`);
  if (el) el.textContent = value;
}

function setMetric(root, key, text, value, warn, danger, max) {
  setText(root, key, text);
  const meter = root.querySelector(`[data-health-${key}-meter]`);
  if (!meter) return;

  const bar = meter.querySelector('i');
  const ratio = value == null ? 0 : Math.max(0, Math.min(value / max, 1));
  if (bar) bar.style.width = `${(ratio * 100).toFixed(1)}%`;

  meter.classList.toggle('is-warn', value != null && value >= warn && value < danger);
  meter.classList.toggle('is-danger', value != null && value >= danger);
}
