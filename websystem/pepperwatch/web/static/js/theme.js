// Light or dark, and the simple or full view, per device. A classic script in
// <head>, not a module, so both are set before the first paint instead of
// flashing the wrong one first.
//
// Theme "system" follows the phone's own setting and keeps following it. Light is
// still the better choice in direct sun; dark is for dusk and indoors.

(function () {
  var media = window.matchMedia('(prefers-color-scheme: dark)');
  var pref = 'system';
  try {
    pref = localStorage.getItem('pw-theme') || 'system';
  } catch (error) {
    // Private mode or blocked storage: follow the system.
  }

  function apply() {
    var dark = pref === 'dark' || (pref === 'system' && media.matches);
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    var bar = document.querySelector('meta[name="theme-color"]');
    if (bar) bar.content = dark ? '#121413' : '#e6e7e4';
  }

  apply();
  media.addEventListener('change', apply);

  // Simple (for farmers, the default) or full (for agricultural experts), per
  // device. Set here so the page never shows the other view's parts first.
  var view = 'simple';
  try {
    view = localStorage.getItem('pw-view') === 'full' ? 'full' : 'simple';
  } catch (error) {
    // Private mode or blocked storage: the simple view.
  }
  document.documentElement.dataset.view = view;

  // For the Settings page.
  window.pwView = {
    get: function () {
      return view;
    },
    set: function (value) {
      view = value === 'full' ? 'full' : 'simple';
      try {
        localStorage.setItem('pw-view', view);
      } catch (error) {
        // Not persisted; applies until the page reloads.
      }
      document.documentElement.dataset.view = view;
    },
  };

  window.pwTheme = {
    get: function () {
      return pref;
    },
    set: function (value) {
      pref = value;
      try {
        localStorage.setItem('pw-theme', value);
      } catch (error) {
        // Not persisted; applies until the page reloads.
      }
      apply();
    },
  };
})();
