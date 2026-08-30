(() => {
  "use strict";

  /*
   * Ожидаемый протокол /ws/aircraft:
   *   {"type":"snapshot","aircraft":[Aircraft, ...],"archived":[Aircraft, ...]};
   *   {"type":"upsert","aircraft":Aircraft}          — новый/изменённый борт;
   *   {"type":"remove","icao":"ABC123"}              — удалить борт.
   * Для совместимости snapshot может быть массивом, а delta — объектом
   * {"type":"delta","upsert":[...], "remove":["ABC123", ...],
   *  "archive":[Aircraft, ...], "archive_remove":["ABC123", ...]}.
   * Элемент upsert может содержать track_append с новыми точками вместо полной
   * истории. Aircraft обязан содержать icao (или hex), lat, lon; остальные
   * используемые поля необязательны: callsign, altitude/alt_baro,
   * speed/ground_speed, squawk, track/heading, distance, trail/positions
   * (массив точек [lat, lon] или {lat, lon}), lost_at для архива.
   */

  const state = {
    map: null,
    config: {},
    station: null,
    basemaps: {},
    activeBasemap: null,
    aircraft: new Map(),
    archived: new Map(),
    markers: new Map(),
    archiveMarkers: new Map(),
    tracks: new Map(),
    customLayers: new Map(),
    socket: null,
    reconnectTimer: null,
    reconnectAttempt: 0,
    tracksVisible: true,
    coverageVisible: true,
    coverageLayer: null,
    coverageRevision: -1,
    mapUserMoved: false,
    autoFitting: false,
    selectedIcao: null,
    search: "",
    journalMode: "decoded",
    journalEvents: [],
    lastEventId: 0,
    rawMessages: [],
    lastRawId: 0,
  };

  const el = {};
  const byId = (id) => document.getElementById(id);
  const finite = (value) => Number.isFinite(Number(value)) ? Number(value) : null;
  const text = (value, fallback = "—") =>
    value === null || value === undefined || value === "" ? fallback : String(value);
  const normalizeIcao = (aircraft) =>
    text(aircraft?.icao ?? aircraft?.hex, "").trim().toUpperCase();

  document.addEventListener("DOMContentLoaded", init);

  async function init() {
    cacheElements();
    bindUi();
    tickClock();
    window.setInterval(tickClock, 1000);

    if (!window.L) {
      setConnection("offline", "Карта недоступна: Leaflet не загружен");
      return;
    }

    try {
      state.config = await fetchJson("/api/config");
    } catch (error) {
      console.warn("Не удалось загрузить конфигурацию, используются безопасные значения.", error);
      state.config = {};
    }

    configureStation();
    setRadioAvailable(Boolean(state.config.radio?.enabled));
    createMap();
    await Promise.allSettled([
      loadInitialAircraft(),
      loadJournal(),
      loadLayers(),
      loadRadioChannels(),
      loadCoverage(),
    ]);
    connectAircraftSocket();
    void refreshHealth();
    window.setInterval(loadRadioChannels, 5000);
    window.setInterval(refreshHealth, 5000);
    window.setInterval(loadJournal, 1000);
    window.setInterval(loadCoverage, 5000);
  }

  function cacheElements() {
    [
      "station-name", "connection", "connection-text", "clock", "clock-date", "clock-time",
      "visible-count", "aircraft-search", "aircraft-list",
      "archive-section", "archive-count", "archive-list",
      "custom-layers", "radio-list", "radio-audio", "now-playing",
      "ofm-option", "fit-aircraft", "toggle-tracks", "toggle-coverage",
      "coverage-visible", "coverage-stats", "reset-coverage",
      "reload-layers", "reload-radio",
      "journal-list", "journal-count", "journal-hint", "clear-journal",
    ].forEach((id) => { el[id] = byId(id); });
  }

  function bindUi() {
    document.querySelectorAll(".tab").forEach((button) => {
      button.addEventListener("click", () => switchTab(button.dataset.tab));
    });
    document.querySelectorAll('input[name="basemap"]').forEach((input) => {
      input.addEventListener("change", () => setBasemap(input.value));
    });
    el["aircraft-search"].addEventListener("input", (event) => {
      state.search = event.target.value.trim().toUpperCase();
      renderAircraftList();
    });
    el["fit-aircraft"].addEventListener("click", fitAircraft);
    el["toggle-tracks"].addEventListener("click", toggleTracks);
    el["toggle-coverage"].addEventListener("click", () => setCoverageVisible(!state.coverageVisible));
    el["coverage-visible"].addEventListener("change", () => {
      setCoverageVisible(el["coverage-visible"].checked);
    });
    el["reset-coverage"].addEventListener("click", resetCoverage);
    el["reload-layers"].addEventListener("click", loadLayers);
    el["reload-radio"].addEventListener("click", loadRadioChannels);
    el["clear-journal"].addEventListener("click", clearJournal);
    document.querySelectorAll("[data-journal-mode]").forEach((button) => {
      button.addEventListener("click", () => {
        state.journalMode = button.dataset.journalMode;
        document.querySelectorAll("[data-journal-mode]").forEach((item) => {
          item.classList.toggle("is-active", item === button);
        });
        renderJournal();
        void loadJournal();
      });
    });
  }

  function switchTab(name) {
    document.querySelectorAll(".tab").forEach((tab) => {
      tab.classList.toggle("is-active", tab.dataset.tab === name);
    });
    document.querySelectorAll(".tab-panel").forEach((panel) => {
      panel.classList.toggle("is-active", panel.id === `panel-${name}`);
    });
  }

  function tickClock() {
    const now = new Date();
    el.clock.dateTime = now.toISOString();
    el["clock-date"].textContent = now.toLocaleDateString("ru-RU", {
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
      timeZone: "UTC",
    });
    el["clock-time"].textContent = formatUtcTime(now);
  }

  function configureStation() {
    const mapConfig = state.config.map || {};
    const station = state.config.station || {};
    const lat = finite(station.lat ?? state.config.station_lat ?? mapConfig.center?.[0]);
    const lon = finite(station.lon ?? state.config.station_lon ?? mapConfig.center?.[1]);
    state.station = lat !== null && lon !== null ? { lat, lon } : null;
    el["station-name"].textContent = text(
      station.name ?? state.config.station_name,
      "Локальная станция",
    );
  }

  function createMap() {
    const mapConfig = state.config.map || {};
    const center = state.station
      ? [state.station.lat, state.station.lon]
      : (Array.isArray(mapConfig.center) ? mapConfig.center : [57.1896, 65.3243]);

    state.map = L.map("map", {
      center,
      zoom: finite(mapConfig.zoom) ?? 8,
      zoomControl: true,
      preferCanvas: true,
    });
    state.map.createPane("coverage");
    state.map.getPane("coverage").style.zIndex = 350;
    state.map.getPane("coverage").style.pointerEvents = "none";
    state.map.on("dragstart", () => {
      if (!state.autoFitting) state.mapUserMoved = true;
    });
    state.map.getContainer().addEventListener("focusin", () => {
      window.scrollTo(0, 0);
    });
    if (state.station) {
      L.circleMarker([state.station.lat, state.station.lon], {
        radius: 6,
        color: "#36b7ff",
        weight: 2,
        fillColor: "#36b7ff",
        fillOpacity: .35,
        interactive: false,
      }).addTo(state.map).bindTooltip(el["station-name"].textContent, {
        permanent: false,
        direction: "bottom",
        className: "aircraft-tooltip",
      });
    }

    const osmConfig = mapConfig.osm || state.config.osm || {};
    state.basemaps.osm = L.tileLayer(
      osmConfig.url_template || osmConfig.url || "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
      {
        attribution: osmConfig.attribution || "&copy; OpenStreetMap contributors",
        maxZoom: finite(osmConfig.max_zoom) ?? 19,
      },
    );

    const ofm = mapConfig.openflightmaps || state.config.openflightmaps || {};
    const ofmUrl = ofm.url_template || ofm.url || mapConfig.openflightmaps_url;
    if (ofmUrl) {
      state.basemaps.ofm = L.tileLayer(ofmUrl, {
        attribution: ofm.attribution || "OpenFlightMaps",
        maxZoom: finite(ofm.max_zoom) ?? 18,
        ...(ofm.options || {}),
      });
      el["ofm-option"].hidden = false;
    }

    setBasemap(state.config.default_basemap || mapConfig.default_basemap || "osm");
    window.setTimeout(() => state.map.invalidateSize(), 0);
  }

  function setBasemap(name) {
    if (!state.map || !state.basemaps[name]) name = "osm";
    if (state.activeBasemap) state.map.removeLayer(state.activeBasemap);
    state.activeBasemap = state.basemaps[name];
    state.activeBasemap.addTo(state.map);
    const input = document.querySelector(`input[name="basemap"][value="${name}"]`);
    if (input) input.checked = true;
  }

  async function loadInitialAircraft() {
    try {
      const payload = await fetchJson("/api/aircraft");
      const aircraft = Array.isArray(payload) ? payload : (payload.aircraft || []);
      replaceAircraft(aircraft, payload.archived || []);
    } catch (error) {
      console.error("Ошибка начальной загрузки бортов:", error);
      setConnection("offline", "Начальные данные недоступны");
    }
  }

  function replaceAircraft(items, archived = []) {
    const incoming = new Set();
    items.forEach((aircraft) => {
      const icao = normalizeIcao(aircraft);
      if (!icao) return;
      incoming.add(icao);
      upsertAircraft(aircraft, false);
    });
    [...state.aircraft.keys()].forEach((icao) => {
      if (!incoming.has(icao)) removeAircraft(icao, false);
    });
    replaceArchive(Array.isArray(archived) ? archived : []);
    finishAircraftUpdate();
  }

  function replaceArchive(items) {
    const incoming = new Set();
    items.forEach((aircraft) => {
      const icao = normalizeIcao(aircraft);
      if (!icao || state.aircraft.has(icao)) return;
      incoming.add(icao);
      archiveAircraft(aircraft, false);
    });
    [...state.archived.keys()].forEach((icao) => {
      if (!incoming.has(icao)) dropArchive(icao, false);
    });
  }

  function upsertAircraft(aircraft, render = true) {
    const icao = normalizeIcao(aircraft);
    if (!icao) return;

    const previous = state.aircraft.get(icao) || state.archived.get(icao) || {};
    dropArchive(icao, false);
    const incomingLat = finite(aircraft.lat ?? aircraft.latitude);
    const incomingLon = finite(aircraft.lon ?? aircraft.lng ?? aircraft.longitude);
    const merged = { ...previous, ...aircraft, icao };
    if (incomingLat === null) {
      merged.lat = finite(previous.lat ?? previous.latitude);
    }
    if (incomingLon === null) {
      merged.lon = finite(previous.lon ?? previous.lng ?? previous.longitude);
    }
    if (Array.isArray(aircraft.track_append)) {
      const track = Array.isArray(previous.track) ? [...previous.track] : [];
      aircraft.track_append.forEach((point) => {
        const last = track[track.length - 1];
        const sameTimestamp = Array.isArray(last) && Array.isArray(point) &&
          last[3] !== undefined && last[3] === point[3];
        if (!sameTimestamp) track.push(point);
      });
      merged.track = track.slice(-500);
    }
    state.aircraft.set(icao, merged);

    const lat = finite(merged.lat ?? merged.latitude);
    const lon = finite(merged.lon ?? merged.lng ?? merged.longitude);
    if (lat !== null && lon !== null) {
      updateMarker(merged, lat, lon);
      updateTrack(merged);
    }
    if (render) finishAircraftUpdate();
  }

  function removeAircraft(icaoValue, render = true) {
    const icao = text(icaoValue, "").toUpperCase();
    state.aircraft.delete(icao);
    removeMapObjects(icao);
    if (state.selectedIcao === icao) state.selectedIcao = null;
    dropArchive(icao, false);
    if (render) finishAircraftUpdate();
  }

  function archiveAircraft(aircraft, render = true) {
    const icao = normalizeIcao(aircraft);
    if (!icao) return;
    state.aircraft.delete(icao);
    removeMapObjects(icao);
    const merged = { ...(state.archived.get(icao) || {}), ...aircraft, icao, status: "archived" };
    state.archived.set(icao, merged);
    updateArchiveMarker(merged);
    if (render) finishAircraftUpdate();
  }

  function dropArchive(icaoValue, render = true) {
    const icao = text(icaoValue, "").toUpperCase();
    state.archived.delete(icao);
    const marker = state.archiveMarkers.get(icao);
    if (marker) state.map.removeLayer(marker);
    state.archiveMarkers.delete(icao);
    if (render) finishAircraftUpdate();
  }

  function removeMapObjects(icao) {
    const marker = state.markers.get(icao);
    const track = state.tracks.get(icao);
    if (marker) state.map.removeLayer(marker);
    if (track) state.map.removeLayer(track);
    state.markers.delete(icao);
    state.tracks.delete(icao);
  }

  function finishAircraftUpdate() {
    renderAircraftList();
    revealAircraftOnMap();
  }

  function updateMarker(aircraft, lat, lon) {
    const icao = aircraft.icao;
    const altitude = aircraftAltitude(aircraft);
    const color = altitudeColor(altitude);
    const rotation = finite(
      aircraft.track_deg ?? aircraft.calculated_track_deg ?? aircraft.heading,
    ) ?? 0;
    let marker = state.markers.get(icao);
    const icon = aircraftIcon(color, rotation);

    if (!marker) {
      marker = L.marker([lat, lon], {
        icon,
        zIndexOffset: Math.round(altitude || 0),
        riseOnHover: true,
      }).addTo(state.map);
      marker.on("click", () => selectAircraft(icao));
      marker.bindTooltip(createDataBlock(aircraft), dataBlockOptions(aircraft));
      marker.bindPopup(createTooltip(aircraft), { className: "aircraft-tooltip" });
      state.markers.set(icao, marker);
    } else {
      marker.setLatLng([lat, lon]);
      marker.setIcon(icon);
      marker.setTooltipContent(createDataBlock(aircraft));
      marker.setPopupContent(createTooltip(aircraft));
      marker.setZIndexOffset(Math.round(altitude || 0));
      marker.getTooltip()?.getElement()?.classList.toggle(
        "is-selected",
        icao === state.selectedIcao,
      );
    }
  }

  function updateArchiveMarker(aircraft) {
    const icao = aircraft.icao;
    const lat = finite(aircraft.lat ?? aircraft.latitude);
    const lon = finite(aircraft.lon ?? aircraft.lng ?? aircraft.longitude);
    const existing = state.archiveMarkers.get(icao);
    if (lat === null || lon === null) {
      if (existing) state.map.removeLayer(existing);
      state.archiveMarkers.delete(icao);
      return;
    }
    const altitude = aircraftAltitude(aircraft);
    const rotation = finite(
      aircraft.track_deg ?? aircraft.calculated_track_deg ?? aircraft.heading,
    ) ?? 0;
    const icon = aircraftIcon(altitudeColor(altitude), rotation, true);
    if (!existing) {
      const marker = L.marker([lat, lon], {
        icon,
        zIndexOffset: Math.round((altitude || 0) / 4) - 200,
        riseOnHover: true,
        opacity: .7,
      }).addTo(state.map);
      marker.on("click", () => selectAircraft(icao));
      marker.bindTooltip(createDataBlock(aircraft), dataBlockOptions(aircraft, true));
      marker.bindPopup(createTooltip(aircraft), { className: "aircraft-tooltip" });
      state.archiveMarkers.set(icao, marker);
      return;
    }
    existing.setLatLng([lat, lon]);
    existing.setIcon(icon);
    existing.setTooltipContent(createDataBlock(aircraft));
    existing.setPopupContent(createTooltip(aircraft));
  }

  function dataBlockOptions(aircraft, archived = false) {
    return {
      permanent: true,
      direction: "right",
      offset: [18, 0],
      className: `aircraft-label${aircraft.icao === state.selectedIcao ? " is-selected" : ""}${
        archived ? " is-archived" : ""
      }`,
      opacity: 1,
      interactive: false,
    };
  }

  function aircraftIcon(color, rotation, archived = false) {
    return L.divIcon({
      className: `aircraft-marker${archived ? " is-archived" : ""}`,
      iconSize: [30, 30],
      iconAnchor: [15, 15],
      html: `<div class="aircraft-marker__plane" style="--rotation:${rotation}deg;color:${color}">
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path fill="currentColor" stroke="#07111d" stroke-width=".7" d="M12 1.2c.8 0 1.35.85 1.55 2.15l.95 6.15 6.8 4.05v1.8l-6.55-1.9-.45 5.05 2.35 1.65v1.35L12 20.6l-4.65.9v-1.35L9.7 18.5l-.45-5.05-6.55 1.9v-1.8L9.5 9.5l.95-6.15C10.65 2.05 11.2 1.2 12 1.2Z"/>
        </svg>
      </div>`,
    });
  }

  function createDataBlock(aircraft) {
    const root = document.createElement("div");
    const callsign = document.createElement("div");
    callsign.className = "aircraft-label__id";
    callsign.textContent = text(aircraft.callsign, aircraft.icao).trim();

    const altitude = aircraftAltitude(aircraft);
    const verticalRate = finite(
      aircraft.vertical_rate_fpm ?? aircraft.baro_rate ?? aircraft.vert_rate,
    );
    const trend = verticalRate !== null && Math.abs(verticalRate) >= 200
      ? (verticalRate > 0 ? " ↑" : " ↓")
      : "";
    const speed = aircraftSpeed(aircraft);
    const motion = document.createElement("div");
    motion.className = "aircraft-label__line";
    motion.textContent = `${aircraft.on_ground ? "GND" : formatBlockAltitude(altitude)}${trend}${
      speed === null ? "" : ` ${String(Math.round(speed)).padStart(3, "0")}`
    }`;

    root.append(callsign, motion);
    const squawk = text(aircraft.squawk, "");
    const third = squawk || (callsign.textContent !== aircraft.icao ? aircraft.icao : "");
    if (third) {
      const line = document.createElement("div");
      line.className = "aircraft-label__line";
      line.textContent = third;
      root.append(line);
    }
    const sector = text(aircraft.sector, "").trim();
    if (sector) {
      const zone = document.createElement("div");
      zone.className = "aircraft-label__zone";
      zone.textContent = sector;
      root.append(zone);
    }
    return root;
  }

  function createTooltip(aircraft) {
    const root = document.createElement("div");
    const title = document.createElement("div");
    title.className = "tooltip-title";
    const callsign = document.createElement("strong");
    callsign.textContent = text(aircraft.callsign, "Без позывного");
    const icao = document.createElement("span");
    icao.textContent = aircraft.icao;
    title.append(callsign, icao);

    const grid = document.createElement("div");
    grid.className = "tooltip-grid";
    const rows = [
      ["Рейс", text(aircraft.callsign, "без позывного")],
      ["Высота", formatAltitude(aircraftAltitude(aircraft))],
      ["Скорость", formatSpeed(aircraftSpeed(aircraft))],
      ["Squawk", text(aircraft.squawk)],
      ["Дистанция", formatDistance(aircraftDistance(aircraft))],
    ];
    if (text(aircraft.sector, "").trim()) {
      rows.push(["Сектор", aircraft.sector]);
    }
    if (aircraft.lost_at) {
      rows.push(["Статус", formatLostAt(aircraft.lost_at)]);
    }
    rows.forEach(([label, value]) => {
      const labelNode = document.createElement("span");
      const valueNode = document.createElement("span");
      labelNode.textContent = label;
      valueNode.textContent = value;
      grid.append(labelNode, valueNode);
    });
    root.append(title, grid);
    return root;
  }

  function updateTrack(aircraft) {
    const points = normalizeTrail(
      aircraft.track ?? aircraft.trail ?? aircraft.positions ?? aircraft.track_history,
    );
    const existing = state.tracks.get(aircraft.icao);
    if (points.length < 2) {
      if (existing) state.map.removeLayer(existing);
      state.tracks.delete(aircraft.icao);
      return;
    }
    const options = {
      color: altitudeColor(aircraftAltitude(aircraft)),
      weight: 2,
      opacity: .65,
      interactive: false,
    };
    if (existing) {
      existing.setLatLngs(points);
      existing.setStyle(options);
    } else {
      const track = L.polyline(points, options);
      if (state.tracksVisible) track.addTo(state.map);
      state.tracks.set(aircraft.icao, track);
    }
  }

  function normalizeTrail(trail) {
    if (!Array.isArray(trail)) return [];
    return trail.map((point) => {
      if (Array.isArray(point)) return [finite(point[0]), finite(point[1])];
      return [finite(point?.lat ?? point?.latitude), finite(point?.lon ?? point?.lng ?? point?.longitude)];
    }).filter(([lat, lon]) => lat !== null && lon !== null);
  }

  function toggleTracks() {
    state.tracksVisible = !state.tracksVisible;
    state.tracks.forEach((track) => {
      if (state.tracksVisible && !state.map.hasLayer(track)) track.addTo(state.map);
      if (!state.tracksVisible && state.map.hasLayer(track)) state.map.removeLayer(track);
    });
    el["toggle-tracks"].classList.toggle("is-active", state.tracksVisible);
    el["toggle-tracks"].setAttribute("aria-pressed", String(state.tracksVisible));
  }

  function setCoverageVisible(visible) {
    state.coverageVisible = Boolean(visible);
    el["toggle-coverage"].classList.toggle("is-active", state.coverageVisible);
    el["toggle-coverage"].setAttribute("aria-pressed", String(state.coverageVisible));
    el["coverage-visible"].checked = state.coverageVisible;
    if (!state.coverageLayer || !state.map) return;
    if (state.coverageVisible && !state.map.hasLayer(state.coverageLayer)) {
      state.coverageLayer.addTo(state.map);
    }
    if (!state.coverageVisible && state.map.hasLayer(state.coverageLayer)) {
      state.map.removeLayer(state.coverageLayer);
    }
  }

  async function loadCoverage() {
    try {
      const payload = await fetchJson("/api/coverage");
      renderCoverage(payload);
    } catch (error) {
      console.warn("Ошибка загрузки розы покрытия:", error);
    }
  }

  async function resetCoverage() {
    if (!window.confirm("Сбросить накопленную розу покрытия?")) return;
    try {
      const payload = await fetchJson("/api/coverage/reset", { method: "POST" });
      state.coverageRevision = -1;
      renderCoverage(payload);
    } catch (error) {
      console.warn("Не удалось сбросить розу покрытия:", error);
    }
  }

  function renderCoverage(payload) {
    const stats = formatCoverageStats(payload);
    if (el["coverage-stats"]) el["coverage-stats"].textContent = stats;
    const revision = finite(payload?.revision) ?? 0;
    const points = Array.isArray(payload?.points) ? payload.points.filter((point) => (
      Array.isArray(point) && finite(point[0]) !== null && finite(point[1]) !== null
    )) : [];
    if (revision === state.coverageRevision && state.coverageLayer) return;
    state.coverageRevision = revision;
    if (!state.map) return;
    if (points.length < 4) {
      if (state.coverageLayer) {
        state.map.removeLayer(state.coverageLayer);
        state.coverageLayer = null;
      }
      return;
    }
    if (state.coverageLayer) {
      state.coverageLayer.setLatLngs(points);
    } else {
      state.coverageLayer = L.polygon(points, {
        pane: "coverage",
        color: "#ffc857",
        weight: 2,
        fillColor: "#ffc857",
        fillOpacity: .14,
        interactive: false,
      });
    }
    setCoverageVisible(state.coverageVisible);
  }

  function formatCoverageStats(payload) {
    const samples = finite(payload?.samples) ?? 0;
    const filled = finite(payload?.filled_bins) ?? 0;
    const maxRange = finite(payload?.max_range_km);
    if (!samples) return "Накопление начнётся с первым бортом.";
    const rangeText = maxRange === null ? "—" : formatDistance(maxRange);
    let started = "";
    if (payload?.started_at) {
      const date = new Date(payload.started_at);
      if (!Number.isNaN(date.getTime())) {
        started = ` с ${date.toLocaleDateString("ru-RU", {
          day: "2-digit",
          month: "2-digit",
          timeZone: "UTC",
        })} ${formatUtcTime(date)} UTC`;
      }
    }
    return `Макс. ${rangeText} · ${filled} из 360 направлений · ${samples} точек${started}`;
  }

  function fitAircraft() {
    const positions = [...state.markers.values()].map((marker) => marker.getLatLng());
    if (!positions.length || !state.map) return;
    state.autoFitting = true;
    if (positions.length === 1) {
      state.map.setView(positions[0], Math.max(state.map.getZoom(), 9), { animate: false });
    } else {
      state.map.fitBounds(L.latLngBounds(positions).pad(.18), { maxZoom: 10, animate: false });
    }
    state.autoFitting = false;
  }

  function revealAircraftOnMap() {
    if (!state.map || !state.markers.size || state.mapUserMoved) return;
    const bounds = L.latLngBounds(
      [...state.markers.values()].map((marker) => marker.getLatLng()),
    );
    const view = state.map.getBounds();
    if (view.isValid() && view.pad(.02).contains(bounds)) return;
    fitAircraft();
  }

  function selectAircraft(icao) {
    const previous = state.selectedIcao;
    state.selectedIcao = icao;
    if (previous && previous !== icao) refreshAircraftMarker(previous);
    refreshAircraftMarker(icao);
    const marker = state.markers.get(icao) || state.archiveMarkers.get(icao);
    if (marker) state.map.panTo(marker.getLatLng());
    renderAircraftList();
  }

  function refreshAircraftMarker(icao) {
    const live = state.aircraft.get(icao);
    if (live) {
      const lat = finite(live.lat ?? live.latitude);
      const lon = finite(live.lon ?? live.lng ?? live.longitude);
      if (lat !== null && lon !== null) updateMarker(live, lat, lon);
      return;
    }
    const archived = state.archived.get(icao);
    if (archived) updateArchiveMarker(archived);
  }

  function matchesSearch(aircraft) {
    const haystack = `${aircraft.icao} ${text(aircraft.callsign, "")} ${text(aircraft.squawk, "")}`.toUpperCase();
    return !state.search || haystack.includes(state.search);
  }

  function fillAircraftCard(aircraft, archived = false) {
    const card = byId("aircraft-card-template").content.firstElementChild.cloneNode(true);
    card.dataset.icao = aircraft.icao;
    card.classList.toggle("is-selected", state.selectedIcao === aircraft.icao);
    card.classList.toggle("is-archived", archived);
    card.style.setProperty("--aircraft-color", altitudeColor(aircraftAltitude(aircraft)));
    card.querySelector(".aircraft-card__flight strong").textContent = text(
      aircraft.callsign, "без позывного",
    );
    card.querySelector(".aircraft-card__flight small").textContent = aircraft.icao;
    const lat = finite(aircraft.lat ?? aircraft.latitude);
    const lon = finite(aircraft.lon ?? aircraft.lng ?? aircraft.longitude);
    const bits = [
      formatAltitude(aircraftAltitude(aircraft)),
      formatSpeed(aircraftSpeed(aircraft)),
    ];
    bits.push(lat === null || lon === null ? "нет координат" : formatDistance(aircraftDistance(aircraft)));
    card.querySelector(".aircraft-card__metrics").textContent = bits.join(" · ");
    const squawk = text(aircraft.squawk, "").trim();
    card.querySelector(".aircraft-card__squawk").textContent = squawk ? `SQ ${squawk}` : "";
    card.querySelector(".aircraft-card__zones").textContent = text(aircraft.sector, "");
    if (archived) {
      const lost = document.createElement("span");
      lost.className = "aircraft-card__lost";
      lost.textContent = formatLostAt(aircraft.lost_at);
      card.querySelector(".aircraft-card__body").append(lost);
    }
    card.addEventListener("click", () => selectAircraft(aircraft.icao));
    return card;
  }

  function renderAircraftList() {
    const fragment = document.createDocumentFragment();
    const items = [...state.aircraft.values()]
      .filter(matchesSearch)
      .sort((a, b) => {
        const da = aircraftDistance(a);
        const db = aircraftDistance(b);
        return (da ?? Infinity) - (db ?? Infinity) ||
          text(a.callsign, a.icao).localeCompare(text(b.callsign, b.icao));
      });

    items.forEach((aircraft) => fragment.append(fillAircraftCard(aircraft)));
    el["aircraft-list"].replaceChildren(
      fragment.childNodes.length ? fragment : emptyNode(state.search ? "Ничего не найдено" : "Нет активных бортов"),
    );
    el["visible-count"].textContent = String(items.length);
    renderArchiveList();
  }

  function renderArchiveList() {
    const items = [...state.archived.values()]
      .filter(matchesSearch)
      .sort((a, b) => text(b.lost_at, "").localeCompare(text(a.lost_at, "")));
    el["archive-section"].hidden = items.length === 0 && !state.search;
    if (!items.length) {
      el["archive-list"].replaceChildren(
        emptyNode(state.search && state.archived.size ? "Ничего не найдено в архиве" : "Архив пуст"),
      );
      el["archive-count"].textContent = "0";
      if (!state.archived.size) el["archive-section"].hidden = true;
      return;
    }
    const fragment = document.createDocumentFragment();
    items.forEach((aircraft) => fragment.append(fillAircraftCard(aircraft, true)));
    el["archive-list"].replaceChildren(fragment);
    el["archive-count"].textContent = String(items.length);
  }

  function aircraftAltitude(aircraft) {
    return finite(
      aircraft.altitude_ft ?? aircraft.altitude ?? aircraft.alt_baro ?? aircraft.alt_geom,
    );
  }

  function aircraftSpeed(aircraft) {
    return finite(aircraft.speed_kt ?? aircraft.speed ?? aircraft.ground_speed ?? aircraft.gs);
  }

  function aircraftDistance(aircraft) {
    const explicit = finite(aircraft.distance ?? aircraft.distance_km);
    if (explicit !== null) return explicit;
    const lat = finite(aircraft.lat ?? aircraft.latitude);
    const lon = finite(aircraft.lon ?? aircraft.lng ?? aircraft.longitude);
    if (!state.station || lat === null || lon === null) return null;
    return haversineKm(state.station.lat, state.station.lon, lat, lon);
  }

  function altitudeColor(altitude) {
    if (altitude === null) return "#a6b4c0";
    const value = Math.max(0, Math.min(45000, altitude));
    const stops = [
      [0, [64, 217, 139]], [10000, [96, 207, 255]],
      [25000, [165, 132, 255]], [45000, [255, 111, 145]],
    ];
    for (let i = 1; i < stops.length; i += 1) {
      if (value <= stops[i][0]) {
        const [fromValue, fromColor] = stops[i - 1];
        const [toValue, toColor] = stops[i];
        const ratio = (value - fromValue) / (toValue - fromValue);
        const rgb = fromColor.map((channel, index) =>
          Math.round(channel + (toColor[index] - channel) * ratio));
        return `rgb(${rgb.join(",")})`;
      }
    }
    return "#ff6f91";
  }

  const formatAltitude = (value) => value === null ? "—" : `${Math.round(value).toLocaleString("ru-RU")} ft`;
  const formatSpeed = (value) => value === null ? "—" : `${Math.round(value)} kt`;
  const formatDistance = (value) => value === null ? "—" : `${value < 10 ? value.toFixed(1) : Math.round(value)} км`;
  const formatUtcTime = (value) => {
    const date = value instanceof Date ? value : new Date(value);
    if (!value || Number.isNaN(date.getTime())) return "—";
    return date.toLocaleTimeString("ru-RU", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
      timeZone: "UTC",
    });
  };
  const formatLostAt = (value) => {
    const time = formatUtcTime(value);
    return time === "—" ? "пропал" : `пропал ${time} UTC`;
  };
  const formatBlockAltitude = (value) => {
    if (value === null) return "---";
    const hundreds = String(Math.max(0, Math.round(value / 100))).padStart(3, "0");
    return `${value >= 5000 ? "F" : "A"}${hundreds}`;
  };

  function haversineKm(lat1, lon1, lat2, lon2) {
    const rad = Math.PI / 180;
    const dLat = (lat2 - lat1) * rad;
    const dLon = (lon2 - lon1) * rad;
    const a = Math.sin(dLat / 2) ** 2 +
      Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(dLon / 2) ** 2;
    return 6371 * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  }

  function connectAircraftSocket() {
    clearTimeout(state.reconnectTimer);
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = state.config.aircraft_ws_url ||
      `${protocol}//${location.host}/ws/aircraft`;
    setConnection("connecting", state.reconnectAttempt ? "Повторное подключение…" : "Подключение…");

    try {
      state.socket = new WebSocket(wsUrl);
    } catch (error) {
      scheduleReconnect();
      return;
    }

    state.socket.addEventListener("open", () => {
      state.reconnectAttempt = 0;
      setConnection("online", "В реальном времени");
    });
    state.socket.addEventListener("message", (event) => {
      try {
        applySocketMessage(JSON.parse(event.data));
      } catch (error) {
        console.warn("Некорректное сообщение /ws/aircraft:", error);
      }
    });
    state.socket.addEventListener("close", scheduleReconnect);
    state.socket.addEventListener("error", () => state.socket?.close());
  }

  function applySocketMessage(message) {
    if (Array.isArray(message)) {
      replaceAircraft(message);
      return;
    }
    if (message.type === "snapshot") {
      replaceAircraft(message.aircraft || [], message.archived || []);
      return;
    }
    if (message.type === "resync") {
      void loadInitialAircraft();
      return;
    }
    if (message.type === "upsert" && message.aircraft) {
      upsertAircraft(message.aircraft);
      return;
    }
    if (message.type === "remove") {
      removeAircraft(message.icao ?? message.hex);
      return;
    }
    if (Array.isArray(message.upsert) || Array.isArray(message.remove) || Array.isArray(message.archive)) {
      (message.upsert || []).forEach((aircraft) => upsertAircraft(aircraft, false));
      const archivedIcaos = new Set(
        (message.archive || []).map((aircraft) => normalizeIcao(aircraft)).filter(Boolean),
      );
      (message.archive_remove || []).forEach((icao) => dropArchive(icao, false));
      (message.archive || []).forEach((aircraft) => archiveAircraft(aircraft, false));
      (message.remove || []).forEach((icao) => {
        if (!archivedIcaos.has(text(icao, "").toUpperCase())) {
          removeAircraft(icao, false);
        }
      });
      finishAircraftUpdate();
    }
  }

  function scheduleReconnect() {
    if (state.reconnectTimer) return;
    setConnection("offline", "Связь потеряна");
    const delay = Math.min(30000, 1000 * (2 ** state.reconnectAttempt)) + Math.random() * 500;
    state.reconnectAttempt += 1;
    state.reconnectTimer = window.setTimeout(() => {
      state.reconnectTimer = null;
      connectAircraftSocket();
    }, delay);
  }

  function setConnection(status, label) {
    el.connection.dataset.state = status;
    el["connection-text"].textContent = label;
  }

  async function refreshHealth() {
    try {
      const health = await fetchJson("/api/health");
      const adsb = health.adsb || {};
      if (adsb.status === "online") {
        setConnection("online", "ADS-B работает");
      } else {
        const labels = {
          unavailable: "ADS-B: readsb недоступен",
          stale: "ADS-B: данные устарели",
          invalid: "ADS-B: ошибка JSON",
        };
        setConnection("offline", labels[adsb.status] || "ADS-B недоступен");
      }
    } catch (_error) {
      // WebSocket owns the connection indicator if the health endpoint is unavailable.
    }
  }

  const JOURNAL_LIMIT = 400;

  async function loadJournal() {
    if (state.journalMode === "raw") await loadRawMessages();
    else await loadDecodedMessages();
  }

  function mergeJournalItems(existing, incoming) {
    if (!incoming.length) return existing;
    const knownIds = new Set(existing.map((item) => item.id));
    return existing.concat(incoming.filter((item) => !knownIds.has(item.id))).slice(-JOURNAL_LIMIT);
  }

  function clearJournal() {
    if (state.journalMode === "raw") state.rawMessages = [];
    else state.journalEvents = [];
    renderJournal();
  }

  async function loadDecodedMessages() {
    try {
      const payload = await fetchJson(
        `/api/adsb/messages?after_id=${state.lastEventId}&limit=${JOURNAL_LIMIT}`,
      );
      const events = Array.isArray(payload.events) ? payload.events : [];
      state.lastEventId = Math.max(
        state.lastEventId,
        finite(payload.last_id) ?? 0,
        ...events.map((event) => finite(event.id) ?? 0),
      );
      if (events.length) {
        state.journalEvents = mergeJournalItems(state.journalEvents, events);
        renderJournal();
      } else if (!state.journalEvents.length) {
        renderJournal();
      }
    } catch (error) {
      if (!state.journalEvents.length) {
        el["journal-list"].replaceChildren(
          emptyNode("Журнал ADS-B недоступен", true),
        );
      }
      console.warn("Ошибка загрузки журнала ADS-B:", error);
    }
  }

  async function loadRawMessages() {
    try {
      const payload = await fetchJson(
        `/api/adsb/raw?after_id=${state.lastRawId}&limit=${JOURNAL_LIMIT}`,
      );
      const messages = Array.isArray(payload.messages) ? payload.messages : [];
      state.lastRawId = Math.max(
        state.lastRawId,
        finite(payload.last_id) ?? 0,
        ...messages.map((message) => finite(message.id) ?? 0),
      );
      if (messages.length) {
        state.rawMessages = mergeJournalItems(state.rawMessages, messages);
        renderJournal();
      } else if (!state.rawMessages.length) {
        renderJournal();
      }
    } catch (error) {
      if (!state.rawMessages.length) {
        el["journal-list"].replaceChildren(
          emptyNode("Поток сырых ADS-B сообщений недоступен", true),
        );
      }
      console.warn("Ошибка загрузки сырых ADS-B сообщений:", error);
    }
  }

  function renderJournal() {
    const rawMode = state.journalMode === "raw";
    const items = rawMode ? state.rawMessages : state.journalEvents;
    const list = el["journal-list"];
    const stickToNewest = list.scrollTop < 32;
    const previousHeight = list.scrollHeight;
    const previousTop = list.scrollTop;
    el["journal-count"].textContent = String(items.length);
    el["journal-hint"].textContent = rawMode
      ? "Кадры Mode-S с порта readsb 30002: тип DF, ICAO, высота/squawk, дальность если борт уже декодирован. Время — UTC."
      : "Изменения декодированных данных бортов. Время записей — UTC.";
    if (!items.length) {
      list.replaceChildren(
        emptyNode(
          rawMode
            ? "Ожидание сырых Mode-S сообщений…"
            : "Ожидание декодированных сообщений…",
        ),
      );
      return;
    }
    const fragment = document.createDocumentFragment();
    [...items].reverse().forEach((event) => {
      const entry = document.createElement("article");
      entry.className = "journal-entry";
      const timestamp = document.createElement("time");
      const date = new Date(event.timestamp);
      const valid = !Number.isNaN(date.getTime());
      if (valid) timestamp.dateTime = date.toISOString();
      timestamp.textContent = valid ? `${formatUtcTime(date)} UTC` : "—";

      if (rawMode) {
        entry.classList.add("is-raw");
        const label = document.createElement("strong");
        const icao = text(event.icao, "").toUpperCase();
        const callsign = text(event.callsign, "");
        label.textContent = [icao || "MODE-S", callsign].filter(Boolean).join(" ");
        const details = document.createElement("p");
        details.textContent = text(event.text, text(event.df_label, "Mode-S"));
        const code = document.createElement("code");
        code.textContent = text(event.raw, "—");
        entry.append(timestamp, label, details, code);
        fragment.append(entry);
        return;
      }

      entry.dataset.kind = text(event.kind, "update");
      const identity = document.createElement("strong");
      identity.textContent = `${text(event.icao, "------").toUpperCase()} ${text(event.callsign, "")}`.trim();

      const message = document.createElement("p");
      message.textContent = text(event.text, "Декодированное обновление");
      entry.append(timestamp, identity, message);
      fragment.append(entry);
    });
    list.replaceChildren(fragment);
    if (stickToNewest) list.scrollTop = 0;
    else list.scrollTop = previousTop + (list.scrollHeight - previousHeight);
  }

  async function loadLayers() {
    el["reload-layers"].disabled = true;
    try {
      const payload = await fetchJson("/api/layers");
      const layers = Array.isArray(payload) ? payload : (payload.layers || []);
      renderLayerList(layers);
    } catch (error) {
      console.error("Ошибка загрузки списка слоёв:", error);
      el["custom-layers"].replaceChildren(emptyNode("Не удалось загрузить слои", true));
    } finally {
      el["reload-layers"].disabled = false;
    }
  }

  function renderLayerList(layers) {
    if (!layers.length) {
      el["custom-layers"].replaceChildren(emptyNode("Нет доступных слоёв"));
      return;
    }
    const fragment = document.createDocumentFragment();
    layers.forEach((layerInfo) => {
      const id = text(layerInfo.id, "");
      if (!id) return;
      const label = document.createElement("label");
      label.className = "option";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = state.customLayers.has(id);
      const name = document.createElement("span");
      name.textContent = text(layerInfo.name ?? layerInfo.title, id);
      input.addEventListener("change", () => toggleCustomLayer(id, input, layerInfo));
      label.append(input, name);
      fragment.append(label);
    });
    el["custom-layers"].replaceChildren(fragment);
  }

  async function toggleCustomLayer(id, input, layerInfo) {
    if (!input.checked) {
      const layer = state.customLayers.get(id);
      if (layer) state.map.removeLayer(layer);
      state.customLayers.delete(id);
      return;
    }

    input.disabled = true;
    try {
      if (layerInfo.kind === "mbtiles") {
        let layer;
        if (layerInfo.format === "pbf") {
          if (!L.vectorGrid?.protobuf) throw new Error("Leaflet.VectorGrid не загружен");
          const vectorStyle = {
            color: "#ffc857",
            weight: 1.5,
            fillColor: "#ffc857",
            fillOpacity: .12,
          };
          const sourceLayers = Array.isArray(layerInfo.vector_layers)
            ? layerInfo.vector_layers
            : [];
          layer = L.vectorGrid.protobuf(layerInfo.tile_url, {
            vectorTileLayerStyles: Object.fromEntries(
              sourceLayers.map((name) => [name, vectorStyle]),
            ),
            interactive: true,
          });
        } else {
          layer = L.tileLayer(layerInfo.tile_url, {
            maxZoom: finite(layerInfo.maxzoom) ?? 22,
            opacity: .85,
          });
        }
        layer.addTo(state.map);
        state.customLayers.set(id, layer);
        return;
      }
      const payload = await fetchJson(`/api/layers/${encodeURIComponent(id)}`);
      const geojson = payload.geojson || payload.data || payload;
      const layer = L.geoJSON(geojson, {
        style: (feature) => {
          const color = feature?.properties?.color || layerInfo.color || "#ffc857";
          return {
            color,
            weight: finite(layerInfo.weight) ?? 2,
            opacity: finite(layerInfo.opacity) ?? .85,
            fillColor: color,
            fillOpacity: finite(layerInfo.fill_opacity) ?? .15,
          };
        },
        pointToLayer: (feature, latlng) => L.circleMarker(latlng, {
          radius: 5,
          color: feature?.properties?.color || layerInfo.color || "#ffc857",
          fillOpacity: .65,
        }),
        onEachFeature: (feature, featureLayer) => {
          const name = feature?.properties?.name ?? feature?.properties?.title;
          const code = feature?.properties?.code;
          const title = code && name && String(name) !== String(code)
            ? `${name} (${code})`
            : (code || name);
          if (title !== undefined && title !== null && title !== "") {
            featureLayer.bindTooltip(String(title));
          }
        },
      }).addTo(state.map);
      state.customLayers.set(id, layer);
    } catch (error) {
      console.error(`Ошибка загрузки слоя ${id}:`, error);
      input.checked = false;
    } finally {
      input.disabled = false;
    }
  }

  async function loadRadioChannels() {
    el["reload-radio"].disabled = true;
    try {
      const payload = await fetchJson("/api/radio/channels");
      const channels = Array.isArray(payload) ? payload : (payload.channels || []);
      setRadioAvailable(channels.length > 0);
      renderRadioChannels(channels);
    } catch (error) {
      console.error("Ошибка загрузки VHF-каналов:", error);
      el["radio-list"].replaceChildren(emptyNode("Не удалось загрузить каналы", true));
    } finally {
      el["reload-radio"].disabled = false;
    }
  }

  function setRadioAvailable(available) {
    const radioTab = document.querySelector('.tab[data-tab="radio"]');
    if (radioTab) radioTab.hidden = !available;
    if (!available && radioTab?.classList.contains("is-active")) switchTab("aircraft");
  }

  function renderRadioChannels(channels) {
    if (!channels.length) {
      el["radio-list"].replaceChildren(emptyNode("Нет доступных VHF-каналов"));
      return;
    }
    const fragment = document.createDocumentFragment();
    channels.forEach((channel) => {
      const row = document.createElement("div");
      row.className = "radio-channel";
      row.classList.toggle("is-active", Boolean(channel.active ?? channel.activity));
      const activity = document.createElement("span");
      activity.className = "radio-channel__activity";
      activity.title = (channel.active ?? channel.activity) ? "Есть активность" : "Нет активности";
      const info = document.createElement("span");
      info.className = "radio-channel__info";
      const name = document.createElement("strong");
      name.textContent = text(channel.name ?? channel.label, "Канал");
      const frequency = document.createElement("small");
      const frequencyValue = channel.frequency_mhz ?? channel.frequency;
      frequency.textContent = frequencyValue === undefined ? "Частота не указана" : `${frequencyValue} МГц`;
      info.append(name, frequency);
      const play = document.createElement("button");
      play.type = "button";
      play.className = "radio-channel__play";
      play.textContent = "▶";
      play.title = "Слушать канал";
      const streamUrl = channel.stream_url ?? channel.url;
      play.disabled = !streamUrl;
      play.addEventListener("click", () => playRadioChannel(channel, row));
      row.append(activity, info, play);
      fragment.append(row);
    });
    el["radio-list"].replaceChildren(fragment);
  }

  async function playRadioChannel(channel, row) {
    const streamUrl = channel.stream_url ?? channel.url;
    if (!streamUrl) return;
    document.querySelectorAll(".radio-channel").forEach((item) => item.classList.remove("is-playing"));
    row.classList.add("is-playing");
    el["now-playing"].textContent = text(channel.name ?? channel.label, "VHF-канал");
    if (el["radio-audio"].src !== new URL(streamUrl, location.href).href) {
      el["radio-audio"].src = streamUrl;
    }
    try {
      await el["radio-audio"].play();
    } catch (error) {
      console.warn("Браузер не начал воспроизведение потока:", error);
    }
  }

  function emptyNode(message, isError = false) {
    const node = document.createElement("p");
    node.className = `empty-state${isError ? " error-state" : ""}`;
    node.textContent = message;
    return node;
  }

  async function fetchJson(url, options = {}) {
    const response = await fetch(url, {
      headers: { Accept: "application/json" },
      cache: "no-store",
      ...options,
    });
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    const contentType = response.headers.get("content-type") || "";
    if (!contentType.includes("application/json")) return {};
    return response.json();
  }
})();
