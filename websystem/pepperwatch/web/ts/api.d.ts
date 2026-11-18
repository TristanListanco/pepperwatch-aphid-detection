// What the Pi's API and event stream send, as far as the dashboard reads it.
// Declarations only: nothing here is compiled, and nothing checks the JSON at
// run time, so every field the server may leave out is optional.

export type Severity = 'none' | 'low' | 'moderate' | 'high';

// telemetry/system, /api/telemetry
export interface Telemetry {
  ts?: number;
  cpu_temp_c?: number | null;
  cpu_percent?: number | null;
  hailo_temp_c?: number | null;
  power_w?: number | null;
  best_power_w?: number | null;
  power_source?: string | null;
  thermal_level?: number;
  thermal_multiplier?: number;
  throttled?: boolean;
  throttle_detail?: string | null;
}

// telemetry/pipeline, /api/pipeline
export interface Pipeline {
  paused?: boolean;
  inference_fps?: number | null;
  inference_ms?: number | null;
  preview_fps?: number | null;
  camera_fps?: Array<number | null>;
  target_fps?: number | null;
  dropped?: number;
  camera_on?: boolean[];
  camera_connected?: boolean[];
  camera_stalled?: boolean[];
  storage?: 'ok' | 'low' | 'full';
  disk_free_mb?: number | null;
}

// One box, normalised 0-1 to the image.
export interface Box {
  class_name: string;
  confidence: number;
  x: number;
  y: number;
  w: number;
  h: number;
}

// detect/frame, /api/detections/latest
export interface DetectionFrame {
  ts?: number;
  camera?: number;
  frame_seq?: number;
  boxes?: Box[];
}

export interface LatestDetections extends DetectionFrame {
  cameras?: DetectionFrame[];
}

// detect/flag
export interface Flag {
  camera?: number;
  severity?: Severity;
  count?: number;
}

export interface Advice {
  urgency?: string;
  headline?: string;
  body?: string;
  actions?: string[];
  validated?: boolean;
}

// event/persisted, /api/events, /api/events/<id>
export interface ScanEvent {
  id: number;
  captured_at: string;
  severity: Severity;
  detection_count: number;
  camera?: number;
  trigger?: string;
  thumb_path?: string | null;
  image_path?: string | null;
  synthetic?: boolean;
  max_confidence?: number | null;
  damaged_area?: number | null;
  model_version?: string;
  conf_threshold?: number;
  inference_ms?: number | null;
  advice?: Advice | null;
  capture_id?: string | null;
  camera_name?: string | null;
  // Every camera's result, when the photo shows more than one.
  cameras?: CaptureCamera[];
}

// One camera's result in a photo of several (ScanEvent.cameras).
export interface CaptureCamera {
  id: number;
  camera: number;
  camera_name?: string | null;
  severity: Severity;
  detection_count: number;
  max_confidence?: number | null;
}

export interface EventsPage {
  total: number;
  items?: ScanEvent[];
}

// status/worker as the bus passes it on, and /api/health's worker.
export interface WorkerState {
  alive?: boolean;
  status?: string;
}

export interface CameraMeta {
  kind?: string;
  device?: string | number;
  model?: string;
  found?: CameraMeta[];
}

export interface Accelerator {
  kind?: string;
  name?: string;
  reason?: string;
  hailo?: { present?: boolean; name?: string; pcie_link?: string };
}

// status/session, /api/session's session
export interface SessionMeta {
  session_id?: string;
  label?: string;
  started_at?: string;
  cameras?: CameraMeta[];
  camera?: CameraMeta;
  accelerator?: Accelerator;
  detector?: { model_file?: string; input_size?: number; synthetic?: boolean };
  host?: { board?: string; cpu?: string; ram_mb?: number; board_tier?: string };
}

// session/summary
export interface Summary {
  energy_wh?: number | null;
  fps_per_watt?: number | null;
  total_ms_p95?: number | null;
}

export interface SessionResponse {
  session?: SessionMeta | null;
  summary?: Summary | null;
  camera_on?: boolean[];
  camera_connected?: boolean[];
}

// /api/sessions
export interface SessionRow {
  session_id: string;
  current?: boolean;
  label?: string;
  started_at: string;
  accelerator?: string;
  duration_s?: number | null;
  inference_fps_mean?: number | null;
  total_ms_p95?: number | null;
  power_mean_w?: number | null;
  power_source?: string;
  energy_wh?: number | null;
  files?: string[];
}

// The SSE hello: what a page needs to paint at once.
export interface Hello {
  session?: SessionMeta | null;
  latest?: Record<string, unknown>;
  worker?: WorkerState;
}

export interface Health {
  worker?: WorkerState;
  server_time?: string;
}

// /api/stats/weekly
export interface WeeklyStats {
  severity?: Severity;
  avg_confidence?: number | null;
  trend?: 'rising' | 'falling' | 'stable';
  events?: number;
  events_previous?: number;
  delta_pct?: number | null;
  distribution?: { healthy?: number; aphid_damage?: number };
  advice?: Advice | null;
  detector_synthetic?: boolean;
  // What recent photos typically show: the simple view's status.
  // photos counts only those with leaves; no_leaves, those without, when none had any.
  status?: { severity?: Severity; basis?: '24h' | '7d'; photos?: number; no_leaves?: number; advice?: Advice | null };
}

// /api/config
export interface Config {
  inference: {
    conf_threshold: number;
    iou_threshold: number;
    // What the Pi's config files say, for "Reset to defaults".
    defaults: { conf_threshold: number; iou_threshold: number };
  };
  // Seconds between saved photos, while damage is in view and while none is.
  persist: { heartbeat_detected_s: number; heartbeat_clean_s: number };
  site: { name: string };
  locales?: string[];
}

export interface ClearResult {
  deleted_events: number;
  deleted_files: number;
}
