// The Live Inference View (Table 3.4).
//
// Two streaming modes, and the default is the cheap one. A 640x480 JPEG at
// quality 75 is roughly 40-55 KB, so MJPEG at 17 fps costs about 6-7 Mbit/s per
// viewer -- against maybe 10-20 Mbit/s total from the Pi's radio on 2.4 GHz in
// an open field. Three phones would saturate it.
//
// So the page opens in "poll" mode, fetching /frame.jpg about once a second
// (~0.4 Mbit/s), and switches to MJPEG only when the user taps Start Camera.
// Figure 3.4 draws exactly this: an inactive feed, a Start Camera button, and a
// Live toggle.
//
// Boxes are drawn on a canvas over the image rather than baked into the frame
// by the worker. That keeps the Pi's CPU on inference, gives crisp labels at
// any device pixel ratio, and lets the overlay be toggled off. Archived
// snapshots are annotated server-side instead -- see render.py.

import { classColour, classLabel } from './fmt.js';

const POLL_MS = 1000;

// Display preferences for this device, set on the Settings page. Live video
// is on by default; turning it off drops to one frame a second, which spares
// the access point's bandwidth when several phones are watching.
const DISPLAY_DEFAULTS = { live: true, mirror: false };

export function displayPrefs() {
  const prefs = {};
  for (const [key, fallback] of Object.entries(DISPLAY_DEFAULTS)) {
    let stored = null;
    try {
      stored = localStorage.getItem(`pw-${key}`);
    } catch (error) {
      // Private mode or blocked storage: fall back to the defaults.
    }
    prefs[key] = stored === null ? fallback : stored === '1';
  }
  return prefs;
}

export function setDisplayPref(key, on) {
  try {
    localStorage.setItem(`pw-${key}`, on ? '1' : '0');
  } catch (error) {
    // Not persisted; applies until the page reloads.
  }
}

export class LiveView {
  constructor(root) {
    this.root = root;
    this.image = root.querySelector('[data-live-image]');
    this.canvas = root.querySelector('[data-live-canvas]');
    this.viewport = root.querySelector('[data-viewport]');
    this.ctx = this.canvas.getContext('2d');

    this.mode = 'idle';
    this.pollTimer = null;
    this.detections = [];
    this.frameSeq = null;
    this.showOverlay = true;
    this.mirror = false;

    // Redraw on resize: the canvas backing store is sized in device pixels, so
    // a rotated phone or a resized window needs new dimensions or the overlay
    // stretches away from the image beneath it.
    this.observer = new ResizeObserver(() => this.resize());
    this.observer.observe(this.viewport);

    this.image.addEventListener('load', () => {
      this.viewport.classList.add('is-active');
      // Take the camera's own shape, so the picture fills the frame with no
      // bars at the sides.
      const { naturalWidth: w, naturalHeight: h } = this.image;
      if (w && h) this.viewport.style.setProperty('--aspect', `${w} / ${h}`);
      this.resize();
    });
    this.image.addEventListener('error', () => {
      if (this.mode === 'mjpeg') this.setMode('poll');
    });
  }

  // -- modes -------------------------------------------------------------

  setMode(mode) {
    if (this.mode === mode) return;
    this.stop();
    this.mode = mode;

    if (mode === 'poll') {
      this.startPolling();
    } else if (mode === 'mjpeg') {
      // Cache-bust so a reconnect is not served the previous stream.
      this.image.src = `/stream.mjpg?t=${Date.now()}`;
    } else {
      this.image.removeAttribute('src');
      this.viewport.classList.remove('is-active');
      this.clearOverlay();
    }

    this.root.dispatchEvent(new CustomEvent('pw:live-mode', { detail: mode, bubbles: true }));
  }

  startPolling() {
    const tick = () => {
      // The URL has to change or the browser serves its cache; the server also
      // answers 304 on an unchanged frame, so this stays cheap either way.
      this.image.src = `/frame.jpg?t=${Date.now()}`;
    };
    tick();
    this.pollTimer = setInterval(tick, POLL_MS);
  }

  stop() {
    if (this.pollTimer) {
      clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
    if (this.mode === 'mjpeg') {
      // Dropping the src is what actually closes the connection and frees the
      // server's viewer slot; without it the slot leaks until the tab closes.
      this.image.removeAttribute('src');
    }
  }

  destroy() {
    this.stop();
    this.observer.disconnect();
  }

  toggleOverlay(show) {
    this.showOverlay = show;
    this.draw();
  }

  // Flips the picture; boxes are drawn at mirrored positions instead of being
  // flipped with it, so their labels stay readable.
  setMirror(on) {
    this.mirror = on;
    this.viewport.classList.toggle('is-mirrored', on);
    this.draw();
  }

  // -- drawing -----------------------------------------------------------

  resize() {
    const rect = this.viewport.getBoundingClientRect();
    const ratio = window.devicePixelRatio || 1;
    this.canvas.width = Math.max(1, Math.round(rect.width * ratio));
    this.canvas.height = Math.max(1, Math.round(rect.height * ratio));
    this.draw();
  }

  update(payload) {
    this.detections = payload.boxes || [];
    this.frameSeq = payload.frame_seq;
    this.draw();
  }

  clearOverlay() {
    this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
  }

  // The image uses object-fit: contain, so it is letterboxed inside the
  // viewport whenever their aspect ratios differ. Boxes are normalised to the
  // *image*, so the drawn rect has to be mapped into the contained box --
  // skipping this is the classic overlay-misalignment bug.
  contentBox() {
    const width = this.canvas.width;
    const height = this.canvas.height;
    const natural = this.image.naturalWidth / this.image.naturalHeight;

    if (!Number.isFinite(natural) || natural <= 0) {
      return { x: 0, y: 0, width, height };
    }

    const viewport = width / height;
    if (viewport > natural) {
      const drawn = height * natural;
      return { x: (width - drawn) / 2, y: 0, width: drawn, height };
    }
    const drawn = width / natural;
    return { x: 0, y: (height - drawn) / 2, width, height: drawn };
  }

  draw() {
    this.clearOverlay();
    if (!this.showOverlay || !this.detections.length) return;
    if (!this.image.naturalWidth) return;

    const box = this.contentBox();
    const ratio = window.devicePixelRatio || 1;
    const lineWidth = Math.max(1.5, 2 * ratio);

    // Label size follows the card's CSS width, not just device pixel ratio.
    // A fixed 11px reads fine across a 650px desktop card but swamps a 320px
    // phone, where the same text covers a third of the frame.
    const cssWidth = this.canvas.width / ratio;
    const fontSize = Math.max(9, Math.min(11, cssWidth / 38)) * ratio;
    // Below this width the class name is dropped: on a phone the colour
    // already carries the class, and the confidence is the part being read.
    const compact = cssWidth < 420;

    this.ctx.lineWidth = lineWidth;
    this.ctx.font = `600 ${fontSize}px ui-monospace, Menlo, monospace`;
    this.ctx.textBaseline = 'top';

    this.detections.forEach((detection) => {
      const colour = classColour(detection.class_name);
      const left = this.mirror ? 1 - detection.x - detection.w : detection.x;
      const x = box.x + left * box.width;
      const y = box.y + detection.y * box.height;
      const w = detection.w * box.width;
      const h = detection.h * box.height;

      this.ctx.strokeStyle = colour;
      this.ctx.strokeRect(x, y, w, h);

      const percent = Math.round(detection.confidence * 100);
      const text = compact ? `${percent}%` : `${classLabel(detection.class_name)} ${percent}%`;
      const metrics = this.ctx.measureText(text);
      const padding = 3 * ratio;
      const labelW = metrics.width + padding * 2;
      const labelH = fontSize + padding * 2;

      // Keep the label on-frame: above the box normally, inside it at the top
      // edge, shifted left at the right edge. Same rules as render.py.
      let labelY = y - labelH;
      if (labelY < box.y) labelY = y + padding;
      let labelX = x;
      if (labelX + labelW > box.x + box.width) {
        labelX = Math.max(box.x, box.x + box.width - labelW);
      }

      this.ctx.fillStyle = colour;
      this.ctx.fillRect(labelX, labelY, labelW, labelH);
      this.ctx.fillStyle = '#0b0f0e';
      this.ctx.fillText(text, labelX + padding, labelY + padding);
    });
  }
}
