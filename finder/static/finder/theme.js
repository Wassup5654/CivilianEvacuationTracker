/* Theme toggle: dark "ops" (default) or the light "Field Brief" look.
 * The choice is remembered per browser; the map listens for "evac-themechange"
 * to swap its base tiles and route/shelter colours. */
"use strict";

(function () {
  var KEY = "evac-theme";
  var btn = document.getElementById("theme-toggle");

  function current() {
    return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "ops";
  }

  function label() {
    if (!btn) return;
    var dark = current() === "ops";
    btn.textContent = dark ? "☀ Light mode" : "▮ Ops mode";
    btn.setAttribute("aria-pressed", dark ? "true" : "false");
    btn.title = dark ? "Switch to the light theme" : "Switch to the dark ops theme";
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", dark ? "#000000" : "#1d4ed8");
  }

  function set(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    try { localStorage.setItem(KEY, theme); } catch (e) {}
    label();
    document.dispatchEvent(new CustomEvent("evac-themechange", { detail: { theme: theme } }));
  }

  window.evacTheme = current;
  if (btn) btn.addEventListener("click", function () { set(current() === "ops" ? "light" : "ops"); });
  label();
})();
