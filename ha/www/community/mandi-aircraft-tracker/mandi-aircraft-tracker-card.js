const LEAFLET_JS_URL = "/local/community/mandi-aircraft-tracker/leaflet.js";
const LEAFLET_CSS_URL = "/local/community/mandi-aircraft-tracker/leaflet.css";
// Fallback only, for the rare case there's no position history at all yet
// (e.g. right after first install) -- HA's configured home location.
const FALLBACK_CENTER = [38.9517, -92.3341];
const MILES_PER_DEGREE_LAT = 69.0;
const NO_HISTORY_VIEW_MILES_ACROSS = 20;
const BOUNDS_PADDING_MILES = 2;

const CURRENT_COLOR_LIGHT = "#2a78d6";
const CURRENT_COLOR_DARK = "#3987e5";
const CURRENT_WEIGHT = 4;
const HISTORICAL_WEIGHT = 3;
// Recency-graded single-hue blue ramp for historical trails, most-recent
// first (index 0 = deepest/strongest blue = most recent flight). A flight
// older than the last step reuses that step (see the Math.min clamp below)
// rather than growing the array -- there's no dashboard value in more than
// 5 visually distinct steps. This is an "ordinal" ramp per the dataviz
// skill (one hue, monotone lightness, not a categorical hue set) --
// validated via validate_palette.js --ordinal --mode light (map tiles are
// always the light OSM style regardless of HA theme, so one ramp covers
// both): lightness monotone, adjacent steps >=0.06 apart, single hue
// (23° spread), light end (#6baed6) at 2.36:1 vs surface -- all PASS.
// Previously a flat gray (#898781) + opacity taper; opacity dropped in
// favor of full-strength color doing the recency encoding, since a
// translucent line reads as weaker, not "older", once you're looking for a
// bold color ramp specifically (see docs/superpowers/specs/2026-09-06-aircraft-tracker-design.md
// for why 5 distinct *hues* was rejected -- this is one hue, not five, so
// that finding doesn't apply here).
const HISTORICAL_COLOR_STEPS = ["#08306b", "#08519c", "#2171b5", "#4292c6", "#6baed6"];

function ensureLeafletScriptLoaded() {
  if (window.L) return Promise.resolve();
  if (window.__mandiLeafletLoading) return window.__mandiLeafletLoading;
  window.__mandiLeafletLoading = new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = LEAFLET_JS_URL;
    script.onload = () => resolve();
    script.onerror = () => reject(new Error("Failed to load Leaflet"));
    document.head.appendChild(script);
  });
  return window.__mandiLeafletLoading;
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : String(str);
  return div.innerHTML;
}

// Bounding box roughly `milesAcross` x `milesAcross`, centered on [lat, lon].
function computeFallbackBounds(lat, lon, milesAcross) {
  const halfMiles = milesAcross / 2;
  const latDelta = halfMiles / MILES_PER_DEGREE_LAT;
  const lonDelta = halfMiles / (MILES_PER_DEGREE_LAT * Math.cos((lat * Math.PI) / 180));
  return [
    [lat - latDelta, lon - lonDelta],
    [lat + latDelta, lon + lonDelta],
  ];
}

// Bounding box covering every point in `points` ([[lat,lon],...]), padded by
// `paddingMiles` on every side so trails don't touch the map edge.
function computePointsBounds(points, paddingMiles) {
  const lats = points.map((p) => p[0]);
  const lons = points.map((p) => p[1]);
  const minLat = Math.min(...lats);
  const maxLat = Math.max(...lats);
  const minLon = Math.min(...lons);
  const maxLon = Math.max(...lons);
  const midLat = (minLat + maxLat) / 2;
  const latPad = paddingMiles / MILES_PER_DEGREE_LAT;
  const lonPad = paddingMiles / (MILES_PER_DEGREE_LAT * Math.cos((midLat * Math.PI) / 180));
  return [
    [minLat - latPad, minLon - lonPad],
    [maxLat + latPad, maxLon + lonPad],
  ];
}

class MandiAircraftTrackerCard extends HTMLElement {
  constructor() {
    super();
    this._boundUpdateMapHeight = () => this._updateMapHeight();
  }

  setConfig(config) {
    this._config = config || {};
    this._entity = this._config.entity || "sensor.n8382a_tracker";
  }

  getCardSize() {
    return 6;
  }

  set hass(hass) {
    this._hass = hass;
    this._init().then(() => this._render());
  }

  _init() {
    if (this._initPromise) return this._initPromise;
    this._initPromise = ensureLeafletScriptLoaded().then(async () => {
      this.innerHTML = `
        <link rel="stylesheet" href="${LEAFLET_CSS_URL}">
        <ha-card>
          <div class="mandi-map-header" style="padding: 8px 16px; font-size: 0.9em; color: var(--secondary-text-color);"></div>
          <div class="mandi-map"></div>
        </ha-card>
      `;
      this._headerEl = this.querySelector(".mandi-map-header");
      this._mapEl = this.querySelector(".mandi-map");

      // HA's `type: panel` view stretches the card to full width but never
      // propagates viewport height down to the card's content -- same
      // situation as mandi-fire-medical-map-card.js, same fix.
      this._updateMapHeight();
      window.addEventListener("resize", this._boundUpdateMapHeight);

      const cssLink = this.querySelector('link[rel="stylesheet"]');
      await new Promise((resolve) => {
        if (cssLink.sheet) {
          resolve();
        } else {
          cssLink.onload = () => resolve();
          cssLink.onerror = () => resolve(); // don't hang forever if the stylesheet fails to load
        }
      });

      // zoomSnap: 0 -- same reasoning as mandi-fire-medical-map-card.js:
      // lets fitBounds match the requested box tightly instead of Leaflet's
      // default integer-only zoom rounding down (up to ~2x too much area).
      this._map = window.L.map(this._mapEl, { zoomSnap: 0 });
      const homeLat = this._hass?.config?.latitude ?? FALLBACK_CENTER[0];
      const homeLon = this._hass?.config?.longitude ?? FALLBACK_CENTER[1];
      this._map.fitBounds(computeFallbackBounds(homeLat, homeLon, NO_HISTORY_VIEW_MILES_ACROSS));
      window.L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        attribution: "&copy; OpenStreetMap contributors",
        maxZoom: 19,
      }).addTo(this._map);
      this._trailsLayer = window.L.layerGroup().addTo(this._map);

      this._resizeObserver = new ResizeObserver(() => {
        this._map.invalidateSize();
      });
      this._resizeObserver.observe(this._mapEl);
    });
    return this._initPromise;
  }

  _updateMapHeight() {
    if (!this._mapEl) return;
    const top = this._mapEl.getBoundingClientRect().top;
    const bottomMargin = 16;
    const height = Math.max(300, window.innerHeight - top - bottomMargin);
    this._mapEl.style.height = `${height}px`;
    if (this._map) this._map.invalidateSize();
  }

  disconnectedCallback() {
    if (this._resizeObserver) {
      this._resizeObserver.disconnect();
    }
    window.removeEventListener("resize", this._boundUpdateMapHeight);
  }

  _formatDate(iso) {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }

  _relativeTime(iso) {
    const t = Date.parse(iso);
    if (isNaN(t)) return "";
    const minutes = Math.round((Date.now() - t) / 60000);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.round(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.round(hours / 24);
    return `${days}d ago`;
  }

  _render() {
    if (!this._map || !this._hass) return;
    const stateObj = this._hass.states[this._entity];
    if (!stateObj) {
      this._headerEl.textContent = `${this._entity} not found`;
      return;
    }

    const tailNumber = stateObj.attributes.tail_number || this._entity;
    const status = stateObj.attributes.status;
    const fetchedAt = stateObj.attributes.fetched_at;
    const historicalFlights = stateObj.attributes.historical_flights || [];
    const currentTrail = stateObj.attributes.current_trail || null;
    const current = stateObj.attributes.current || null;

    const ageMin = Math.round((Date.now() - Date.parse(fetchedAt)) / 60000);
    const freshnessHtml =
      !isNaN(ageMin) && ageMin > 5
        ? `<span style="color: #e65100">⚠ Stale — updated ${ageMin}m ago</span>`
        : `<span style="color: #757575">Updated ${isNaN(ageMin) ? "just now" : ageMin + "m ago"}</span>`;

    let statusHtml;
    if (status === "flying" && current) {
      const parts = [];
      if (current.altitude_ft != null) parts.push(`${current.altitude_ft} ft`);
      if (current.ground_speed_kt != null) parts.push(`${current.ground_speed_kt} kt`);
      statusHtml = `<b>${escapeHtml(tailNumber)} — Flying now</b>${
        parts.length ? " — " + escapeHtml(parts.join(", ")) : ""
      }`;
    } else {
      const lastFlight = historicalFlights[0];
      const lastSeenHtml = lastFlight
        ? ` — last flight ${escapeHtml(this._relativeTime(lastFlight.ended_at))}`
        : "";
      statusHtml = `<b>${escapeHtml(tailNumber)} — Not currently flying</b>${lastSeenHtml}`;
    }

    this._headerEl.innerHTML = `${statusHtml} — ${freshnessHtml}`;

    // Check theme change BEFORE data-change early-return so theme-only toggles trigger re-render.
    const isDark = !!this._hass?.themes?.darkMode;
    const currentColor = isDark ? CURRENT_COLOR_DARK : CURRENT_COLOR_LIGHT;
    const themeChanged = isDark !== this._lastRenderedDarkMode;

    // `fetched_at` changes on every ~60s poll regardless of whether the
    // actual trail/position data changed (the common case: grounded, same
    // historical flights, nothing new) -- gating the map redraw (which
    // includes fitBounds) on that would snap a user's pan/zoom back every
    // minute even when nothing rendered actually changed. Gate on a cheap
    // signature of the fields that actually affect rendering instead. The
    // header text above already updated unconditionally, every poll.
    const dataSignature = JSON.stringify({ status, currentTrail, current, historicalFlights });
    const dataChanged = dataSignature !== this._lastRenderedDataSignature;
    if (!dataChanged && !themeChanged) return;
    this._lastRenderedDataSignature = dataSignature;
    this._lastRenderedDarkMode = isDark;

    this._trailsLayer.clearLayers();
    const allPoints = [];

    // Historical trails first (drawn underneath), most-recent-first per the
    // cache shape -- index 0 gets the deepest (most recent) blue step.
    historicalFlights.forEach((flight, index) => {
      const points = flight.trail || [];
      if (points.length < 2) return; // a 1-point "trail" can't draw a line
      const color = HISTORICAL_COLOR_STEPS[Math.min(index, HISTORICAL_COLOR_STEPS.length - 1)];
      const line = window.L.polyline(points, {
        color,
        weight: HISTORICAL_WEIGHT,
        opacity: 1,
      });
      line.bindTooltip(
        `${index + 1} flight${index === 0 ? "" : "s"} ago — ` +
          `${escapeHtml(this._formatDate(flight.started_at))}, ${escapeHtml(String(flight.duration_min))} min`
      );
      line.addTo(this._trailsLayer);
      allPoints.push(...points);
    });

    // Flying aircraft: draw marker and include trail points even with just 1 point,
    // but only draw polyline if 2+ points exist.
    if (status === "flying" && currentTrail && currentTrail.length > 0) {
      allPoints.push(...currentTrail);

      const last = currentTrail[currentTrail.length - 1];
      const heading = current?.heading_deg ?? 0;
      const marker = window.L.marker(last, {
        icon: window.L.divIcon({
          html: `<div style="font-size: 22px; line-height: 1; transform: rotate(${heading}deg);">✈️</div>`,
          className: "mandi-aircraft-marker",
          iconSize: [24, 24],
        }),
      });
      marker.bindPopup(
        `<b>${escapeHtml(tailNumber)}</b><br>` +
          `${current?.altitude_ft ?? "?"} ft, ${current?.ground_speed_kt ?? "?"} kt, heading ${escapeHtml(String(heading))}°`
      );
      marker.addTo(this._trailsLayer);

      // Only draw polyline if we have 2+ points (Leaflet can't draw a line from 1 point).
      if (currentTrail.length >= 2) {
        const line = window.L.polyline(currentTrail, {
          color: currentColor,
          weight: CURRENT_WEIGHT,
          opacity: 1,
        });
        line.addTo(this._trailsLayer);
      }
    } else if (status !== "flying" && historicalFlights.length > 0) {
      // Grounded aircraft: show marker at last known position (from most recent flight).
      const lastFlight = historicalFlights[0];
      const lastPoint = lastFlight.trail[lastFlight.trail.length - 1];
      const marker = window.L.marker(lastPoint, {
        icon: window.L.divIcon({
          html: `<div style="font-size: 22px; line-height: 1; opacity: 0.6;">✈️</div>`,
          className: "mandi-aircraft-marker",
          iconSize: [24, 24],
        }),
      });
      marker.bindPopup(
        `<b>${escapeHtml(tailNumber)}</b><br>Not currently flying — ` +
          `last seen ${escapeHtml(this._relativeTime(lastFlight.ended_at))}`
      );
      marker.addTo(this._trailsLayer);
    }

    if (allPoints.length > 0) {
      this._map.fitBounds(computePointsBounds(allPoints, BOUNDS_PADDING_MILES));
    }
    // else: no history at all yet -- map stays on the fallback view set in _init().
  }
}

customElements.define("mandi-aircraft-tracker-card", MandiAircraftTrackerCard);
