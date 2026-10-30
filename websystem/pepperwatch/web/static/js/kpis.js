// The weekly figures (Figure 3.4): the leaf-health tile, which carries the
// severity, plus Weekly Detections, Avg. Confidence and Trend.

import { num, pct, severityLabel, signedPct } from './fmt.js';

export function renderKpis(root, stats) {
  set(root, 'detections', String(stats.detections ?? 0));
  set(root, 'confidence', pct(stats.avg_confidence, 0));
  // The tile's own trending icon carries the direction; the value is the word.
  set(root, 'trend', capitalise(stats.trend));
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

  renderHealth(root, stats);

  const eventsNote = root.querySelector('[data-kpi-events]');
  if (eventsNote) {
    const events = stats.events ?? 0;
    eventsNote.textContent = `${events} scan${events === 1 ? '' : 's'} logged`;
  }

}

// Share of this week's detected leaves that were healthy. The tile turns amber
// or red with the severity band, so its colour carries the reading at a
// glance from the end of a pepper row.
function renderHealth(root, stats) {
  const tile = root.querySelector('[data-health-tile]');
  if (!tile) return;

  const severity = stats.severity || 'none';
  ['none', 'low', 'moderate', 'high'].forEach((band) => {
    tile.classList.toggle(`is-${band}`, band === severity);
  });

  const distribution = stats.distribution || {};
  const healthy = distribution.healthy || 0;
  const damaged = distribution.aphid_damage || 0;
  const total = healthy + damaged;

  const share = root.querySelector('[data-health-share]');
  if (share) share.textContent = total ? num((100 * healthy) / total, 0) : '—';

  const note = root.querySelector('[data-health-note]');
  if (note) {
    const headline = stats.advice && stats.advice.headline;
    note.textContent = total
      ? headline || `${healthy} of ${total} leaves healthy this week`
      : 'No leaves scanned this week yet';
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
