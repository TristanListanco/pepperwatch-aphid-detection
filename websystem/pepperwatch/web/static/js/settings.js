// The Settings page: the simple or full view, display preferences, detection
// thresholds, how often photos are saved, locale, and clearing stored detections.
//
// The confidence slider matters more than it looks. Figure 3.3 specifies a
// fixed "Confidence Filter (80)", but 0.80 on small aphid clusters would
// discard most true detections, and §4 counts False Negatives -- infestations
// missed entirely -- as a headline metric. Exposing the threshold lets the
// value be chosen from a measured precision/recall sweep instead of asserted,
// which is a stronger result to defend.
import { fetchJson, patchJson, poll, postJson } from './bus.js';
import { bytes, dateTime, errorText, num } from './fmt.js';
import { displayPrefs, setDisplayPref } from './liveview.js';
// The admin PIN unlocks thresholds, photo saving and clearing data, for this
// tab: kept in sessionStorage so a reload stays unlocked, and in memory where
// storage is blocked.
const PIN_KEY = 'pw-admin-pin';
let pin = '';
// Set by bindLock: locks the cards again when the Pi refuses a kept PIN.
let pinRefused = () => { };
// The HAT filters boxes on its chip: an IoU change waits for the next session.
let iouOnChip = false;
function adminHeaders() {
    return pin ? { 'X-Admin-Pin': pin } : {};
}
function keepPin(value) {
    pin = value;
    try {
        if (value)
            window.sessionStorage.setItem(PIN_KEY, value);
        else
            window.sessionStorage.removeItem(PIN_KEY);
    }
    catch (error) {
        // Blocked storage: kept in memory until the page closes.
    }
}
// Asks the Pi; changes nothing. Returns null when the PIN is right, or what to say.
async function checkPin(candidate) {
    try {
        const response = await fetch('/api/admin/unlock', { method: 'POST', headers: { 'X-Admin-Pin': candidate } });
        if (response.ok)
            return null;
        if (response.status === 429)
            return 'Too many wrong tries. Wait a minute, then try again.';
        return candidate ? "That PIN isn't right. Check it and try again." : 'Type the PIN first.';
    }
    catch (error) {
        return `Could not reach the Pi: ${errorText(error)}`;
    }
}
function bindLock(root, required) {
    const fields = [...root.querySelectorAll('[data-pin-fields]')];
    const chips = [...root.querySelectorAll('[data-lock-chip]')];
    const bar = root.querySelector('[data-pin-bar]');
    const dialog = root.querySelector('[data-pin-dialog]');
    const form = root.querySelector('[data-pin-form]');
    const input = root.querySelector('[data-pin-input]');
    const message = root.querySelector('[data-pin-error]');
    const submit = root.querySelector('[data-pin-submit]');
    const show = (locked, arriving = false) => {
        fields.forEach((set) => {
            set.disabled = locked;
            set.classList.remove('is-unlocking');
            if (arriving && !locked) {
                void set.offsetWidth; // restart the settle animation
                set.classList.add('is-unlocking');
            }
        });
        chips.forEach((chip) => {
            chip.hidden = !locked;
        });
        if (!bar)
            return;
        bar.classList.toggle('is-unlocked', !locked);
        bar.querySelector('[data-lock-icon]')?.setAttribute('href', locked ? '#i-lock' : '#i-lock-open');
        const title = bar.querySelector('[data-lock-title]');
        const note = bar.querySelector('[data-lock-note]');
        if (title)
            title.textContent = locked ? 'Locked: processor, detection, photos, data and debug log' : 'Unlocked in this tab';
        if (note) {
            note.textContent = locked
                ? 'Anyone can see these settings. Changing them needs the admin PIN.'
                : 'You can change the settings below. Closing the tab, or Lock again, locks them.';
        }
        bar.querySelector('[data-unlock]')?.toggleAttribute('hidden', !locked);
        bar.querySelector('[data-relock]')?.toggleAttribute('hidden', locked);
    };
    if (!required) {
        // No PIN set up on this Pi (a development checkout): nothing to unlock.
        if (bar)
            bar.hidden = true;
        show(false);
        return;
    }
    if (bar)
        bar.hidden = false;
    let kept = '';
    try {
        kept = window.sessionStorage.getItem(PIN_KEY) || '';
    }
    catch (error) {
        // Not kept: asked for again.
    }
    pin = kept;
    show(!kept);
    // A PIN kept from earlier in this tab may have been changed on the Pi since.
    if (kept) {
        checkPin(kept).then((problem) => {
            if (problem) {
                keepPin('');
                show(true);
            }
        });
    }
    pinRefused = () => {
        keepPin('');
        show(true);
    };
    const say = (text) => {
        if (message)
            message.textContent = text;
        input?.setAttribute('aria-invalid', text ? 'true' : 'false');
    };
    root.querySelectorAll('[data-unlock]').forEach((button) => {
        button.addEventListener('click', () => {
            if (!dialog || !input)
                return;
            input.value = '';
            say('');
            dialog.showModal();
            input.focus();
        });
    });
    root.querySelector('[data-pin-cancel]')?.addEventListener('click', () => dialog?.close());
    root.querySelector('[data-relock]')?.addEventListener('click', () => {
        keepPin('');
        show(true);
    });
    form?.addEventListener('submit', async (event) => {
        event.preventDefault();
        if (!input || !dialog)
            return;
        const candidate = input.value.trim();
        if (submit)
            submit.disabled = true;
        const problem = await checkPin(candidate);
        if (submit)
            submit.disabled = false;
        if (problem) {
            say(problem);
            input.classList.remove('is-wrong');
            void input.offsetWidth;
            input.classList.add('is-wrong');
            input.value = ''; // typed again, rather than edited blind
            input.focus();
            return;
        }
        keepPin(candidate);
        dialog.close();
        show(false, true);
    });
}
export async function initSettings(root) {
    bindDisplayPrefs(root);
    bindClear(root);
    bindDebugLog(root);
    const config = await fetchJson('/api/config');
    if (!config)
        return;
    bindLock(root, config.pin_required === true);
    showModel(root, config.model);
    iouOnChip = config.model?.detail?.nms_on_chip === true;
    const chipNote = root.querySelector('[data-iou-chip-note]');
    if (chipNote)
        chipNote.hidden = !iouOnChip;
    const showConf = bindSlider(root, 'conf', config.inference.conf_threshold);
    const showIou = bindSlider(root, 'iou', config.inference.iou_threshold);
    bindReset(root, config.inference.defaults, () => {
        showConf(config.inference.defaults.conf_threshold);
        showIou(config.inference.defaults.iou_threshold);
    });
    bindIntervals(root, config.persist);
    bindAccelerator(root, config.accelerator);
    bindSite(root, config.site.name);
    const localeSelect = root.querySelector('[data-setting-locale]');
    if (localeSelect) {
        localeSelect.innerHTML = (config.locales || ['en'])
            .map((code) => `<option value="${code}">${code === 'ceb' ? 'Cebuano' : 'English'}</option>`)
            .join('');
        localeSelect.value = localStorage.getItem('pw-locale') || 'en';
        localeSelect.addEventListener('change', () => {
            localStorage.setItem('pw-locale', localeSelect.value);
            window.location.reload();
        });
    }
}
// The model doing the detecting, as the worker last reported it. Read only.
function showModel(root, model) {
    const list = root.querySelector('[data-model-facts]');
    const note = root.querySelector('[data-model-note]');
    if (!list || !note || !model?.kind)
        return;
    if (model.synthetic) {
        note.textContent = 'No trained model is installed, so a heuristic stand-in is running: its boxes are '
            + 'made up, and every photo it saves is marked as not from a model. Copy the model to '
            + 'models/pepperwatch.onnx on the Pi and restart Pepper Watch.';
        return;
    }
    const detail = model.detail || {};
    const file = detail.model_path || detail.hef_path;
    let exported = detail.exported ? dateTime(detail.exported) : null;
    if (exported && detail.ultralytics_version)
        exported += `, with Ultralytics ${detail.ultralytics_version}`;
    const rows = [
        ['YOLO version', detail.architecture || (model.kind === 'onnx' ? 'Not recognised from the model file' : null)],
        ['Classes it was trained on', (detail.trained_classes || model.class_names)?.join(', ')],
        ['Input size', model.input_size ? `${model.input_size} × ${model.input_size} pixels` : null],
        ['Weights', detail.weights_m ? `About ${num(detail.weights_m, 1)} million, ${detail.precision}` : null],
        ['Exported', exported],
        ['File', file ? `${file.split('/').pop()}, ${num(detail.size_mb, 1)} MB` : null],
        ['Box filtering (NMS)', detail.nms_on_chip == null ? null : detail.nms_on_chip ? 'On the AI HAT+' : 'On the CPU'],
        ['Runs on', model.description],
    ];
    list.replaceChildren(...rows.filter(([, value]) => value).map(([label, value]) => {
        const row = document.createElement('div');
        const term = document.createElement('dt');
        const text = document.createElement('dd');
        term.textContent = label;
        text.textContent = value || '';
        row.append(term, text);
        return row;
    }));
    note.hidden = true;
    list.hidden = false;
}
// The farm's name: on every page and on each new photo. No PIN; anyone may.
function bindSite(root, initial) {
    const form = root.querySelector('[data-site-form]');
    const input = root.querySelector('[data-setting-site]');
    const status = root.querySelector('[data-site-status]');
    if (!form || !input || !status)
        return;
    input.value = initial;
    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const button = form.querySelector('button');
        if (button)
            button.disabled = true;
        status.className = 'note';
        try {
            const { name } = await patchJson('/api/site', { name: input.value });
            input.value = name;
            document.querySelectorAll('[data-site-name]').forEach((heading) => {
                heading.textContent = name;
            });
            document.title = `${document.title.split(' · ')[0]} · ${name}`;
            status.textContent = 'Saved. Other phones show the new name when they next open the page.';
        }
        catch (error) {
            status.textContent = `Not saved: ${errorText(error)}`;
            status.className = 'note sev-high';
        }
        finally {
            if (button)
                button.disabled = false;
        }
    });
}
// Saved in this browser and read by the dashboard when it next opens.
function bindDisplayPrefs(root) {
    const themeSelect = root.querySelector('[data-setting-theme]');
    const theme = window.pwTheme;
    if (themeSelect && theme) {
        themeSelect.value = theme.get();
        themeSelect.addEventListener('change', () => theme.set(themeSelect.value));
    }
    // Simple or full view: applied at once, so the cards below show or hide.
    const view = root.querySelector('[data-setting-view]');
    const pwView = window.pwView;
    if (view && pwView) {
        view.querySelectorAll('input[name="view"]').forEach((input) => {
            input.checked = input.value === pwView.get();
            input.addEventListener('change', () => {
                const note = root.querySelector('[data-view-note]');
                if (note)
                    note.hidden = pwView.set(input.value);
            });
        });
    }
    const prefs = displayPrefs();
    root.querySelectorAll('[data-pref]').forEach((input) => {
        const key = input.dataset.pref ?? '';
        input.checked = prefs[key] === true;
        input.addEventListener('change', () => setDisplayPref(key, input.checked));
    });
}
// Two steps, inline: a pop-up confirm is easy to dismiss by reflex on a phone.
function bindClear(root) {
    const start = root.querySelector('[data-clear-start]');
    const confirm = root.querySelector('[data-clear-confirm]');
    const status = root.querySelector('[data-clear-status]');
    const yes = root.querySelector('[data-clear-yes]');
    if (!start || !confirm || !status || !yes)
        return;
    start.addEventListener('click', () => {
        confirm.hidden = false;
        start.hidden = true;
    });
    root.querySelector('[data-clear-no]')?.addEventListener('click', () => {
        confirm.hidden = true;
        start.hidden = false;
    });
    yes.addEventListener('click', async () => {
        yes.disabled = true;
        try {
            const result = await postJson('/api/data/clear', { confirm: 'DELETE' }, adminHeaders());
            status.textContent = `Deleted ${result.deleted_events} detections and ${result.deleted_files} image files.`;
        }
        catch (error) {
            if (/PIN/.test(errorText(error)))
                pinRefused();
            status.textContent = `Could not clear: ${errorText(error)}`;
        }
        finally {
            yes.disabled = false;
            confirm.hidden = true;
            start.hidden = false;
        }
    });
}
// Returns how to show a value the detector has taken, set from elsewhere.
function bindSlider(root, key, initial) {
    const slider = root.querySelector(`[data-slider-${key}]`);
    const readout = root.querySelector(`[data-slider-${key}-value]`);
    const status = root.querySelector('[data-settings-status]');
    if (!slider)
        return () => { };
    let applied = initial;
    const show = (value) => {
        applied = value;
        slider.value = String(value);
        if (readout)
            readout.textContent = num(value, 2);
    };
    show(initial);
    slider.addEventListener('input', () => {
        if (readout)
            readout.textContent = num(Number(slider.value), 2);
    });
    // Commit on release rather than on every input event: dragging the slider
    // would otherwise fire a PATCH per pixel, and each one republishes to the
    // worker over MQTT.
    slider.addEventListener('change', async () => {
        const field = key === 'conf' ? 'conf_threshold' : 'iou_threshold';
        if (await applySetting({ [field]: Number(slider.value) }, status)) {
            applied = Number(slider.value);
        }
        else {
            show(applied); // what the detector is really using, not the value that failed
        }
    });
    return show;
}
// Both thresholds back to the values in the Pi's config files, in one change.
function bindReset(root, defaults, onApplied) {
    const button = root.querySelector('[data-thresholds-reset]');
    const note = root.querySelector('[data-thresholds-defaults]');
    if (!button)
        return;
    if (note) {
        note.textContent = `Confidence ${num(defaults.conf_threshold, 2)} and NMS IoU ${num(defaults.iou_threshold, 2)}, as set in the Pi's config files.`;
    }
    button.addEventListener('click', async () => {
        button.disabled = true;
        if (await applySetting(defaults, root.querySelector('[data-settings-status]')))
            onApplied();
        button.disabled = false;
    });
}
function bindIntervals(root, initial) {
    const status = root.querySelector('[data-interval-status]');
    root.querySelectorAll('[data-setting-interval]').forEach((select) => {
        const field = select.dataset.settingInterval;
        let applied = initial[field];
        // A value set in the config files may not be one of the choices; show it
        // anyway, so the page never claims an interval the Pi is not using.
        if (![...select.options].some((option) => Number(option.value) === applied)) {
            select.add(new Option(`${num(applied, 0)} seconds`, String(applied)));
        }
        select.value = String(applied);
        select.addEventListener('change', async () => {
            if (await applySetting({ [field]: Number(select.value) }, status))
                applied = Number(select.value);
            else
                select.value = String(applied);
        });
    });
}
// Which processor runs the detector. It is picked once per session, so a change
// starts a new one; the page then says what that session really runs on, since
// a HAT that will not start still falls back to the CPU.
function bindAccelerator(root, initial) {
    const select = root.querySelector('[data-setting-accelerator]');
    const now = root.querySelector('[data-accelerator-now]');
    const status = root.querySelector('[data-accelerator-status]');
    if (!select || !initial)
        return;
    let applied = initial.mode;
    let session = initial.session_id;
    const show = (state) => {
        session = state.session_id;
        const hat = select.querySelector('option[value="hailo"]');
        if (hat) {
            hat.disabled = !state.hat && state.mode !== 'hailo';
            hat.textContent = state.hat ? `AI HAT+ (${state.hat})` : 'AI HAT+ (not detected)';
        }
        select.value = state.mode;
        if (now) {
            now.textContent = !state.running ? 'The detector has not reported yet.'
                : state.running === 'CPU' && state.reason ? `Running on the CPU: ${state.reason}.`
                    : `Running on the ${state.running}.`;
        }
    };
    show(initial);
    const awaitSession = async () => {
        for (let tries = 0; tries < 20; tries += 1) {
            await new Promise((resolve) => setTimeout(resolve, 2000));
            const state = (await fetchJson('/api/config').catch(() => null))?.accelerator;
            if (state?.session_id && state.session_id !== session) {
                show(state);
                if (status)
                    status.textContent = `A new session started on the ${state.running}.`;
                return;
            }
        }
        if (status)
            status.textContent = 'Saved, but no new session has started yet. Check the dashboard.';
    };
    select.addEventListener('change', async () => {
        const mode = select.value;
        if (status) {
            status.textContent = 'Saving…';
            status.className = 'note';
        }
        try {
            const result = await postJson('/api/accelerator', { mode }, adminHeaders());
            applied = mode;
            if (!result.restarting) {
                if (status)
                    status.textContent = 'Saved. It applies when the detector next starts.';
                return;
            }
            if (status)
                status.textContent = 'Saved. A new session is starting…';
            await awaitSession();
        }
        catch (error) {
            if (/PIN/.test(errorText(error)))
                pinRefused();
            select.value = applied;
            if (status) {
                status.textContent = `Not saved: ${errorText(error)}`;
                status.className = 'note sev-high';
            }
        }
    });
}
// Start, stop and download a debug log. Downloads stay open while the card is
// locked, like the session CSV files; starting and stopping need the PIN.
function bindDebugLog(root) {
    const start = root.querySelector('[data-debug-start]');
    const stop = root.querySelector('[data-debug-stop]');
    const status = root.querySelector('[data-debug-status]');
    const list = root.querySelector('[data-debug-logs]');
    if (!start || !stop || !list)
        return;
    const show = (state) => {
        start.hidden = Boolean(state.active);
        stop.hidden = !state.active;
        const running = state.logs.find((entry) => entry.active);
        if (status) {
            status.className = 'note';
            status.textContent = running
                ? `Recording since ${dateTime(running.started_at)}: ${bytes(running.bytes)} so far.`
                : state.logs.length ? '' : 'No debug logs yet.';
        }
        list.replaceChildren(...state.logs.map((entry) => {
            const link = document.createElement('a');
            link.className = 'btn is-quiet';
            link.href = `/api/debug-log/${encodeURIComponent(entry.id)}.zip`;
            link.download = '';
            link.innerHTML = '<svg class="icon" aria-hidden="true"><use href="#i-download"/></svg>';
            link.append(`Debug log, ${dateTime(entry.started_at)}`);
            const note = document.createElement('span');
            note.className = 'note';
            note.textContent = `${bytes(entry.bytes)}${entry.active ? ', still recording' : ''}`;
            const item = document.createElement('li');
            item.append(link, note);
            return item;
        }));
    };
    const control = async (action, button) => {
        button.disabled = true;
        try {
            show(await postJson('/api/debug-log', { action }, adminHeaders()));
        }
        catch (error) {
            if (/PIN/.test(errorText(error)))
                pinRefused();
            if (status) {
                status.textContent = `Could not ${action} it: ${errorText(error)}`;
                status.className = 'note sev-high';
            }
        }
        finally {
            button.disabled = false;
        }
    };
    start.addEventListener('click', () => control('start', start));
    stop.addEventListener('click', () => control('stop', stop));
    // Its size grows while it records.
    let first = true;
    poll(async () => {
        if (!first && stop.hidden)
            return;
        first = false;
        const state = await fetchJson('/api/debug-log');
        if (state)
            show(state);
    }, 10000);
}
// Sends one change and says whether the worker took it.
async function applySetting(changes, status) {
    if (status) {
        status.textContent = 'Applying…';
        status.className = 'note';
    }
    try {
        // Answers only once the worker reports the new value, a second or two.
        await patchJson('/api/config', changes, adminHeaders());
        if (status) {
            status.textContent = iouOnChip && 'iou_threshold' in changes
                ? `${'conf_threshold' in changes ? 'Confidence applied now. ' : 'Saved. '}`
                    + 'On the AI HAT+ a new IoU is used from the next session.'
                : 'Applied: the detector is using it now, and keeps it after restarts.';
        }
        return true;
    }
    catch (error) {
        if (/PIN/.test(errorText(error)))
            pinRefused();
        if (status) {
            status.textContent = `Not applied: ${errorText(error)}`;
            status.className = 'note sev-high';
        }
        return false;
    }
}
