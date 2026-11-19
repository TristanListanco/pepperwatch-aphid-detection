// Capture & Export Controls (Table 3.4): manual snapshot, pause/resume, CSV.
import { postJson } from './bus.js';
import { errorText } from './fmt.js';
import { isSimple, word } from './words.js';
export class Controls {
    constructor(root, { onToast } = {}) {
        this.paused = false;
        // False while the detector is down: a pause or snapshot request would
        // reach the broker and be reported as done, with nothing to act on it.
        this.available = true;
        this.root = root;
        this.onToast = onToast || (() => { });
        this.bind();
    }
    bind() {
        const snapshot = this.root.querySelector('[data-action-snapshot]');
        if (snapshot) {
            // Its labels, one per view; swapped for "Taking…" while it works.
            const labels = [...snapshot.childNodes];
            snapshot.addEventListener('click', async () => {
                const simple = isSimple();
                snapshot.disabled = true;
                snapshot.textContent = simple ? word('live.taking') : 'Capturing…';
                try {
                    await postJson('/api/capture', { hi_res: true });
                    // 202, not 200: the worker takes the still asynchronously, so the
                    // gallery entry appears a moment later rather than immediately.
                    this.onToast(simple ? word('toast.taking') : 'Taking a snapshot…');
                }
                catch (error) {
                    this.onToast(simple ? word('toast.failed', { error: errorText(error) }) : `Snapshot failed: ${errorText(error)}`, 'error');
                }
                finally {
                    snapshot.disabled = !this.available;
                    snapshot.replaceChildren(...labels);
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
                }
                catch (error) {
                    this.onToast(`Could not ${action}: ${errorText(error)}`, 'error');
                }
                finally {
                    pause.disabled = !this.available;
                }
            });
        }
        // Plain navigations, so the browser's own download UI handles them and
        // the response streams rather than buffering in memory.
        const exportBtn = this.root.querySelector('[data-action-export]');
        if (exportBtn) {
            exportBtn.addEventListener('click', () => {
                window.location.href = exportUrl('/api/export.csv');
            });
        }
        const exportZip = this.root.querySelector('[data-action-export-zip]');
        if (exportZip) {
            exportZip.addEventListener('click', () => {
                window.location.href = exportUrl('/api/export.zip');
            });
        }
    }
    setAvailable(available) {
        this.available = available;
        ['[data-action-snapshot]', '[data-action-pause]'].forEach((selector) => {
            const button = this.root.querySelector(selector);
            if (button)
                button.disabled = !available;
        });
    }
    setPaused(paused) {
        this.paused = paused === true;
        const pause = this.root.querySelector('[data-action-pause]');
        if (pause)
            pause.textContent = this.paused ? 'Resume inference' : 'Pause inference';
    }
}
// The scans between the gallery's From and To dates, when set: what is saved
// matches what the filters show.
function exportUrl(base) {
    const query = new URLSearchParams();
    for (const name of ['from', 'to']) {
        const value = document.querySelector(`[data-filters] [name="${name}"]`)?.value;
        if (value)
            query.set(name, value);
    }
    const text = query.toString();
    return text ? `${base}?${text}` : base;
}
// The page has one toast; a new message replaces the one showing.
let toastTimer;
export function toast(message, kind = 'info') {
    const host = document.querySelector('[data-toast]');
    if (!host)
        return;
    host.textContent = message;
    host.className = kind === 'error' ? 'banner is-offline' : 'banner';
    host.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
        host.classList.add('is-leaving'); // app.css: a short fade out
        toastTimer = setTimeout(() => {
            host.hidden = true;
        }, 160);
    }, 6000);
}
