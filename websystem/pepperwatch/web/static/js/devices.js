// The Hardware list: what this session is running on, and whether each part
// is healthy.
//
// Built from three feeds: the session record (board, AI HAT+, every camera
// found), live telemetry (throttling, the power source), and the worker's
// liveness. Each device gets a status dot and, when something needs
// attention, a pill saying what. Names come from the hardware itself, so they
// are only ever set as text.

const ICON_WARN = 'i-triangle-alert';

export class DeviceList {
  constructor(list) {
    this.list = list;
    this.session = null;
    this.telemetry = null;
    this.worker = null;
    this.signature = '';
  }

  setSession(meta) {
    if (meta && meta.session_id) this.session = meta;
    this.render();
  }

  setTelemetry(telemetry) {
    this.telemetry = telemetry;
    this.render();
  }

  setWorker(state) {
    this.worker = state;
    this.render();
  }

  render() {
    const devices = this.devices();
    // Telemetry arrives every second; rebuild only when something shown
    // has changed, so the list does not flicker.
    const signature = JSON.stringify(devices);
    if (signature === this.signature) return;
    this.signature = signature;
    this.list.replaceChildren(...devices.map(deviceItem));
  }

  devices() {
    const meta = this.session;
    const telemetry = this.telemetry || {};
    const alive = this.worker ? this.worker.alive === true : null;

    if (!meta) {
      return [{
        status: alive === false ? 'off' : 'idle',
        name: alive === false ? 'Detector offline' : 'Waiting for the detector',
        meta: 'Listed when a session starts',
      }];
    }

    const accel = meta.accelerator || {};
    const hailo = accel.hailo || {};
    const host = meta.host || {};
    const camera = meta.camera || {};
    const out = [];

    // The board. Throttling is the thing worth flagging here.
    const board = {
      status: alive === false ? 'off' : telemetry.throttled ? 'warn' : 'ok',
      name: shortBoard(host.board) || 'Raspberry Pi 5',
      meta: [host.ram_mb ? `${Math.round(host.ram_mb / 1024)} GB RAM` : null,
        accel.kind === 'cpu' ? 'runs inference' : 'host'].filter(Boolean).join(' · '),
    };
    if (alive === false) board.pill = { kind: 'off', text: 'Detector offline' };
    else if (telemetry.throttled) {
      board.pill = { kind: 'warn', text: `Throttled: ${telemetry.throttle_detail || 'check cooling'}` };
    }
    out.push(board);

    // The AI HAT+, if one is on the bus; why it is idle, if it is.
    if (hailo.present) {
      const used = accel.kind === 'hailo';
      out.push({
        status: used ? 'ok' : 'warn',
        name: `AI HAT+ ${hailo.name || ''}`.trim(),
        meta: [hailo.pcie_link ? `PCIe ${hailo.pcie_link}` : null, used ? 'runs inference' : 'idle']
          .filter(Boolean).join(' · '),
        pill: used ? null : { kind: 'warn', text: `Not used: ${accel.reason}` },
      });
    }

    // Every camera found; the one in use is green.
    const found = camera.found && camera.found.length ? camera.found : [camera];
    found.forEach((candidate) => {
      const active = candidate.kind === camera.kind && String(candidate.device) === String(camera.device);
      const where = candidate.kind === 'csi' ? `CSI camera ${candidate.device}` : candidate.device;
      out.push({
        status: active ? 'ok' : 'idle',
        name: candidate.model || 'Camera',
        meta: [where, active ? 'in use' : 'standby'].filter(Boolean).join(' · '),
      });
    });

    // Where the power figure comes from.
    if (telemetry.power_source === 'ina219') {
      out.push({ status: 'ok', name: 'INA219 power monitor', meta: 'Whole-device power · I²C' });
    } else if (telemetry.power_source === 'pmic') {
      out.push({
        status: 'ok',
        name: 'Pi 5 PMIC',
        meta: 'Power from board rails only',
        pill: { kind: 'warn', text: 'Fit an INA219 for whole-device power' },
      });
    }

    return out;
  }
}

function deviceItem(device) {
  const item = document.createElement('li');
  item.className = 'device';

  const dot = document.createElement('span');
  dot.className = `dot is-${device.status}`;
  dot.setAttribute('aria-hidden', 'true');

  const body = document.createElement('div');
  const name = document.createElement('div');
  name.className = 'device-name';
  name.textContent = device.name;
  const meta = document.createElement('div');
  meta.className = 'device-meta';
  meta.textContent = device.meta;
  body.append(name, meta);

  if (device.pill) {
    const pill = document.createElement('div');
    pill.className = `pill is-${device.pill.kind}`;
    pill.append(icon(ICON_WARN), document.createTextNode(device.pill.text));
    body.append(pill);
  }

  // Status as words too, so it does not rest on the dot's colour alone.
  const status = document.createElement('span');
  status.className = 'sr-only';
  status.textContent = { ok: 'working', warn: 'needs attention', off: 'offline', idle: 'not in use' }[device.status];
  body.append(status);

  item.append(dot, body);
  return item;
}

function icon(id) {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('class', 'icon');
  svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS(ns, 'use');
  use.setAttribute('href', `#${id}`);
  svg.append(use);
  return svg;
}

// "Raspberry Pi 5 Model B Rev 1.1" -> "Raspberry Pi 5 Model B".
function shortBoard(model) {
  return model ? model.replace(/\s+Rev\s+[\d.]+$/i, '') : '';
}
