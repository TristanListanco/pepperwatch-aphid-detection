// Light or dark, per device. A classic script in <head>, not a module, so the
// theme is set before the first paint instead of flashing light first.
//
// "system" follows the phone's own setting and keeps following it. Light is
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

  // For the Settings page.
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
