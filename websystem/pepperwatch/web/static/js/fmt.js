// Formatting helpers. Locale-aware where it matters, deliberately plain where
// it does not: this is read on a phone in a pepper row, not in an office.

export const SEVERITY_LABELS = {
  none: 'None',
  low: 'Low',
  moderate: 'Moderate',
  high: 'High',
};

export const CLASS_LABELS = {
  healthy: 'Healthy',
  aphid_damage: 'Aphid Damage',
};

// Matches render.py's COLOUR_* constants, so a box drawn over the live feed is
// the same colour as one baked into an archived JPEG. Tuned to stand out on a
// camera image; the donut uses darker ones for the light card.
export const CLASS_COLOURS = {
  healthy: '#5cbe6e',
  aphid_damage: '#eb8228',
};

export const SEVERITY_COLOURS = {
  none: '#5cbe6e',
  low: '#a3cc52',
  moderate: '#f5c451',
  high: '#e5484d',
};

export function classLabel(name) {
  return CLASS_LABELS[name] || name;
}

export function severityLabel(name) {
  return SEVERITY_LABELS[name] || name || '—';
}

export function classColour(name) {
  return CLASS_COLOURS[name] || '#9aa8a4';
}

export function pct(value, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return `${(value * 100).toFixed(digits)}%`;
}

export function num(value, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return Number(value).toFixed(digits);
}

export function signedPct(value, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  const fixed = Math.abs(value).toFixed(digits);
  // A true minus, and no sign at all on a value that rounds to zero.
  const sign = Number(fixed) === 0 ? '' : value > 0 ? '+' : '\u2212';
  return `${sign}${fixed}%`;
}

export function temp(celsius) {
  return celsius === null || celsius === undefined ? '—' : `${Number(celsius).toFixed(1)}°C`;
}

// Short local time. The Pi may have no RTC battery, in which case this shows a
// nonsense date -- which is itself useful information to see on screen.
export function clockTime(iso) {
  if (!iso) return '—';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

export function dateTime(iso) {
  if (!iso) return '—';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleString([], {
    year: 'numeric',
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function relative(iso) {
  if (!iso) return '—';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '—';
  const seconds = Math.max(0, (Date.now() - then) / 1000);
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

export function bytes(value) {
  if (!value) return '—';
  const units = ['B', 'KB', 'MB', 'GB'];
  let scaled = value;
  let unit = 0;
  while (scaled >= 1024 && unit < units.length - 1) {
    scaled /= 1024;
    unit += 1;
  }
  return `${scaled.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}
