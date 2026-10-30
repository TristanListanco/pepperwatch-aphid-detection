// Capture & Export Controls (Table 3.4): manual snapshot, pause/resume, CSV.

import { postJson } from './bus.js';

export class Controls {
  constructor(root, { onToast } = {}) {
    this.root = root;
    this.onToast = onToast || (() => {});
    this.paused = false;
    this.bind();
  }

  bind() {
    const snapshot = this.root.querySelector('[data-action-snapshot]');
    if (snapshot) {
      snapshot.addEventListener('click', async () => {
        snapshot.disabled = true;
        snapshot.textContent = 'Capturing…';
        try {
          await postJson('/api/capture', { hi_res: true });
          // 202, not 200: the worker takes the still asynchronously, so the
          // gallery entry appears a moment later rather than immediately.
          this.onToast('Taking a snapshot…');
        } catch (error) {
          this.onToast(`Snapshot failed: ${error.message}`, 'error');
        } finally {
          snapshot.disabled = false;
          snapshot.textContent = 'Snapshot';
        }
      });
    }

    const pause = this.root.querySelector('[data-action-pause]');
    if (pause) {
      pause.addEventListener('click', async () => {
        const action = this.paused ? 'resume' : 'pause';
        pause.disabled = true;
        try {
          await postJson(`/api/inference/${action}`);
          this.paused = !this.paused;
          pause.textContent = this.paused ? 'Resume inference' : 'Pause inference';
          this.onToast(this.paused ? 'Inference paused' : 'Inference resumed');
        } catch (error) {
          this.onToast(`Could not ${action}: ${error.message}`, 'error');
        } finally {
          pause.disabled = false;
        }
      });
    }

    const exportBtn = this.root.querySelector('[data-action-export]');
    if (exportBtn) {
      exportBtn.addEventListener('click', () => {
        // A plain navigation, so the browser's own download UI handles it and
        // the response streams rather than buffering in memory.
        window.location.href = '/api/export.csv';
      });
    }
  }

  setPaused(paused) {
    this.paused = paused === true;
    const pause = this.root.querySelector('[data-action-pause]');
    if (pause) pause.textContent = this.paused ? 'Resume inference' : 'Pause inference';
  }
}

export function toast(message, kind = 'info') {
  const host = document.querySelector('[data-toast]');
  if (!host) return;
  host.textContent = message;
  host.className = kind === 'error' ? 'banner is-offline' : 'banner';
  host.hidden = false;
  clearTimeout(host._timer);
  host._timer = setTimeout(() => {
    host.hidden = true;
  }, 6000);
}
