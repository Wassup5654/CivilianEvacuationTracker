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
  let damageLayer, deadEndLayer, strandedLayer, routeDeadEndLayer, shelterLayer, routeLayers;

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
    tileOSM = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
    });

    tileSat = L.tileLayer(
      "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
      maxZoom: 19,
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

    // Wire up layer toggles
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

  // ── Debounced API fetch ──
  function debouncedFetch() {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(fetchContextAndRoutes, DEBOUNCE_MS);
  }

  // ── Fetch context + routes in parallel with immediate rendering ──
  async function fetchContextAndRoutes() {
    var bbox = getZoneBbox();
    if (!bbox || !startMarker) return;

    var token = ++fetchToken;
    routeCards.innerHTML = '<div class="panel-loading"><span class="spinner"></span> Computing best evacuation routes…</div>';

    var startPos = startMarker.getLatLng();
    var bboxStr = bbox.join(",");

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
        renderRoutes(routeData);
        scheduleRadarRefresh(routeData.radar_pending, token);
      })
      .catch(function (err) {
        if (token !== fetchToken) return;
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

    // 2. Fetch context layers (damage, shelters, dead ends) independently
    fetch("/api/context?bbox=" + encodeURIComponent(bboxStr) + "&mode=" + currentMode)
      .then(function (resp) {
        if (!resp.ok) return null;
        return resp.json();
      })
      .then(function (ctx) {
        if (token !== fetchToken || !ctx) return;
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

    var routes = data.routes || [];
    if (routes.length === 0) {
      routeCards.innerHTML = '<p class="panel-placeholder">' +
        escapeHtml(data.warnings && data.warnings.length ? data.warnings[0] : "No valid routes found.") +
        '</p>';
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
    matchesList.innerHTML = "";
    setStatus("Looking up that address…", false);
    var query = input.value.trim();
    if (query.length < 3) {
      setStatus("Type at least 3 characters.", true);
      return;
    }
    fetch("/api/search?q=" + encodeURIComponent(query))
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
        setStatus("Could not reach the address lookup.", true);
      });
  });

  // ── Select a place ──
  function selectPlace(match) {
    var lat = parseFloat(match.latitude);
    var lon = parseFloat(match.longitude);
    matchesList.innerHTML = "";
    setStatus("📍 " + match.display_name, false);
    evacBody.hidden = false;

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

  // ── Geolocation ──
  geoBtn.addEventListener("click", function () {
    if (!navigator.geolocation) {
      showToast("Geolocation not supported by your browser.");
      return;
    }
    setStatus("Getting your location…", false);
    navigator.geolocation.getCurrentPosition(
      function (pos) {
        selectPlace({
          display_name: "Your location",
          latitude: pos.coords.latitude.toFixed(7),
          longitude: pos.coords.longitude.toFixed(7),
          kind: "geolocation"
        });
      },
      function (err) {
        setStatus("Location access denied: " + err.message, true);
      },
      { enableHighAccuracy: true, timeout: 10000 }
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

    var queryUrl = "/api/streetview?lat=" + encodeURIComponent(lat) + "&lon=" + encodeURIComponent(lon);

    fetch(queryUrl, {
      headers: { "Accept": "application/json" }
    })
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
