// Detection-distribution donut (Figure 3.4).
//
// Hand-rolled SVG rather than a charting library: nothing may load from a CDN
// on an offline device, and vendoring a chart library to draw one ring would
// add far more bytes over the Pi's radio than the ring is worth.

import { CLASS_COLOURS, classLabel } from './fmt.js';

const SIZE = 120;
const RADIUS = 46;
const STROKE = 18;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;

export function renderDonut(container, distribution, legendEl) {
  const entries = Object.entries(distribution || {}).filter(([, count]) => count > 0);
  const total = entries.reduce((sum, [, count]) => sum + count, 0);

  if (!total) {
    container.innerHTML = emptyRing();
    if (legendEl) legendEl.innerHTML = '<span class="muted">No detections in this period</span>';
    return;
  }

  // Sorted so the ring's colour order is stable between refreshes -- an order
  // that shuffles as counts change makes the chart hard to read at a glance.
  entries.sort((a, b) => b[1] - a[1]);

  let offset = 0;
  const arcs = entries
    .map(([name, count]) => {
      const fraction = count / total;
      const length = fraction * CIRCUMFERENCE;
      const arc = `
        <circle
          cx="${SIZE / 2}" cy="${SIZE / 2}" r="${RADIUS}"
          fill="none"
          stroke="${CLASS_COLOURS[name] || '#9aa8a4'}"
          stroke-width="${STROKE}"
          stroke-dasharray="${length.toFixed(2)} ${(CIRCUMFERENCE - length).toFixed(2)}"
          stroke-dashoffset="${(-offset).toFixed(2)}"
          stroke-linecap="butt"
        ><title>${classLabel(name)}: ${count} (${(fraction * 100).toFixed(1)}%)</title></circle>`;
      offset += length;
      return arc;
    })
    .join('');

  container.innerHTML = `
    <svg class="donut" viewBox="0 0 ${SIZE} ${SIZE}" role="img"
         aria-label="Detection distribution by class">
      <g transform="rotate(-90 ${SIZE / 2} ${SIZE / 2})">
        <circle cx="${SIZE / 2}" cy="${SIZE / 2}" r="${RADIUS}" fill="none"
                stroke="#232d2a" stroke-width="${STROKE}"></circle>
        ${arcs}
      </g>
      <text x="${SIZE / 2}" y="${SIZE / 2 - 3}" text-anchor="middle"
            fill="#e8efec" font-size="17" font-weight="650">${total}</text>
      <text x="${SIZE / 2}" y="${SIZE / 2 + 11}" text-anchor="middle"
            fill="#6b7975" font-size="7.5">DETECTIONS</text>
    </svg>`;

  if (legendEl) {
    legendEl.innerHTML = entries
      .map(
        ([name, count]) => `
        <span><i style="background:${CLASS_COLOURS[name] || '#9aa8a4'}"></i>
        ${classLabel(name)} <span class="muted">${count}</span></span>`,
      )
      .join('');
  }
}

function emptyRing() {
  return `
    <svg class="donut" viewBox="0 0 ${SIZE} ${SIZE}" role="img" aria-label="No detections">
      <circle cx="${SIZE / 2}" cy="${SIZE / 2}" r="${RADIUS}" fill="none"
              stroke="#232d2a" stroke-width="${STROKE}"></circle>
      <text x="${SIZE / 2}" y="${SIZE / 2 - 3}" text-anchor="middle"
            fill="#6b7975" font-size="17" font-weight="650">0</text>
      <text x="${SIZE / 2}" y="${SIZE / 2 + 11}" text-anchor="middle"
            fill="#6b7975" font-size="7.5">DETECTIONS</text>
    </svg>`;
}
