/* Registers the offline service worker and clears offline data on log out. */
"use strict";

(function () {
  var OFFLINE_DB = "evac-offline";

  if ("serviceWorker" in navigator && window.isSecureContext) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/sw.js", { scope: "/" }).then(function () {
        return navigator.serviceWorker.ready;
      }).then(function (reg) {
        if (!reg.active || location.pathname !== "/dashboard/") return;
        // Cache this page's own scripts and styles so it opens offline from the first visit.
        var urls = [];
        document.querySelectorAll("script[src], link[rel=stylesheet][href]").forEach(function (el) {
          urls.push(el.src || el.href);
        });
        reg.active.postMessage({ type: "cache-urls", urls: urls });
      }).catch(function (err) {
        console.warn("Offline support unavailable:", err);
      });
    });
  }

  // Log out: delete the cached dashboard, viewed map tiles and saved areas from this device.
  document.querySelectorAll('form[action$="/logout/"]').forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (form.dataset.cleared) return;
      event.preventDefault();
      var jobs = [];
      if (window.caches) {
        jobs.push(caches.keys().then(function (keys) {
          return Promise.all(keys.filter(function (k) { return k.indexOf("evac-") === 0; })
            .map(function (k) { return caches.delete(k); }));
        }));
      }
      if (window.indexedDB) {
        jobs.push(new Promise(function (resolve) {
          var r = indexedDB.deleteDatabase(OFFLINE_DB);
          r.onsuccess = r.onerror = r.onblocked = function () { resolve(); };
        }));
      }
      var done = function () { form.dataset.cleared = "1"; form.submit(); };
      Promise.all(jobs).then(done, done);
      setTimeout(done, 2000);   // never block logging out
    });
  });
})();
