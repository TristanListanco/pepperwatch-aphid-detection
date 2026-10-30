// The weekly figures (Figure 3.4): the leaf-health tile, which carries the
// severity, plus Damaged leaves, Avg. Confidence and the damage Trend.

import { num, pct, severityLabel, signedPct } from './fmt.js';

const TREND_ICONS = { rising: 'i-trending-up', falling: 'i-trending-down', stable: 'i-minus' };

export function renderKpis(root, stats) {
  set(root, 'confidence', pct(stats.avg_confidence, 0));
  // The icon carries the direction; the value is the word. With no scans the
  // week before there is nothing to compare, so no direction either.
  const trend = stats.events_previous ? stats.trend : null;
  set(root, 'trend', trend ? capitalise(trend) : 'Too early');
  const trendIcon = root.querySelector('[data-kpi-trend-icon]');
  if (trendIcon) trendIcon.setAttribute('href', `#${TREND_ICONS[trend] || 'i-minus'}`);
  // Says what the badge measures: "Low" alone, next to a health figure, reads
  // as "health is low".
  set(root, 'severity', stats.severity && stats.severity !== 'none'
    ? `${severityLabel(stats.severity)} damage`
    : 'No damage');

  renderDamaged(root, stats);

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

// Aphid-damaged leaves this week against all leaves scanned, and the change
// on last week. Rising damage is bad news, so "up" is coloured as a warning
// rather than the green a financial dashboard would use.
function renderDamaged(root, stats) {
  const distribution = stats.distribution || {};
  const damaged = distribution.aphid_damage || 0;
  const total = damaged + (distribution.healthy || 0);
  set(root, 'damaged', String(damaged));

  const scanned = root.querySelector('[data-kpi-scanned]');
  if (scanned) {
    scanned.textContent = total
      ? `of ${total} ${total === 1 ? 'leaf' : 'leaves'} scanned`
      : 'No leaves scanned this week yet';
  }

  const delta = root.querySelector('[data-kpi-delta]');
  if (!delta) return;
  // null: damage this week and none last week, so there is no percentage.
  const change = stats.delta_pct ?? null;
  const compared = Boolean(stats.events_previous);
  if (!compared) {
    delta.textContent = 'No scans last week to compare';
  } else if (change === null) {
    delta.textContent = 'None last week';
  } else if (Math.round(change) === 0) {
    delta.textContent = 'Same as last week';
  } else {
    delta.textContent = `${signedPct(change, 0)} on last week`;
  }
  delta.classList.toggle('is-up', compared && (change === null || Math.round(change) > 0));
  delta.classList.toggle('is-down', compared && change !== null && Math.round(change) < 0);
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
