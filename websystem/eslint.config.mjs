// JavaScript lint for CI: `npx eslint@10.14.0 pepperwatch/web/static/js`.
//
// The dashboard has no package.json or node_modules, so this imports no plugin
// or shared config. The rules are the ones that catch real bugs, above all
// no-undef: a variable used after its declaration was deleted only failed in a
// browser, and `node --check` (scripts/check_js.sh) cannot see that.

const browser = Object.fromEntries(
  [
    'window', 'document', 'navigator', 'location', 'localStorage', 'console',
    'fetch', 'EventSource', 'AbortController', 'FormData', 'URL', 'URLSearchParams',
    'CustomEvent', 'ResizeObserver', 'matchMedia', 'devicePixelRatio',
    'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'requestAnimationFrame',
  ].map((name) => [name, 'readonly']),
);

export default [
  {
    files: ['pepperwatch/web/static/js/**/*.js'],
    languageOptions: { ecmaVersion: 2022, sourceType: 'module', globals: browser },
    rules: {
      'no-undef': 'error',
      'no-unused-vars': ['error', { args: 'none', caughtErrors: 'none' }],
      'no-unreachable': 'error',
      'no-dupe-keys': 'error',
      'no-const-assign': 'error',
      'no-redeclare': 'error',
      'no-self-assign': 'error',
      'use-isnan': 'error',
      'valid-typeof': 'error',
    },
  },
  {
    // Loaded as a classic script in <head>, before any module.
    files: ['pepperwatch/web/static/js/theme.js'],
    languageOptions: { sourceType: 'script' },
  },
];
