/* ── Evacuation Map — Leaflet interactive map + side panel ── */
/* global L */
"use strict";

(function () {
  // ── Constants ──
  const ROUTE_COLORS = ["#16A34A", "#7C3AED", "#EA580C"];
  const ROUTE_LABELS = ["Route 1 (best)", "Route 2", "Route 3"];
  const ROUTE_DASHES = ["", "16 8", "8 6 2 6"];
  const ROUTE_WEIGHTS = [6, 5, 5];
  const DAMAGE_STROKE = "#DC2626";
  const GRADE_COLORS = { destroyed: "#7F1D1D", damaged: "#DC2626", possibly_damaged: "#F59E0B" };
  const ESTIMATE_COLORS = { war_start: "#7C3AED", recent: "#DB2777" };
  const DEAD_END_COLOR = "#6B7280";
  const SHELTER_COLOR = "#22C55E";
  const ZONE_COLOR = "#2563EB";
  const DEFAULT_ZOOM = 16;
  const DEFAULT_RECT_M = 1500;   // 1.5 km default side
  const MAX_RECT_M = 5000;       // 5 km max side
  const DEBOUNCE_MS = 600;

  // ── State ──
  let map, tileOSM, tileSat;
  let startMarker, zoneRect;
  let currentMode = "walk";
  let searchToken = 0;
  let fetchToken = 0;
  let debounceTimer = null;
  let lastStreetViewQuery = null;

  // Layer groups
  // deadEndLayer holds two sub-groups so the parallel context/routes fetches
  // never clear each other's features.
  let damageLayer, deadEndLayer, strandedLayer, routeDeadEndLayer, shelterLayer, routeLayers, suggestLayer, hazardLayer;
  let suggestToken = 0;

  // Latest results for the current zone, kept so the area can be saved for offline use
  let lastCtx = null, lastRoutes = null, lastSuggest = null, lastHazards = null;
  let lastQuery = null;            // {bbox, start, mode} the results above belong to
  let currentPlaceLabel = "";
  let viewingSaved = null;         // the saved area on screen while offline, if any

  // ── DOM refs ──
  const form        = document.getElementById("lookup");
  const input       = document.getElementById("address");
  const statusEl    = document.getElementById("status");
  const matchesList = document.getElementById("matches");
  const evacBody    = document.getElementById("evac-body");
  const mapDiv      = document.getElementById("map");
  const routeCards  = document.getElementById("route-cards");
  const imgDate     = document.getElementById("imagery-date");
  const geoBtn      = document.getElementById("geolocate-btn");

  // ── Helpers ──
  function setStatus(text, isError) {
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.className = isError ? "status error" : "status";
  }

  function showToast(msg, duration) {
    duration = duration || 4000;
    const existing = document.querySelector(".map-toast");
    if (existing) existing.remove();
    const el = document.createElement("div");
    el.className = "map-toast";
    el.textContent = msg;
    if (mapDiv && mapDiv.parentElement) {
      mapDiv.parentElement.appendChild(el);
      setTimeout(function () { el.remove(); }, duration);
    }
  }

  function metersToLatDeg(m) { return m / 111320; }
  function metersToLonDeg(m, lat) {
    return m / (111320 * Math.cos(lat * Math.PI / 180));
  }

  function formatTime(seconds) {
    if (seconds < 60) return seconds + " sec";
    var mins = Math.round(seconds / 60);
    if (mins < 60) return mins + " min";
    var h = Math.floor(mins / 60);
    var rm = mins % 60;
    return h + " h " + rm + " min";
  }

  function formatDist(meters) {
    if (meters < 1000) return Math.round(meters) + " m";
    return (meters / 1000).toFixed(1) + " km";
  }

  // ── Initialize map ──
  function initMap(lat, lon) {
    if (map) {
      map.flyTo([lat, lon], DEFAULT_ZOOM);
      return;
    }

    map = L.map("map", { zoomControl: true }).setView([lat, lon], DEFAULT_ZOOM);

    // Tile layers
    // crossOrigin lets the service worker keep viewed tiles for offline use
    tileOSM = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      crossOrigin: true,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
    });

    tileSat = L.tileLayer(
      "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
      maxZoom: 19,
      crossOrigin: true,
      attribution: 'Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics'
    });

    tileOSM.addTo(map);
    L.control.layers({ "OpenStreetMap": tileOSM, "Satellite": tileSat }).addTo(map);

    // Layer groups
    damageLayer  = L.layerGroup().addTo(map);
    deadEndLayer = L.layerGroup().addTo(map);
    strandedLayer = L.layerGroup().addTo(deadEndLayer);
    routeDeadEndLayer = L.layerGroup().addTo(deadEndLayer);
    shelterLayer = L.layerGroup().addTo(map);
    routeLayers  = L.layerGroup().addTo(map);
    suggestLayer = L.layerGroup().addTo(map);
    hazardLayer  = L.layerGroup().addTo(map);

    // Wire up layer toggles
    wireToggle("toggle-hazards", hazardLayer);
    wireToggle("toggle-damage", damageLayer);
    wireToggle("toggle-deadends", deadEndLayer);
    wireToggle("toggle-shelters", shelterLayer);
    wireToggle("toggle-routes", routeLayers);

    // Right-click anywhere on map to inspect street view
    map.on("contextmenu", function (e) {
      openStreetView(e.latlng.lat, e.latlng.lng, "Pinned Location");
    });

    // Wire up street view drawer controls
    var closeSvBtn = document.getElementById("streetview-close-btn");
    if (closeSvBtn) {
      closeSvBtn.onclick = function () {
        var drawer = document.getElementById("streetview-drawer");
        if (drawer) drawer.hidden = true;
        var iframe = document.getElementById("streetview-iframe");
        if (iframe) iframe.src = "about:blank";
      };
    }
  }

  function wireToggle(id, layer) {
    var cb = document.getElementById(id);
    if (!cb) return;
    cb.addEventListener("change", function () {
      if (cb.checked) map.addLayer(layer);
      else map.removeLayer(layer);
    });
  }

  // ── Start marker ──
  function placeStartMarker(lat, lon) {
    if (startMarker) {
      startMarker.setLatLng([lat, lon]);
    } else {
      startMarker = L.marker([lat, lon], {
        draggable: true,
        title: "Start location",
        icon: L.divIcon({
          className: "",
          html: '<div style="width:22px;height:22px;background:#9a6324;border:3px solid #fff;border-radius:50%;box-shadow:0 2px 6px rgba(0,0,0,0.5);cursor:grab"></div>',
          iconSize: [22, 22],
          iconAnchor: [11, 11]
        })
      }).addTo(map);

      startMarker.on("dragend", function () {
        clearAccuracyCircle();   // the user has placed themselves; the GPS estimate no longer applies
        var pos = startMarker.getLatLng();
        centerZone(pos.lat, pos.lng);
        debouncedFetch();
      });
    }
  }

  // ── Blue evacuation zone rectangle ──
  function placeZone(lat, lon) {
    var halfLatDeg = metersToLatDeg(DEFAULT_RECT_M / 2);
    var halfLonDeg = metersToLonDeg(DEFAULT_RECT_M / 2, lat);
    var bounds = L.latLngBounds(
      [lat - halfLatDeg, lon - halfLonDeg],
      [lat + halfLatDeg, lon + halfLonDeg]
    );

    if (zoneRect) {
      zoneRect.setBounds(bounds);
    } else {
      zoneRect = L.rectangle(bounds, {
        color: ZONE_COLOR,
        weight: 3,
        fillColor: ZONE_COLOR,
        fillOpacity: 0.15,
        dashArray: "6 4",
        interactive: true
      }).addTo(map);

      // Make it editable & draggable with Geoman
      if (zoneRect && zoneRect.pm && typeof zoneRect.pm.enable === "function") {
        try {
          zoneRect.pm.enable({
            allowSelfIntersection: false,
            draggable: true
          });
        } catch (e) {
          console.warn("Geoman enable failed:", e);
        }
      }

      zoneRect.on("pm:edit pm:dragend pm:markerdragend", function () {
        enforceBounds();
        debouncedFetch();
      });
    }
  }

  function centerZone(lat, lon) {
    if (!zoneRect) {
      placeZone(lat, lon);
      return;
    }
    var b = zoneRect.getBounds();
    var latSpan = b.getNorth() - b.getSouth();
    var lonSpan = b.getEast() - b.getWest();
    zoneRect.setBounds(L.latLngBounds(
      [lat - latSpan / 2, lon - lonSpan / 2],
      [lat + latSpan / 2, lon + lonSpan / 2]
    ));
  }

  function enforceBounds() {
    if (!zoneRect) return;
    var b = zoneRect.getBounds();
    var center = b.getCenter();
    var latSpanM = (b.getNorth() - b.getSouth()) * 111320;
    var lonSpanM = (b.getEast() - b.getWest()) * 111320 * Math.cos(center.lat * Math.PI / 180);

    if (latSpanM > MAX_RECT_M || lonSpanM > MAX_RECT_M) {
      var clampLat = Math.min(latSpanM, MAX_RECT_M);
      var clampLon = Math.min(lonSpanM, MAX_RECT_M);
      var halfLat = metersToLatDeg(clampLat / 2);
      var halfLon = metersToLonDeg(clampLon / 2, center.lat);
      zoneRect.setBounds(L.latLngBounds(
        [center.lat - halfLat, center.lng - halfLon],
        [center.lat + halfLat, center.lng + halfLon]
      ));
      showToast("Maximum zone size is 5 km per side.", 3000);
    }
  }

  function getZoneBbox() {
    if (!zoneRect) return null;
    var b = zoneRect.getBounds();
    return [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()];
  }

  function setZoneBbox(bbox) {
    var bounds = L.latLngBounds([bbox[1], bbox[0]], [bbox[3], bbox[2]]);
    if (!zoneRect) placeZone((bbox[1] + bbox[3]) / 2, (bbox[0] + bbox[2]) / 2);
    zoneRect.setBounds(bounds);
  }

  // ── Debounced API fetch ──
  function debouncedFetch() {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(fetchContextAndRoutes, DEBOUNCE_MS);
  }

  // ── Fetch context + routes in parallel with immediate rendering ──
  async function fetchContextAndRoutes() {
    var bbox = getZoneBbox();
    if (!bbox || !startMarker) return;

    if (!navigator.onLine) { handleOffline(); return; }

    var token = ++fetchToken;
    routeCards.innerHTML = '<div class="panel-loading"><span class="spinner"></span> Computing best evacuation routes…</div>';

    var startPos = startMarker.getLatLng();
    var query = { bbox: bbox, start: { lat: startPos.lat, lon: startPos.lng }, mode: currentMode };
    if (!sameQuery(lastQuery, query)) {
      lastCtx = lastRoutes = lastSuggest = null;
    }
    lastQuery = query;

    // 1. Fetch routes (primary result)
    fetch("/api/routes", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": getCSRF()
      },
      body: JSON.stringify({
        start: { lat: startPos.lat, lon: startPos.lng },
        bbox: bbox,
        mode: currentMode
      })
    })
      .then(async function (resp) {
        if (token !== fetchToken) return;
        if (!resp.ok) {
          var errJson = await resp.json().catch(function () { return {}; });
          throw new Error(errJson.error || "Routes API returned " + resp.status);
        }
        return resp.json();
      })
      .then(function (routeData) {
        if (token !== fetchToken || !routeData) return;
        viewingSaved = null;
        lastRoutes = routeData;
        renderRoutes(routeData);
        scheduleRadarRefresh(routeData.radar_pending, token);
      })
      .catch(function (err) {
        if (token !== fetchToken) return;
        if (err instanceof TypeError && !navigator.onLine) { handleOffline(); return; }
        console.error("Route fetch error:", err);
        routeLayers.clearLayers();
        routeDeadEndLayer.clearLayers();
        var msg = err.message || "Area too large or map servers busy. Try a smaller area.";
        showToast(msg, 5000);
        routeCards.innerHTML =
          '<div class="panel-placeholder error-card" style="padding:16px;text-align:center;">' +
            '<p style="color:var(--danger, #DC2626);font-weight:600;margin-bottom:12px;">' +
              escapeHtml(msg) +
            '</p>' +
            '<button type="button" class="btn btn-secondary" id="retry-routes-btn" style="padding:6px 14px;cursor:pointer;border-radius:6px;border:1px solid #d1d5db;background:#fff;font-size:13px;display:inline-flex;align-items:center;gap:6px;">' +
              '↻ Retry' +
            '</button>' +
          '</div>';
        var retryBtn = document.getElementById("retry-routes-btn");
        if (retryBtn) {
          retryBtn.addEventListener("click", function () {
            fetchContextAndRoutes();
          });
        }
      });

    // Suggested shelter sites follow the zone while the box is ticked
    if (suggestBox && suggestBox.checked) fetchSuggestions();

    // 2. Fetch context layers (damage, shelters, dead ends) independently
    postJSON("/api/context", { bbox: bbox, mode: currentMode })
      .then(function (resp) {
        if (!resp.ok) return null;
        return resp.json();
      })
      .then(function (ctx) {
        if (token !== fetchToken || !ctx) return;
        lastCtx = ctx;
        renderContextLayers(ctx);
        scheduleRadarRefresh(ctx.radar_pending, token);
      })
      .catch(function (err) {
        console.warn("Context fetch error:", err);
      });
  }

  // ── Render context layers ──
  function renderContextLayers(ctx) {
    damageLayer.clearLayers();
    strandedLayer.clearLayers();
    shelterLayer.clearLayers();

    // Official damage (Copernicus EMS / UNOSAT), coloured by grade
    if (ctx.damage && ctx.damage.features && ctx.damage.features.length > 0) {
      L.geoJSON(ctx.damage, {
        style: function (feature) {
          var p = feature.properties || {};
          if (p.estimated) {
            var ec = ESTIMATE_COLORS[p.layer] || ESTIMATE_COLORS.war_start;
            return { color: ec, weight: 2, dashArray: "5 4", fillColor: ec,
                     fillOpacity: p.grade === "damaged" ? 0.3 : 0.15 };
          }
          var color = GRADE_COLORS[p.grade] || DAMAGE_STROKE;
          if (p.kind === "road") return { color: color, weight: 6, opacity: 0.85 };
          if (p.kind === "area") return { color: color, weight: 1.5, fillColor: color, fillOpacity: 0.12, dashArray: "4 3" };
          return { color: color, weight: 1, fillColor: color, fillOpacity: 0.6 };
        },
        pointToLayer: function (feature, latlng) {
          var color = GRADE_COLORS[(feature.properties || {}).grade] || DAMAGE_STROKE;
          return L.circleMarker(latlng, { radius: 4, color: color, weight: 1, fillColor: color, fillOpacity: 0.8 });
        },
        onEachFeature: function (feature, layer) {
          var p = feature.properties || {};
          if (p.estimated) {
            layer.bindPopup(
              "<strong>Estimate, low confidence</strong><br>" +
              escapeHtml(p.layer === "recent" ? "New radar change in the last 12 months"
                                              : "Radar change in the war's first 3 months") +
              "<br>" + escapeHtml(p.label || "") +
              "<br><small>" + escapeHtml(p.source || "") + ", " + escapeHtml(p.date || "") +
              ". Rebuilding or construction can also cause change.</small>"
            );
            return;
          }
          var what = p.kind === "road" ? "Road" : (p.kind === "area" ? "Assessed area" : "Building");
          layer.bindPopup(
            "<strong>" + escapeHtml(p.grade_label || "Damage") + " · " + what + "</strong>" +
            (p.label ? "<br>" + escapeHtml(p.label) : "") +
            "<br><small>" + escapeHtml(p.source || "") + (p.date ? ", imagery " + escapeHtml(p.date) : "") + "</small>"
          );
        }
      }).addTo(damageLayer);
    }
    renderDamageSources(ctx.damage_sources, ctx.damage_assessed);
    if (ctx.hazards) renderHazards(ctx.hazards);

    // Stranded edges (roads cut off from every shelter by damage).
    // Dead ends come from /api/routes instead — see renderRouteDeadEnds().
    if (ctx.stranded && ctx.stranded.features && ctx.stranded.features.length > 0) {
      L.geoJSON(ctx.stranded, {
        style: function () {
          return {
            color: DEAD_END_COLOR,
            weight: 3,
            dashArray: "6 4",
            opacity: 0.6
          };
        }
      }).addTo(strandedLayer);
    }

    // Shelters (green markers)
    if (ctx.shelters && ctx.shelters.length > 0) {
      ctx.shelters.forEach(function (s) {
        var marker = L.circleMarker([s.lat, s.lon], {
          radius: 8,
          fillColor: SHELTER_COLOR,
          fillOpacity: 0.9,
          color: "#fff",
          weight: 2
        });

        var displayName = s.name || "Shelter";
        var rawType = s.type || "Shelter";
        var subType = rawType.replace(/_/g, " ");

        // Exact name tooltip displayed on hover
        marker.bindTooltip(
          '<div class="shelter-tooltip-content">' +
            '<strong>' + escapeHtml(displayName) + '</strong>' +
            (s.type && s.type.toLowerCase() !== "shelter"
              ? '<span class="shelter-tooltip-sub">' + escapeHtml(subType) + '</span>'
              : '') +
          '</div>',
          { direction: "top", offset: [0, -8], className: "shelter-hover-tooltip" }
        );

        // Detailed popup on click
        marker.bindPopup(
          '<div class="shelter-popup">' +
            '<strong>' + escapeHtml(displayName) + '</strong>' +
            '<span class="shelter-type">' + escapeHtml(subType) +
            (s.verified ? " ✓ Verified" : "") + '</span>' +
            (s.street ? '<div class="shelter-street">📍 ' + escapeHtml(s.street) + '</div>' : '') +
            '<div class="shelter-popup-actions">' +
              '<button type="button" class="btn-sv-trigger btn-sv-popup" data-lat="' + s.lat + '" data-lon="' + s.lon + '" data-name="' + escapeHtml(displayName) + '">📷 Street View</button>' +
            '</div>' +
          '</div>'
        );

        marker.addTo(shelterLayer);
      });
    }
  }

  // ── Relief planning: suggested shelter sites (never routing destinations) ──
  var suggestBox = document.getElementById("toggle-suggest");
  var suggestCount = document.getElementById("suggest-count");
  var suggestResults = document.getElementById("suggest-results");

  function clearSuggestions() {
    suggestToken++;
    if (suggestLayer) suggestLayer.clearLayers();
    if (suggestResults) suggestResults.innerHTML = "";
  }

  function fetchSuggestions() {
    var bbox = getZoneBbox();
    if (!bbox || !suggestResults) return;
    var token = ++suggestToken;
    suggestLayer.clearLayers();
    suggestResults.innerHTML = '<div class="panel-loading"><span class="spinner"></span> Finding the best places for new shelters…</div>';
    postJSON("/api/suggest-shelters", { bbox: bbox, count: suggestCount ? Number(suggestCount.value) : 3 })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
      .then(function (res) {
        if (token !== suggestToken) return;
        if (!res.ok) {
          suggestResults.textContent = res.data.error || "Could not compute suggestions.";
          return;
        }
        lastSuggest = res.data;
        renderSuggestions(res.data);
      })
      .catch(function () {
        if (token !== suggestToken) return;
        suggestResults.textContent = navigator.onLine
          ? "Could not reach the suggestion service."
          : "You're offline. Suggested sites can only be shown for areas saved while they were on screen.";
      });
  }

  function renderSuggestions(data) {
    suggestLayer.clearLayers();
    var sites = data.sites || [];
    var cov = data.coverage || {};
    var html = "";
    if (cov.with_suggestions_pct !== undefined) {
      html += '<div class="suggest-summary">' +
        (cov.existing_shelters
          ? "Existing shelters reach <strong>" + cov.existing_shelters_pct + "%</strong> of this zone's built-up area within a 10-minute walk. "
          : "No existing shelters in this zone. ") +
        (sites.length
          ? "With " + sites.length + " suggested site" + (sites.length > 1 ? "s" : "") +
            ": <strong>" + cov.with_suggestions_pct + "%</strong>."
          : "") +
        "</div>";
    }
    sites.forEach(function (s) {
      if (s.catchment) {
        L.geoJSON(s.catchment, {
          style: { color: "#0D9488", weight: 1.5, dashArray: "4 4", fillColor: "#0D9488", fillOpacity: 0.07 },
          interactive: false
        }).addTo(suggestLayer);
      }
      var marker = L.marker([s.lat, s.lon], {
        icon: L.divIcon({ className: "suggest-marker", html: String(s.rank), iconSize: [26, 26], iconAnchor: [13, 13] }),
        title: s.name
      });
      var detail =
        "Adds <strong>" + s.adds_coverage_pct + "%</strong> of the zone's built-up area within a 10-minute walk" +
        (s.damaged_area_share_pct > 0 ? " (" + s.damaged_area_share_pct + "% of it in damaged areas)" : "") + ".<br>" +
        (s.nearest_damage_m !== null ? "Nearest known damage: " + s.nearest_damage_m + " m." : "No known damage nearby.");
      marker.bindPopup(
        '<div class="shelter-popup"><strong>' + escapeHtml(s.name) + "</strong>" +
        '<span class="shelter-type">Suggestion, not an established shelter</span>' +
        "<div>" + detail + "</div>" +
        '<div class="shelter-popup-actions"><button type="button" class="btn-sv-trigger btn-sv-popup" data-lat="' + s.lat +
        '" data-lon="' + s.lon + '" data-name="' + escapeHtml(s.name) + '">📷 Street View</button></div></div>'
      );
      marker.addTo(suggestLayer);
      html += '<div class="suggest-site" data-lat="' + s.lat + '" data-lon="' + s.lon + '"><strong>' + s.rank + ". " +
        escapeHtml(s.street || "Unnamed road") + "</strong> · +" + s.adds_coverage_pct + "% coverage" +
        (s.damaged_area_share_pct > 0 ? ", " + s.damaged_area_share_pct + "% damaged-area demand" : "") + "</div>";
    });
    (data.notes || []).forEach(function (n) { html += '<div class="suggest-basis">' + escapeHtml(n) + "</div>"; });
    if (data.basis) html += '<div class="suggest-basis">' + escapeHtml(data.basis) + "</div>";
    suggestResults.innerHTML = html;
    suggestResults.querySelectorAll(".suggest-site").forEach(function (el) {
      el.addEventListener("click", function () {
        map.setView([parseFloat(el.getAttribute("data-lat")), parseFloat(el.getAttribute("data-lon"))], 17);
      });
    });
  }

  if (suggestBox) {
    suggestBox.addEventListener("change", function () {
      if (suggestBox.checked) fetchSuggestions(); else clearSuggestions();
    });
  }
  if (suggestCount) {
    suggestCount.addEventListener("change", function () {
      if (suggestBox && suggestBox.checked) fetchSuggestions();
    });
  }

  // ── Radar estimates computing in the background: refresh until they're ready ──
  var radarTimer = null, radarRetries = 0;
  function scheduleRadarRefresh(pending, token) {
    if (!pending) { radarRetries = 0; return; }
    if (radarTimer || radarRetries >= 8) return;
    radarRetries += 1;
    showToast("Computing radar damage estimate for this area… the map will update shortly.", 5000);
    radarTimer = setTimeout(function () {
      radarTimer = null;
      if (token === fetchToken) fetchContextAndRoutes();
    }, 25000);
  }

  // ── Footer: which official damage assessments cover this area ──
  function renderDamageSources(sources, assessed) {
    if (!imgDate) return;
    imgDate.textContent = "";
    if (!sources || sources.length === 0) {
      imgDate.textContent = assessed === false || !sources
        ? "Damage data: no official damage assessment (Copernicus EMS or imported UNOSAT) covers this area."
        : "";
      return;
    }
    imgDate.appendChild(document.createTextNode("Damage data: "));
    sources.forEach(function (s, i) {
      if (i > 0) imgDate.appendChild(document.createTextNode(" · "));
      var label = s.name + (s.date ? " (imagery " + s.date + ")" : "");
      if (s.url) {
        var a = document.createElement("a");
        a.href = s.url; a.target = "_blank"; a.rel = "noopener noreferrer";
        a.textContent = label;
        imgDate.appendChild(a);
      } else {
        imgDate.appendChild(document.createTextNode(label));
      }
      if (s.attribution) imgDate.appendChild(document.createTextNode(" — " + s.attribution));
    });
  }

  // ── Dead ends branching off the chosen routes ("don't turn here") ──
  // The server computes these after routing and guarantees none overlaps a route.
  function renderRouteDeadEnds(deadEnds) {
    routeDeadEndLayer.clearLayers();
    if (!deadEnds || !deadEnds.features || deadEnds.features.length === 0) return;
    L.geoJSON(deadEnds, {
      style: function () {
        return { color: DEAD_END_COLOR, weight: 3, dashArray: "6 4", opacity: 0.8 };
      },
      pointToLayer: function (feature, latlng) {
        return L.marker(latlng, {
          icon: L.divIcon({
            className: "dead-end-x",
            html: "✕",
            iconSize: [14, 14],
            iconAnchor: [7, 7]
          })
        }).bindTooltip("Dead end — don't turn here", { direction: "top" });
      }
    }).addTo(routeDeadEndLayer);
  }

  // ── Render routes ──
  function renderRoutes(data) {
    routeLayers.clearLayers();
    routeCards.innerHTML = "";
    renderRouteDeadEnds(data.dead_ends);
    if (viewingSaved) {
      routeCards.innerHTML = '<div class="saved-copy-note">📴 Saved copy from ' + escapeHtml(formatSavedAt(viewingSaved.savedAt)) +
        ". These routes won't update until you're back online.</div>";
    }

    var routes = data.routes || [];
    if (routes.length === 0) {
      routeCards.innerHTML += '<p class="panel-placeholder">' +
        escapeHtml(data.warnings && data.warnings.length ? data.warnings[0] : "No valid routes found.") +
        '</p><p class="panel-placeholder">Relief worker? Tick <strong>Show suggested shelters</strong> under ' +
        'Relief planning to see where new shelters would reach the most people.</p>';
      return;
    }

    routes.forEach(function (route, idx) {
      var color = ROUTE_COLORS[idx] || ROUTE_COLORS[2];
      var dash = ROUTE_DASHES[idx] || "";
      var weight = ROUTE_WEIGHTS[idx] || 4;
      var label = ROUTE_LABELS[idx] || "Route " + (idx + 1);

      // Draw polyline
      var polyline = null;
      if (route.geojson) {
        polyline = L.geoJSON(route.geojson, {
          style: function () {
            return { color: color, weight: weight, dashArray: dash, opacity: 0.9 };
          }
        });
      }
      if (polyline) polyline.addTo(routeLayers);

      // Route card
      var card = document.createElement("div");
      card.className = "route-card";
      card.setAttribute("data-rank", idx + 1);
      card.tabIndex = 0;
      card.setAttribute("role", "button");
      card.setAttribute("aria-label", label);

      var shelterName = route.shelter ? (route.shelter.name || "Shelter") : "—";
      var dmgText = route.damage_exposure_m > 0
        ? (route.damage_exposure_m + " m on damaged roads")
        : "No damaged roads";

      card.innerHTML =
        '<div class="route-card-header">' +
          '<span class="route-card-title">' + escapeHtml(shelterName) + '</span>' +
          '<span class="route-card-badge">' + label + '</span>' +
        '</div>' +
        '<div class="route-card-stats">' +
          '<span><strong>' + formatDist(route.distance_m || 0) + '</strong></span>' +
          '<span><strong>' + formatTime(route.eta_seconds || 0) + '</strong> ' +
            (currentMode === "walk" ? "walking" : "driving") + '</span>' +
        '</div>' +
        (route.damage_assessed === false
          ? '<div class="route-card-damage" style="background:#f3f4f6;color:#6b7280;border-left:3px solid #9ca3af">No official damage data for this area</div>'
          : (route.damage_exposure_m > 0
            ? '<div class="route-card-damage">' + dmgText + '</div>'
            : '')) +
        (route.damage_warnings || []).map(function (w) {
          return '<div class="route-card-damage">⚠ ' + escapeHtml(w) + '</div>';
        }).join("") +
        (route.hazard_warnings || []).map(function (w) {
          return '<div class="route-card-damage route-card-hazard">🔥 ' + escapeHtml(w) + '</div>';
        }).join("") +
        '<div class="route-card-reason">' + escapeHtml(route.rank_reason || "") + '</div>' +
        (route.shelter && route.shelter.lat
          ? '<div class="route-card-actions">' +
              '<button type="button" class="btn-sv-trigger btn-sv-card" data-lat="' + route.shelter.lat + '" data-lon="' + route.shelter.lon + '" data-name="' + escapeHtml(shelterName) + '">📷 Shelter Street View</button>' +
            '</div>'
          : '');

      // Hover → highlight route
      card.addEventListener("mouseenter", function () {
        card.classList.add("highlighted");
        if (polyline) polyline.setStyle({ weight: weight + 3, opacity: 1 });
      });
      card.addEventListener("mouseleave", function () {
        card.classList.remove("highlighted");
        if (polyline) polyline.setStyle({ weight: weight, opacity: 0.9 });
      });

      // Click → zoom to route
      card.addEventListener("click", function () {
        if (polyline) map.fitBounds(polyline.getBounds(), { padding: [40, 40] });
      });
      card.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          card.click();
        }
      });

      routeCards.appendChild(card);
    });
  }

  // ── Address search (reuses /api/search) ──
  var searchDebounce = null;
  form.addEventListener("submit", function (event) {
    event.preventDefault();
    clearTimeout(searchDebounce);
    var token = ++searchToken;
    ++geoRequest;               // a pending location request must not overwrite this search
    matchesList.innerHTML = "";
    setStatus("Looking up that address…", false);
    var query = input.value.trim();
    if (query.length < 3) {
      setStatus("Type at least 3 characters.", true);
      return;
    }
    postJSON("/api/search", { q: query })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
      .then(function (res) {
        if (token !== searchToken) return;
        if (!res.ok) {
          setStatus(res.data.error || "Address lookup failed.", true);
          return;
        }
        var matches = res.data.matches || [];
        if (matches.length === 0) {
          setStatus("No place matched. Add the city and country, then try again.", true);
          return;
        }
        setStatus(matches.length === 1
          ? "One place matched. Confirm it."
          : matches.length + " places matched. Pick the right one.", false);

        matches.forEach(function (match) {
          var item = document.createElement("li");
          var name = document.createElement("strong");
          name.textContent = match.display_name;
          var kind = document.createElement("p");
          kind.className = "kind";
          kind.textContent = match.kind || "place";
          var coords = document.createElement("p");
          coords.className = "coords";
          coords.textContent = match.latitude + ", " + match.longitude;
          var btn = document.createElement("button");
          btn.type = "button";
          btn.className = "secondary";
          btn.textContent = "Use this place";
          btn.addEventListener("click", function () { selectPlace(match); });
          item.append(name, kind, coords, btn);
          matchesList.appendChild(item);
        });
      })
      .catch(function () {
        if (token !== searchToken) return;
        if (!navigator.onLine) {
          setStatus("You're offline, so addresses can't be looked up. Open an area you saved for offline below.", true);
          refreshSavedList(true);
          return;
        }
        setStatus("Could not reach the address lookup.", true);
      });
  });

  // ── Select a place ──
  function selectPlace(match) {
    var lat = parseFloat(match.latitude);
    var lon = parseFloat(match.longitude);
    matchesList.innerHTML = "";
    setStatus("📍 " + match.display_name, false);
    currentPlaceLabel = match.display_name;
    viewingSaved = null;
    evacBody.hidden = false;
    clearAccuracyCircle();

    initMap(lat, lon);
    placeStartMarker(lat, lon);
    placeZone(lat, lon);

    // Give browser time to complete layout of #map container, then invalidateSize & fitBounds
    setTimeout(function () {
      if (map) {
        map.invalidateSize(true);
        if (zoneRect) {
          map.fitBounds(zoneRect.getBounds(), { padding: [40, 40], maxZoom: 16 });
        }
      }
    }, 100);

    debouncedFetch();
  }

  // ── Geolocation ("📍 Use my location") ──
  var GEO_ACCURACY_WARN_M = 100;   // worse than this: ask the user to place the pin themselves
  var GEO_GUARD_MS = 20000;        // never leave the button stuck if the permission prompt is ignored
  var geoRequest = 0;              // bumped by each new location request or address search
  var accuracyCircle = null;

  function setGeoBusy(busy) {
    geoBtn.disabled = busy;
    geoBtn.setAttribute("aria-busy", busy ? "true" : "false");
  }

  function clearAccuracyCircle() {
    if (accuracyCircle && map) map.removeLayer(accuracyCircle);
    accuracyCircle = null;
  }

  function geoErrorMessage(err) {
    if (!window.isSecureContext) {
      return "Your browser only shares location on secure pages (https:// or localhost). " +
             "Open the app over HTTPS, or search for your address instead.";
    }
    switch (err && err.code) {
      case 1: return "Location permission was denied. Allow location for this site in your browser settings, or search for your address instead.";
      case 2: return "Your device couldn't determine where you are (no GPS or network fix). Try again near a window, or search for your address instead.";
      case 3: return "Finding your location took too long. Try again, or search for your address instead.";
      default: return "Couldn't get your location. Search for your address instead.";
    }
  }

  function showAccuracy(lat, lon, accuracyM) {
    clearAccuracyCircle();
    if (!map || !accuracyM) return;
    accuracyCircle = L.circle([lat, lon], {
      radius: accuracyM, color: ZONE_COLOR, weight: 1, fillColor: ZONE_COLOR, fillOpacity: 0.08, interactive: false
    }).addTo(map);
    if (accuracyM > GEO_ACCURACY_WARN_M) {
      setStatus("📍 Your location is approximate (within about " + formatDist(accuracyM) +
                "). Drag the brown pin to where you actually are before following a route.", true);
      showToast("Location is approximate (±" + formatDist(accuracyM) + "). Drag the pin to your exact spot.", 7000);
    } else {
      setStatus("📍 Your location (accurate to about " + Math.round(accuracyM) + " m)", false);
    }
  }

  geoBtn.addEventListener("click", function () {
    if (geoBtn.disabled) return;
    if (!navigator.geolocation) {
      setStatus("This browser can't share your location. Search for your address instead.", true);
      return;
    }
    if (!window.isSecureContext) {
      setStatus(geoErrorMessage(null), true);
      return;
    }
    var myRequest = ++geoRequest;
    ++searchToken;              // a pending address search must not overwrite this result
    matchesList.innerHTML = "";
    setGeoBusy(true);
    setStatus("Getting your location… allow location access if your browser asks.", false);

    var settled = false;
    function settle() {
      if (settled) return false;
      settled = true;
      clearTimeout(guard);
      setGeoBusy(false);
      return myRequest === geoRequest;   // false if a newer search or request superseded this one
    }
    var guard = setTimeout(function () {
      if (settle()) setStatus(geoErrorMessage({ code: 3 }), true);
    }, GEO_GUARD_MS);

    navigator.geolocation.getCurrentPosition(
      function (pos) {
        if (!settle()) return;
        var lat = pos.coords.latitude, lon = pos.coords.longitude;
        selectPlace({
          display_name: "Your location",
          latitude: lat.toFixed(7),
          longitude: lon.toFixed(7),
          kind: "geolocation"
        });
        showAccuracy(lat, lon, pos.coords.accuracy);
      },
      function (err) {
        if (settle()) setStatus(geoErrorMessage(err), true);
      },
      { enableHighAccuracy: true, timeout: 15000, maximumAge: 30000 }
    );
  });

  // ── Mode toggle ──
  document.querySelectorAll(".mode-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      document.querySelectorAll(".mode-btn").forEach(function (b) {
        b.classList.remove("active");
        b.setAttribute("aria-pressed", "false");
      });
      btn.classList.add("active");
      btn.setAttribute("aria-pressed", "true");
      currentMode = btn.getAttribute("data-mode");
      debouncedFetch();
    });
  });

  // ── Live hazards: NASA FIRMS fire/heat detections + GDACS disaster alerts ──
  // Warnings only; they never change routes. Refreshed every few minutes while online.
  var hazardsPanel = document.getElementById("live-hazards");
  var HAZARD_POLL_MS = 5 * 60 * 1000;
  var hazardRetryTimer = null, hazardRetries = 0;

  function renderHazards(h) {
    if (!h || h.enabled === false || !hazardLayer) {
      if (hazardsPanel) hazardsPanel.hidden = true;
      return;
    }
    lastHazards = h;
    hazardLayer.clearLayers();
    var fires = (h.fires && h.fires.features) || [];
    fires.forEach(function (f) {
      var p = f.properties || {};
      var c = f.geometry.coordinates;
      L.marker([c[1], c[0]], {
        icon: L.divIcon({ className: "fire-marker" + (p.hours_ago <= 12 ? " fresh" : ""), html: "🔥",
                          iconSize: [22, 22], iconAnchor: [11, 11] }),
        title: "Fire or heat detected " + (p.age_text || "")
      }).bindPopup(
        "<strong>Fire or heat detected</strong><br>" + escapeHtml(p.age_text || "") +
        " · " + escapeHtml(p.satellite || "") + (p.detections > 1 ? " · seen " + p.detections + " times" : "") +
        "<br>Confidence: " + escapeHtml(p.confidence || "") +
        "<br><small>NASA FIRMS satellite hot spot (375 m pixel), unverified. It can be a strike or building fire, " +
        "but also field burning, a wildfire or an industrial flare.</small>"
      ).addTo(hazardLayer);
    });

    if (!hazardsPanel) return;
    var alerts = h.alerts || [];
    var html = '<h3>Live hazards <span class="live-dot" aria-hidden="true"></span></h3>';
    if (h.pending) {
      html += '<p class="live-line"><span class="spinner"></span> Downloading live satellite fire data…</p>';
    } else {
      alerts.forEach(function (a) {
        html += '<div class="live-alert level-' + escapeHtml((a.level || "green").toLowerCase()) + '">' +
          '<strong>' + escapeHtml(a.level) + " " + escapeHtml(a.type_label).toLowerCase() + " alert</strong>: " +
          escapeHtml(a.name) + (a.distance_km ? " (" + a.distance_km + " km away)" : " (this area)") +
          (a.severity ? "<br><small>" + escapeHtml(a.severity) + "</small>" : "") +
          (a.url ? ' <a href="' + escapeHtml(a.url) + '" target="_blank" rel="noopener noreferrer">GDACS report</a>' : "") +
          "</div>";
      });
      html += '<p class="live-line">' + (fires.length
        ? "🔥 <strong>" + fires.length + "</strong> fire or heat detection" + (fires.length > 1 ? "s" : "") +
          " in or near this zone in the last 48 h, newest " + escapeHtml(fires[0].properties.age_text) + "."
        : "No fire or heat detected by satellite in this zone in the last 48 h.") + "</p>";
      if (!alerts.length) html += '<p class="live-line">No GDACS disaster alerts (earthquake, flood, cyclone, volcano, drought) near this area.</p>';
    }
    (h.notes || []).forEach(function (n) { html += '<p class="live-line muted">' + escapeHtml(n) + "</p>"; });
    html += '<p class="live-line muted">' + (viewingSaved ? "Saved " + escapeHtml(formatSavedAt(viewingSaved.savedAt)) + ". "
      : (h.updated ? "Feeds downloaded " + escapeHtml(timeAgo(h.updated)) + ". " : "")) +
      'Sources: <a href="https://firms.modaps.eosdis.nasa.gov/map/" target="_blank" rel="noopener noreferrer">NASA FIRMS</a>, ' +
      '<a href="https://www.gdacs.org/" target="_blank" rel="noopener noreferrer">GDACS</a>.</p>';
    hazardsPanel.innerHTML = html;
    hazardsPanel.hidden = false;

    clearTimeout(hazardRetryTimer);
    if (h.pending && hazardRetries < 8) {
      hazardRetries += 1;
      hazardRetryTimer = setTimeout(refreshHazards, 15000);
    } else if (!h.pending) {
      hazardRetries = 0;
    }
  }

  function refreshHazards() {
    var bbox = getZoneBbox();
    if (!bbox || viewingSaved || !navigator.onLine || document.hidden) return;
    postJSON("/api/hazards", { bbox: bbox })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (h) {
        var now = getZoneBbox();
        if (h && now && now.join() === bbox.join() && !viewingSaved) renderHazards(h);
      })
      .catch(function () {});
  }
  setInterval(refreshHazards, HAZARD_POLL_MS);

  function timeAgo(iso) {
    var mins = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
    if (mins < 2) return "just now";
    if (mins < 60) return mins + " min ago";
    return Math.round(mins / 60) + " h ago";
  }

  // ── Offline: save areas on this device (IndexedDB), open them with no internet ──
  var OFFLINE_DB = "evac-offline", OFFLINE_STORE = "areas";
  var SAVE_DAYS = 7;
  var saveBtn = document.getElementById("save-offline");
  var savedBox = document.getElementById("saved-areas-box");
  var savedList = document.getElementById("saved-areas");
  var savedCount = document.getElementById("saved-count");
  var clearBtn = document.getElementById("clear-offline");
  var offlineBanner = document.getElementById("offline-banner");

  function openDb() {
    return new Promise(function (resolve, reject) {
      if (!window.indexedDB) { reject(new Error("IndexedDB unavailable")); return; }
      var req = indexedDB.open(OFFLINE_DB, 1);
      req.onupgradeneeded = function () { req.result.createObjectStore(OFFLINE_STORE, { keyPath: "id" }); };
      req.onsuccess = function () { resolve(req.result); };
      req.onerror = function () { reject(req.error); };
    });
  }

  function dbOp(mode, fn) {
    return openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var tx = db.transaction(OFFLINE_STORE, mode);
        var result = fn(tx.objectStore(OFFLINE_STORE));
        tx.oncomplete = function () { db.close(); resolve(result && result.result !== undefined ? result.result : result); };
        tx.onerror = function () { db.close(); reject(tx.error); };
      });
    });
  }

  function listSaved() {
    return dbOp("readonly", function (s) { return s.getAll(); }).then(function (all) {
      var now = Date.now();
      var expired = (all || []).filter(function (a) { return a.expiresAt <= now; });
      if (expired.length) {
        dbOp("readwrite", function (s) { expired.forEach(function (a) { s.delete(a.id); }); }).catch(function () {});
      }
      return (all || []).filter(function (a) { return a.expiresAt > now; })
        .sort(function (a, b) { return b.savedAt - a.savedAt; });
    });
  }

  function formatSavedAt(ms) {
    return new Date(ms).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  }

  function sameQuery(a, b) {
    return !!a && !!b && a.mode === b.mode && a.bbox.join() === b.bbox.join() &&
      Math.abs(a.start.lat - b.start.lat) < 1e-7 && Math.abs(a.start.lon - b.start.lon) < 1e-7;
  }

  function refreshSavedList(open) {
    if (!savedBox) return Promise.resolve([]);
    return listSaved().then(function (areas) {
      savedList.innerHTML = "";
      savedCount.textContent = String(areas.length);
      savedBox.hidden = areas.length === 0;
      if (open && areas.length) savedBox.open = true;
      areas.forEach(function (a) {
        var li = document.createElement("li");
        var name = document.createElement("strong");
        name.textContent = a.label || "Saved area";
        var meta = document.createElement("span");
        meta.className = "saved-meta";
        var nRoutes = (a.routes && a.routes.routes || []).length;
        meta.textContent = (a.mode === "drive" ? "🚗" : "🚶") + " " + nRoutes + " route" + (nRoutes === 1 ? "" : "s") +
          " · saved " + formatSavedAt(a.savedAt) +
          " · deleted after " + new Date(a.expiresAt).toLocaleDateString([], { dateStyle: "medium" });
        var openBtn = document.createElement("button");
        openBtn.type = "button";
        openBtn.textContent = "Open";
        openBtn.addEventListener("click", function () { openSaved(a); });
        var delBtn = document.createElement("button");
        delBtn.type = "button";
        delBtn.className = "secondary";
        delBtn.textContent = "Delete";
        delBtn.addEventListener("click", function () {
          dbOp("readwrite", function (s) { s.delete(a.id); }).then(function () { refreshSavedList(); });
        });
        li.append(name, meta, openBtn, delBtn);
        savedList.appendChild(li);
      });
      return areas;
    }).catch(function () { if (savedBox) savedBox.hidden = true; return []; });
  }

  function saveForOffline() {
    var bbox = getZoneBbox();
    var start = startMarker && startMarker.getLatLng();
    var query = bbox && start ? { bbox: bbox, start: { lat: start.lat, lon: start.lng }, mode: currentMode } : null;
    if (!query || !sameQuery(lastQuery, query) || !lastRoutes || !lastCtx) {
      showToast("Wait for the routes and map layers to finish loading, then save.", 4000);
      return;
    }
    var now = Date.now();
    var record = {
      id: "area-" + now,
      label: currentPlaceLabel || "Saved area",
      savedAt: now,
      expiresAt: now + SAVE_DAYS * 24 * 3600 * 1000,
      bbox: bbox, start: query.start, mode: currentMode,
      ctx: lastCtx, routes: lastRoutes,
      suggest: suggestBox && suggestBox.checked ? lastSuggest : null,
      hazards: lastHazards
    };
    // Show the whole zone so its map tiles are fetched (and kept by the service worker).
    map.fitBounds(zoneRect.getBounds(), { padding: [20, 20] });
    dbOp("readwrite", function (s) { s.put(record); })
      .then(function () {
        showToast("Saved for offline use until " + new Date(record.expiresAt).toLocaleDateString([], { dateStyle: "medium" }) +
                  ". It opens with no internet from 'Saved for offline' at the top.", 6000);
        refreshSavedList();
      })
      .catch(function () { showToast("This browser can't store offline data (private window?).", 5000); });
  }

  function openSaved(a) {
    viewingSaved = a;
    ++fetchToken;                    // ignore any request still in flight
    evacBody.hidden = false;
    currentPlaceLabel = a.label;
    setStatus("📴 " + a.label + " (saved " + formatSavedAt(a.savedAt) + ")", false);
    currentMode = a.mode || "walk";
    document.querySelectorAll(".mode-btn").forEach(function (b) {
      var on = b.getAttribute("data-mode") === currentMode;
      b.classList.toggle("active", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    });
    initMap(a.start.lat, a.start.lon);
    placeStartMarker(a.start.lat, a.start.lon);
    setZoneBbox(a.bbox);
    lastQuery = { bbox: a.bbox, start: a.start, mode: currentMode };
    lastCtx = a.ctx; lastRoutes = a.routes; lastSuggest = a.suggest;
    renderContextLayers(a.ctx);
    if (a.hazards) renderHazards(a.hazards);
    renderRoutes(a.routes);
    if (a.suggest) { if (suggestBox) suggestBox.checked = true; renderSuggestions(a.suggest); }
    else { if (suggestBox) suggestBox.checked = false; clearSuggestions(); }
    setTimeout(function () {
      map.invalidateSize(true);
      map.fitBounds(zoneRect.getBounds(), { padding: [40, 40], maxZoom: 16 });
    }, 100);
    if (savedBox) savedBox.open = false;
  }

  function handleOffline() {
    updateOnlineBanner();
    if (viewingSaved) {
      // Routes can't be recalculated offline: put the pin, zone and mode back as they were saved.
      openSaved(viewingSaved);
      showToast("You're offline. Routes can't be recalculated, so the saved routes are shown.", 5000);
      return;
    }
    routeLayers && routeLayers.clearLayers();
    routeCards.innerHTML = '<div class="panel-placeholder error-card"><p><strong>You\'re offline.</strong> ' +
      "Routes need an internet connection to calculate. Open an area you saved for offline from " +
      "<strong>Saved for offline</strong> at the top of the page.</p></div>";
    refreshSavedList(true);
  }

  function updateOnlineBanner() {
    if (!offlineBanner) return;
    if (navigator.onLine) {
      offlineBanner.hidden = true;
    } else {
      offlineBanner.hidden = false;
      offlineBanner.textContent = "📴 You're offline. Saved areas still work; new searches and routes need a connection.";
    }
  }

  window.addEventListener("offline", function () { updateOnlineBanner(); refreshSavedList(true); });
  window.addEventListener("online", function () {
    updateOnlineBanner();
    showToast("Back online.", 3000);
    if (viewingSaved) {
      setStatus("📍 " + viewingSaved.label + " (showing saved copy; move the pin or zone to refresh)", false);
      viewingSaved = null;
    }
    refreshHazards();
  });

  if (saveBtn) saveBtn.addEventListener("click", saveForOffline);
  if (clearBtn) {
    clearBtn.addEventListener("click", function () {
      dbOp("readwrite", function (s) { s.clear(); })
        .then(function () {
          if (navigator.serviceWorker && navigator.serviceWorker.controller) {
            navigator.serviceWorker.controller.postMessage({ type: "clear" });
          }
          refreshSavedList();
          showToast("All offline data was deleted from this device.", 4000);
        })
        .catch(function () {});
    });
  }
  updateOnlineBanner();
  refreshSavedList(!navigator.onLine);

  // ── POST helper: locations, zones and searched addresses travel in the request
  //    body, never in URLs (which end up in browser history and server logs). ──
  function postJSON(url, payload) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "application/json", "X-CSRFToken": getCSRF() },
      body: JSON.stringify(payload)
    });
  }

  // ── CSRF helper ──
  function getCSRF() {
    var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
    if (input && input.value) return input.value;
    var cookie = document.cookie.split(";").find(function (c) {
      return c.trim().startsWith("csrftoken=");
    });
    return cookie ? cookie.split("=")[1].trim() : "";
  }

  // ── Delegated click for street view triggers ──
  document.addEventListener("click", function (e) {
    var btn = e.target.closest(".btn-sv-trigger");
    if (btn) {
      e.preventDefault();
      e.stopPropagation();
      var lat = parseFloat(btn.getAttribute("data-lat"));
      var lon = parseFloat(btn.getAttribute("data-lon"));
      var name = btn.getAttribute("data-name") || "Street View";
      if (!isNaN(lat) && !isNaN(lon)) {
        openStreetView(lat, lon, name);
      }
    }
  });

  // ── Google Street View drawer ──
  function openStreetView(lat, lon, label) {
    lastStreetViewQuery = { lat: lat, lon: lon, label: label };

    var drawer = document.getElementById("streetview-drawer");
    var titleEl = document.getElementById("streetview-title");
    var subEl = document.getElementById("streetview-subtitle");
    var linkGmaps = document.getElementById("streetview-link-gmaps");
    var statusEl = document.getElementById("streetview-status");
    var iframe = document.getElementById("streetview-iframe");

    if (!drawer) return;

    drawer.hidden = false;
    titleEl.textContent = label || "Google Street View";
    subEl.textContent = "Loading 360° panorama…";
    if (statusEl) {
      statusEl.hidden = false;
      statusEl.innerHTML = '<span class="spinner"></span> Connecting to Google Street View…';
    }

    var defaultPanoUrl = "https://www.google.com/maps/@?api=1&map_action=pano&viewpoint=" + lat + "," + lon;
    if (linkGmaps) linkGmaps.href = defaultPanoUrl;

    postJSON("/api/streetview", { lat: lat, lon: lon })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        if (data.gmaps_url && linkGmaps) linkGmaps.href = data.gmaps_url;
        subEl.textContent = "360° Street Panorama";
        if (statusEl) statusEl.hidden = true;

        if (iframe && data.embed_url) {
          iframe.src = data.embed_url;
        }
      })
      .catch(function (err) {
        subEl.textContent = "Google Street View";
        if (statusEl) statusEl.hidden = true;
        if (iframe) {
          iframe.src = "https://maps.google.com/maps?layer=c&cbll=" + lat + "," + lon + "&cbp=12,0,0,0,0&output=svembed";
        }
      });
  }

  // ── HTML escape ──
  function escapeHtml(text) {
    var el = document.createElement("span");
    el.textContent = text || "";
    return el.innerHTML;
  }

})();
