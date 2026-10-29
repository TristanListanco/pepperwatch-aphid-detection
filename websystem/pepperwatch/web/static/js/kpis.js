// The four KPI tiles (Figure 3.4).
//
// The tiles are Weekly Detections, Avg. Confidence, Trend, and Severity.

import { num, pct, severityLabel, signedPct, trendGlyph } from './fmt.js';

export function renderKpis(root, stats) {
  set(root, 'detections', String(stats.detections ?? 0));
  set(root, 'confidence', pct(stats.avg_confidence, 0));
  set(root, 'trend', `${trendGlyph(stats.trend)} ${capitalise(stats.trend)}`);
  set(root, 'severity', severityLabel(stats.severity));

  // Rising detections are bad news here, so "up" is coloured as a warning
  // rather than the green a financial dashboard would use.
  const delta = root.querySelector('[data-kpi-delta]');
  if (delta) {
    delta.textContent = signedPct(stats.delta_pct, 0);
    delta.classList.toggle('is-up', (stats.delta_pct ?? 0) > 0);
    delta.classList.toggle('is-down', (stats.delta_pct ?? 0) < 0);
  }

  const confidenceNote = root.querySelector('[data-kpi-confidence-note]');
  if (confidenceNote) confidenceNote.textContent = confidenceBand(stats.avg_confidence);

  const severityEl = root.querySelector('[data-kpi-severity]');
  if (severityEl) {
    severityEl.className = `kpi-value sev-${stats.severity || 'none'}`;
  }

  const eventsNote = root.querySelector('[data-kpi-events]');
  if (eventsNote) {
    const events = stats.events ?? 0;
    eventsNote.textContent = `${events} scan${events === 1 ? '' : 's'} logged`;
  }

}

function confidenceBand(value) {
  if (!value) return 'No data';
  if (value >= 0.8) return 'High';
  if (value >= 0.5) return 'Moderate';
  return 'Low';
}

function capitalise(text) {
  if (!text) return '—';
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function set(root, key, value) {
  const el = root.querySelector(`[data-kpi-${key}]`);
  if (el) el.textContent = value;
}
