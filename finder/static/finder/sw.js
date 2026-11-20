/* Service worker: lets the dashboard open with no internet connection.
 *
 * - The dashboard page: network first, falling back to the last copy.
 * - Scripts, styles and Leaflet from the CDN: served from cache, refreshed in the background.
 * - Map tiles: only tiles the user has already viewed are kept (no bulk downloading,
 *   per the OpenStreetMap tile usage policy), capped at MAX_TILES.
 * - API calls are never cached here. Areas the user saves for offline use are kept
 *   by map.js in IndexedDB, on the device only, and expire after 7 days.
 */
"use strict";

const VERSION = "v1";
const SHELL_CACHE = "evac-shell-" + VERSION;
const TILE_CACHE = "evac-tiles-" + VERSION;
const MAX_TILES = 1500;
const NAV_TIMEOUT_MS = 6000;
const TILE_HOSTS = ["tile.openstreetmap.org", "server.arcgisonline.com"];
const CDN_HOSTS = ["unpkg.com"];

self.addEventListener("install", function (event) {
  event.waitUntil(caches.open(SHELL_CACHE).then(function (c) {
    return fetch("/dashboard/", { credentials: "same-origin" }).then(function (resp) {
      if (resp.ok && !resp.redirected) return c.put("/dashboard/", resp);   // skip the login page
    }).catch(function () {});
  }).then(function () { return self.skipWaiting(); }));
});

self.addEventListener("activate", function (event) {
  event.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) {
      return k.startsWith("evac-") && k !== SHELL_CACHE && k !== TILE_CACHE;
    }).map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});

// The page sends the scripts and styles it loaded, so they work offline from the first visit.
self.addEventListener("message", function (event) {
  const data = event.data || {};
  if (data.type === "cache-urls" && Array.isArray(data.urls)) {
    event.waitUntil(caches.open(SHELL_CACHE).then(function (c) {
      return Promise.all(data.urls.map(function (url) {
        return fetch(url, { mode: sameOrigin(url) ? "same-origin" : "cors", credentials: "same-origin" })
          .then(function (resp) { if (isCacheable(resp)) return c.put(url, resp); })
          .catch(function () {});
      }));
    }));
  } else if (data.type === "clear") {
    event.waitUntil(caches.keys().then(function (keys) {
      return Promise.all(keys.filter(function (k) { return k.startsWith("evac-"); })
        .map(function (k) { return caches.delete(k); }));
    }));
  }
});

self.addEventListener("fetch", function (event) {
  const req = event.request;
  if (req.method !== "GET") return;                       // API POSTs go straight to the network
  const url = new URL(req.url);

  if (req.mode === "navigate") {
    if (url.pathname === "/dashboard/") event.respondWith(dashboardPage(req));
    return;
  }
  if (TILE_HOSTS.some(function (h) { return url.hostname.endsWith(h); })) {
    event.respondWith(tile(req));
    return;
  }
  if ((sameOrigin(req.url) && url.pathname.startsWith("/static/")) || CDN_HOSTS.indexOf(url.hostname) >= 0) {
    event.respondWith(staleWhileRevalidate(req));
  }
});

function sameOrigin(url) {
  return new URL(url, self.location.origin).origin === self.location.origin;
}

function isCacheable(resp) {
  return resp && (resp.ok || resp.type === "opaque") && !resp.redirected;
}

function timeout(ms) {
  return new Promise(function (_, reject) { setTimeout(function () { reject(new Error("timeout")); }, ms); });
}

async function dashboardPage(req) {
  const cache = await caches.open(SHELL_CACHE);
  try {
    const resp = await Promise.race([fetch(req), timeout(NAV_TIMEOUT_MS)]);
    // A redirect means the user is logged out (to the login page): never cache that.
    if (resp.ok && !resp.redirected) cache.put("/dashboard/", resp.clone());
    return resp;
  } catch (e) {
    const cached = await cache.match("/dashboard/");
    if (cached) return cached;
    return new Response(
      "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>" +
      "<title>Offline</title><body style='font-family:system-ui;padding:24px;max-width:36em'>" +
      "<h1>You're offline</h1><p>The evacuation planner hasn't been opened on this device while online yet, " +
      "so there's nothing saved to show. Connect once, open the dashboard and use " +
      "<strong>Save this area for offline</strong>.</p></body>",
      { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } });
  }
}

async function staleWhileRevalidate(req) {
  const cache = await caches.open(SHELL_CACHE);
  const cached = await cache.match(req, { ignoreVary: true });
  const network = fetch(req).then(function (resp) {
    if (isCacheable(resp)) cache.put(req, resp.clone());
    return resp;
  }).catch(function () { return null; });
  if (cached) return cached;
  const resp = await network;
  return resp || new Response("", { status: 504 });
}

async function tile(req) {
  const cache = await caches.open(TILE_CACHE);
  const cached = await cache.match(req, { ignoreVary: true });
  if (cached) return cached;
  try {
    const resp = await fetch(req);
    if (resp.ok) {
      await cache.put(req, resp.clone());
      trimTiles(cache);
    }
    return resp;
  } catch (e) {
    return new Response("", { status: 504 });
  }
}

let trimming = false;
async function trimTiles(cache) {
  if (trimming) return;
  trimming = true;
  try {
    const keys = await cache.keys();                       // oldest first
    for (let i = 0; i < keys.length - MAX_TILES; i++) await cache.delete(keys[i]);
  } finally {
    trimming = false;
  }
}
