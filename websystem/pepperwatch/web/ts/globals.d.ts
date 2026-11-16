// What the page hangs on window: theme.js's view and theme switches (a
// classic script, so they cannot be imported), and bus.js's counters.

interface PwCounters {
  sseMessages: number;
  pollTicks: number;
  intervalFires: number;
  errors: number;
  timeouts: number;
  mode: string;
  lastUpdate: number;
}

interface Window {
  __pw: PwCounters;
  pwView?: {
    get(): 'simple' | 'full';
    // Whether the choice was saved for the next visit.
    set(value: string): boolean;
  };
  pwTheme?: {
    get(): string;
    set(value: string): void;
  };
}
