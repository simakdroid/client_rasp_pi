(() => {
  "use strict";

  /*
   * Протокол /ws/aircraft:
   *   snapshot {generation, seq, aircraft, archived} — полная замена состояния;
   *   delta {generation, seq, upsert, remove, archive, archive_remove};
    *   resync — переподключить сокет, не смешивать REST со живыми дельтами;
   *   heartbeat — keepalive.
   * Дельты принимаются только после snapshot (состояние LIVE).
   * Дельты с seq <= текущего игнорируются; разрыв seq или смена generation
   * вызывает повторный snapshot через новое WebSocket-соединение.
   * upsert.track_append дополняет трек; точки с тем же timestamp не дублируются.
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
    wsPingTimer: null,
    wsWatchdogTimer: null,
    lastWsMessageAt: 0,
    tracksVisible: true,
    surfaceVisible: false,
    mapUserMoved: false,
    autoFitting: false,
    selectedIcao: null,
    search: "",
    journalMode: "decoded",
    journalEvents: [],
    lastEventId: 0,
    eventGeneration: null,
    geofenceEvents: [],
    lastGeofenceId: 0,
    geofenceGeneration: null,
    geofenceJournalEpoch: 0,
    rawMessages: [],
    lastRawId: 0,
    rawGeneration: null,
    typeCatalog: new Map(),
    typeCatalogVersion: -1,
    typeCatalogError: "",
    decodedJournalEpoch: 0,
    rawJournalEpoch: 0,
    journalTruncated: false,
    acarsMessages: [],
    lastAcarsId: 0,
    acarsGeneration: null,
    acarsEpoch: 0,
    acarsError: "",
    acarsTruncated: false,
    journalError: "",
    stripRows: new Map(),
    stripMode: "live",
    aircraftRenderFrame: 0,
    layerCatalogVersion: -1,
    syncGeneration: null,
    syncSeq: 0,
    syncMode: "syncing",
    resyncing: false,
  };

  const el = {};
  const byId = (id) => document.getElementById(id);
  const ADMIN_TOKEN_KEY = "airmon-admin-token";
  const finite = (value) => {
    if (value === null || value === undefined || value === "") return null;
    if (typeof value === "boolean" || typeof value === "object") return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  };
  const inRange = (value, min, max) => {
    const number = finite(value);
    return number !== null && number >= min && number <= max ? number : null;
  };
  const latNum = (value) => inRange(value, -90, 90);
  const lonNum = (value) => inRange(value, -180, 180);
  const plainTooltip = (value) => {
    const node = document.createElement("span");
    node.textContent = String(value);
    return node;
  };
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
    createMap();
    connectAircraftSocket();
    document.addEventListener("visibilitychange", resumeSocketIfVisible);
    window.setTimeout(() => {
      if (state.syncMode !== "live") void loadInitialAircraft();
    }, 2500);
    const pollAcars = startPolling(loadAcarsMessages, 1000);
    const pollTypes = startPolling(loadTypeCatalog, 5000);
    const pollJournal = startPolling(loadJournal, 1000);
    const pollHealth = startPolling(refreshHealth, 5000);
    const pollLayers = startPolling(loadLayers, 15000);
    const pollStation = startPolling(loadStationStatus, 5000);
    void pollLayers();
    void pollStation();
    void pollAcars();
    void pollTypes();
    void pollJournal();
    void pollHealth();
    el["reload-acars"].addEventListener("click", () => void pollAcars());
    el["clear-acars"].addEventListener("click", () => void clearAcarsMessages());
  }

  function startPolling(task, ms) {
    let timer = 0;
    let inflight = false;
    const run = async () => {
      if (inflight) return;
      inflight = true;
      window.clearTimeout(timer);
      try {
        await task();
      } finally {
        inflight = false;
        timer = window.setTimeout(() => { void run(); }, ms);
      }
    };
    return run;
  }

  function cacheElements() {
    [
      "station-name", "connection", "connection-text", "clock", "clock-date", "clock-time",
      "visible-count", "aircraft-search", "aircraft-strip-body", "strip-empty",
      "strip-time-heading",
      "custom-layers",
      "ofm-option", "fit-aircraft", "toggle-tracks", "toggle-surface",
      "reload-layers", "gis-diagnostics", "reload-acars", "acars-hint", "acars-quality",
      "acars-list", "acars-count", "clear-acars",
      "journal-list", "journal-count", "journal-hint", "clear-journal",
      "type-catalog-count", "type-catalog-form", "type-catalog-icao",
      "type-catalog-type", "type-catalog-desc", "type-catalog-list", "type-catalog-hint",
      "station-ready", "station-hint", "station-status", "session-status",
      "session-start", "session-stop", "session-download", "session-replay",
      "session-upload", "session-file",
      "diagnostics-download",
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
    document.querySelectorAll("[data-strip-mode]").forEach((button) => {
      button.addEventListener("click", () => {
        state.stripMode = button.dataset.stripMode === "archive" ? "archive" : "live";
        document.querySelectorAll("[data-strip-mode]").forEach((item) => {
          item.classList.toggle("is-active", item === button);
        });
        if (el["strip-time-heading"]) {
          el["strip-time-heading"].textContent = state.stripMode === "archive"
            ? "Потеря"
            : "Начало";
        }
        state.stripRows.clear();
        el["aircraft-strip-body"].replaceChildren();
        renderAircraftList();
      });
    });
    el["fit-aircraft"].addEventListener("click", fitAircraft);
    el["toggle-tracks"].addEventListener("click", toggleTracks);
    el["toggle-surface"].addEventListener("click", toggleSurfaceVehicles);
    el["reload-layers"].addEventListener("click", () => void loadLayers());
    el["clear-journal"].addEventListener("click", clearJournal);
    el["type-catalog-form"].addEventListener("submit", submitTypeCatalog);
    el["type-catalog-icao"].addEventListener("input", () => {
      el["type-catalog-icao"].value = el["type-catalog-icao"].value.toUpperCase();
    });
    el["type-catalog-type"].addEventListener("input", () => {
      el["type-catalog-type"].value = el["type-catalog-type"].value.toUpperCase();
    });
    el["type-catalog-list"].addEventListener("click", (event) => {
      const button = event.target.closest("[data-remove-icao]");
      if (button) void removeTypeCatalogEntry(button.dataset.removeIcao);
    });
    el["session-start"].addEventListener("click", () => void controlSession("start"));
    el["session-stop"].addEventListener("click", () => void controlSession("stop"));
    el["session-download"].addEventListener("click", () => void downloadSession());
    el["session-replay"].addEventListener("click", () => void replaySession());
    el["session-upload"].addEventListener("click", () => el["session-file"].click());
    el["session-file"].addEventListener("change", () => void uploadSessionFile());
    el["diagnostics-download"].addEventListener("click", () => void downloadDiagnostics());
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
    const lat = latNum(station.lat ?? state.config.station_lat ?? mapConfig.center?.[0]);
    const lon = lonNum(station.lon ?? state.config.station_lon ?? mapConfig.center?.[1]);
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
    state.map.on("dragstart", () => {
      if (!state.autoFitting) state.mapUserMoved = true;
    });
    state.map.getContainer().addEventListener("focusin", () => {
      window.scrollTo(0, 0);
    });
    const stripBoard = byId("strip-board");
    if (stripBoard) {
      L.DomEvent.disableClickPropagation(stripBoard);
      L.DomEvent.disableScrollPropagation(stripBoard);
    }
    if (state.station) {
      L.circleMarker([state.station.lat, state.station.lon], {
        radius: 6,
        color: "#36b7ff",
        weight: 2,
        fillColor: "#36b7ff",
        fillOpacity: .35,
        interactive: false,
      }).addTo(state.map).bindTooltip(plainTooltip(el["station-name"].textContent), {
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
    if (state.syncMode === "live") return;
    const socketState = state.socket?.readyState;
    if (socketState === WebSocket.OPEN || socketState === WebSocket.CONNECTING) return;
    try {
      const payload = await fetchJson("/api/aircraft");
      if (state.syncMode === "live") return;
      applySnapshot(payload);
    } catch (error) {
      console.error("Ошибка начальной загрузки бортов:", error);
      if (state.syncMode !== "live") {
        setConnection("offline", "Начальные данные недоступны");
      }
    }
  }

  function replaceAircraft(items, archived = []) {
    [...state.aircraft.keys()].forEach((icao) => removeAircraft(icao, false));
    (Array.isArray(items) ? items : []).forEach((aircraft) => {
      upsertAircraft(aircraft, false);
    });
    replaceArchive(Array.isArray(archived) ? archived : []);
    finishAircraftUpdate();
  }

  function applySnapshot(message) {
    if (!message || typeof message !== "object") return false;
    const generation = text(message.generation, "");
    const seq = finite(message.seq) ?? 0;
    if (
      state.syncGeneration
      && generation
      && generation === state.syncGeneration
      && seq < state.syncSeq
    ) {
      return false;
    }
    if (
      state.syncMode === "live"
      && generation
      && generation === state.syncGeneration
      && seq === state.syncSeq
    ) {
      return false;
    }
    replaceAircraft(message.aircraft || [], message.archived || []);
    if (generation) state.syncGeneration = generation;
    state.syncSeq = seq;
    state.syncMode = "live";
    state.resyncing = false;
    setConnection("online", "В реальном времени");
    return true;
  }

  function trackLimit() {
    return finite(state.config.track_max_points) ?? 300;
  }

  function mergeTrackPoints(baseTrack, appends) {
    const track = Array.isArray(baseTrack) ? [...baseTrack] : [];
    const seen = new Set(
      track.map((point) => (Array.isArray(point) ? point[3] : point?.timestamp)).filter(Boolean),
    );
    (Array.isArray(appends) ? appends : []).forEach((point) => {
      const stamp = Array.isArray(point) ? point[3] : point?.timestamp;
      if (stamp && seen.has(stamp)) return;
      if (stamp) seen.add(stamp);
      track.push(point);
    });
    return track.slice(-trackLimit());
  }

  function fieldPresent(object, names) {
    return names.some((name) => Object.prototype.hasOwnProperty.call(object, name));
  }

  function archiveKey(aircraft) {
    return text(aircraft.contact_id, "") || normalizeIcao(aircraft);
  }

  function selectionKey(aircraft, archived = false) {
    return archived ? archiveKey(aircraft) : normalizeIcao(aircraft);
  }

  function replaceArchive(items) {
    const incoming = new Set();
    items.forEach((aircraft) => {
      const key = archiveKey(aircraft);
      if (!key) return;
      incoming.add(key);
      archiveAircraft(aircraft, false);
    });
    [...state.archived.keys()].forEach((key) => {
      if (!incoming.has(key)) dropArchive(key, false);
    });
  }

  function upsertAircraft(aircraft, render = true) {
    const icao = normalizeIcao(aircraft);
    if (!icao) return;

    const previous = state.aircraft.get(icao) || {};
    const incomingId = text(aircraft.contact_id, "");
    const previousId = text(previous.contact_id, "");
    const resetContact = Boolean(incomingId && previousId && incomingId !== previousId);
    const incomingRev = finite(aircraft.revision);
    const previousRev = finite(previous.revision);
    if (!resetContact && incomingRev !== null && previousRev !== null && incomingRev < previousRev) {
      return;
    }
    const base = resetContact ? {} : previous;
    const incomingLat = latNum(aircraft.lat ?? aircraft.latitude);
    const incomingLon = lonNum(aircraft.lon ?? aircraft.lng ?? aircraft.longitude);
    const merged = { ...base, ...aircraft, icao };
    if (!fieldPresent(aircraft, ["lat", "latitude"])) {
      merged.lat = latNum(base.lat ?? base.latitude);
    } else {
      merged.lat = incomingLat;
    }
    if (!fieldPresent(aircraft, ["lon", "lng", "longitude"])) {
      merged.lon = lonNum(base.lon ?? base.lng ?? base.longitude);
    } else {
      merged.lon = incomingLon;
    }
    if (Array.isArray(aircraft.track) || Array.isArray(aircraft.track_append)) {
      const baseTrack = Array.isArray(aircraft.track)
        ? aircraft.track
        : (resetContact ? [] : base.track);
      merged.track = mergeTrackPoints(baseTrack, aircraft.track_append);
    } else if (resetContact) {
      merged.track = [];
    }
    state.aircraft.set(icao, merged);

    const lat = latNum(merged.lat ?? merged.latitude);
    const lon = lonNum(merged.lon ?? merged.lng ?? merged.longitude);
    if (lat !== null && lon !== null) {
      updateMarker(merged, lat, lon);
      updateTrack(merged);
    } else {
      removeMapObjects(icao);
    }
    if (render) finishAircraftUpdate();
  }

  function removeAircraft(icaoValue, render = true) {
    const icao = text(icaoValue, "").toUpperCase();
    state.aircraft.delete(icao);
    removeMapObjects(icao);
    if (state.selectedIcao === icao) state.selectedIcao = null;
    if (render) finishAircraftUpdate();
  }

  function archiveAircraft(aircraft, render = true) {
    const icao = normalizeIcao(aircraft);
    const key = archiveKey(aircraft);
    if (!icao || !key) return;
    const live = state.aircraft.get(icao);
    if (live && (text(live.contact_id, "") === key || !live.contact_id)) {
      state.aircraft.delete(icao);
      removeMapObjects(icao);
      if (state.selectedIcao === icao) state.selectedIcao = null;
    }
    const merged = { ...(state.archived.get(key) || {}), ...aircraft, icao, status: "archived" };
    state.archived.set(key, merged);
    removeArchiveMarker(key);
    if (render) finishAircraftUpdate();
  }

  function dropArchive(keyValue, render = true) {
    const key = text(keyValue, "");
    if (!key) return;
    state.archived.delete(key);
    removeArchiveMarker(key);
    if (state.selectedIcao === key) state.selectedIcao = null;
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

  function removeArchiveMarker(key) {
    const marker = state.archiveMarkers.get(key);
    if (marker) state.map.removeLayer(marker);
    state.archiveMarkers.delete(key);
  }

  function finishAircraftUpdate() {
    if (state.aircraftRenderFrame) return;
    state.aircraftRenderFrame = window.requestAnimationFrame(() => {
      state.aircraftRenderFrame = 0;
      renderAircraftList();
      revealAircraftOnMap();
    });
  }

  function updateMarker(aircraft, lat, lon) {
    const icao = aircraft.icao;
    if (!shouldShowContact(aircraft)) {
      removeMapObjects(icao);
      return;
    }
    const altitude = aircraftAltitude(aircraft);
    const color = altitudeColor(altitude);
    const rotation = aircraftHeading(aircraft) ?? 0;
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
      marker._airmonStamp = "";
    } else {
      const stamp = `${lat.toFixed(5)}|${lon.toFixed(5)}|${color}|${rotation}|${text(aircraft.callsign, "")}|${text(aircraft.sector, "")}|${aircraftTypeCode(aircraft)}|${aircraft.revision || ""}`;
      if (marker._airmonStamp !== stamp) {
        marker.setLatLng([lat, lon]);
        marker.setIcon(icon);
        marker.setTooltipContent(createDataBlock(aircraft));
        marker.setPopupContent(createTooltip(aircraft));
        marker.setZIndexOffset(Math.round(altitude || 0));
        marker._airmonStamp = stamp;
      }
      marker.getTooltip()?.getElement()?.classList.toggle(
        "is-selected",
        icao === state.selectedIcao,
      );
    }
  }

  function dataBlockOptions(aircraft, archived = false) {
    return {
      permanent: true,
      direction: "right",
      offset: [18, 0],
      className: `aircraft-label${selectionKey(aircraft, archived) === state.selectedIcao ? " is-selected" : ""}${
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
    root.append(callsign);

    const typeLabel = formatAircraftType(aircraft, true);
    if (typeLabel) {
      const typeLine = document.createElement("div");
      typeLine.className = "aircraft-label__type";
      typeLine.textContent = typeLabel;
      root.append(typeLine);
    }

    const verticalRate = finite(
      aircraft.vertical_rate_fpm ?? aircraft.baro_rate ?? aircraft.vert_rate,
    );
    const trend = verticalRate !== null && Math.abs(verticalRate) >= 200
      ? (verticalRate > 0 ? " ↑" : " ↓")
      : "";
    const speed = aircraftSpeed(aircraft);
    const motion = document.createElement("div");
    motion.className = "aircraft-label__line";
    motion.textContent = `${aircraft.on_ground ? "GND" : formatBlockAltitude(aircraft)}${trend}${
      speed === null ? "" : ` ${String(Math.round(speed)).padStart(3, "0")}`
    }`;
    root.append(motion);

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
      ["Тип ВС", formatAircraftType(aircraft) || "не определён"],
      ["Высота", formatAltitude(aircraftAltitude(aircraft), aircraft.altitude_reference)],
      ["Скорость", formatSpeed(aircraftSpeed(aircraft))],
      ["Код ответчика", text(aircraft.squawk)],
      ["Положение", formatPosition(aircraft)],
    ];
    if (text(aircraft.sector, "").trim()) {
      rows.push(["Сектор", aircraft.sector]);
    }
    rows.push(["Начало контакта", formatContactTime(contactStartedAt(aircraft))]);
    if (aircraft.lost_at) {
      rows.push(["Потеря контакта", formatContactTime(aircraft.lost_at)]);
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
    if (!shouldShowContact(aircraft)) {
      const hidden = state.tracks.get(aircraft.icao);
      if (hidden) state.map.removeLayer(hidden);
      state.tracks.delete(aircraft.icao);
      return;
    }
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
      if (Array.isArray(point)) return [latNum(point[0]), lonNum(point[1])];
      return [latNum(point?.lat ?? point?.latitude), lonNum(point?.lon ?? point?.lng ?? point?.longitude)];
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

  function toggleSurfaceVehicles() {
    state.surfaceVisible = !state.surfaceVisible;
    el["toggle-surface"].classList.toggle("is-active", state.surfaceVisible);
    el["toggle-surface"].setAttribute("aria-pressed", String(state.surfaceVisible));
    if (!state.surfaceVisible && state.selectedIcao) {
      const selected = state.aircraft.get(state.selectedIcao);
      if (selected && isSurfaceVehicle(selected)) state.selectedIcao = null;
    }
    state.aircraft.forEach((aircraft) => {
      const lat = latNum(aircraft.lat ?? aircraft.latitude);
      const lon = lonNum(aircraft.lon ?? aircraft.lng ?? aircraft.longitude);
      if (lat !== null && lon !== null) {
        updateMarker(aircraft, lat, lon);
      } else {
        removeMapObjects(aircraft.icao);
      }
      updateTrack(aircraft);
    });
    renderAircraftList();
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
    if (!live) return;
    const lat = latNum(live.lat ?? live.latitude);
    const lon = lonNum(live.lon ?? live.lng ?? live.longitude);
    if (lat !== null && lon !== null) updateMarker(live, lat, lon);
  }

  function matchesSearch(aircraft) {
    const haystack = [
      aircraft.icao, text(aircraft.callsign, ""), text(aircraft.squawk, ""),
      aircraftTypeCode(aircraft), text(aircraft.type_desc, ""),
    ].join(" ").toUpperCase();
    return !state.search || haystack.includes(state.search);
  }

  const SURFACE_VEHICLE_CATEGORIES = new Set(["C0", "C1", "C2"]);
  const SURFACE_VEHICLE_TYPE = "NULL";

  function isSurfaceVehicle(aircraft) {
    if (SURFACE_VEHICLE_CATEGORIES.has(text(aircraft.category, "").toUpperCase())) return true;
    return aircraftTypeCode(aircraft) === SURFACE_VEHICLE_TYPE;
  }

  function shouldShowContact(aircraft) {
    return state.surfaceVisible || !isSurfaceVehicle(aircraft);
  }

  const LIST_LIMIT = 250;

  function stripCell(className, value) {
    const cell = document.createElement("td");
    if (className) cell.className = className;
    cell.textContent = value;
    return cell;
  }

  function fillStripRow(aircraft, archived = false) {
    const row = document.createElement("tr");
    row.addEventListener("click", () => {
      selectAircraft(selectionKey(aircraftFromStrip(row), archived));
    });
    updateStripRow(row, aircraft, archived);
    return row;
  }

  function aircraftFromStrip(row) {
    if (row.dataset.key && state.archived.has(row.dataset.key)) {
      return state.archived.get(row.dataset.key);
    }
    return state.aircraft.get(row.dataset.icao) || { icao: row.dataset.icao };
  }

  function formatStripAltitude(aircraft) {
    if (aircraft.on_ground) return "земля";
    const altitude = aircraftAltitude(aircraft);
    if (altitude === null) return "—";
    const feet = `${Math.round(altitude).toLocaleString("ru-RU")} ft`;
    const source = altitudeReferenceLabel(aircraft.altitude_reference);
    return source ? `${feet} ${source}` : feet;
  }

  function updateStripRow(row, aircraft, archived = false) {
    const key = archived ? archiveKey(aircraft) : aircraft.icao;
    row.dataset.icao = aircraft.icao;
    if (archived) row.dataset.key = key;
    row.classList.toggle("is-selected", state.selectedIcao === selectionKey(aircraft, archived));
    row.classList.toggle("is-archived", archived);
    row.style.setProperty("--aircraft-color", altitudeColor(aircraftAltitude(aircraft)));
    const cells = [
      ["", formatUtcTime(archived ? aircraft.lost_at : contactStartedAt(aircraft))],
      ["strip-lit", text(aircraft.callsign, "без позывного")],
      ["strip-icao", text(aircraft.icao, "").toUpperCase()],
      ["", formatAircraftType(aircraft, true) || "—"],
      ["strip-code", text(aircraft.squawk, "").trim() || "—"],
      ["", formatStripAltitude(aircraft)],
      ["", formatSpeed(aircraftSpeed(aircraft))],
      ["", formatPosition(aircraft)],
      ["", text(aircraft.sector, "").trim() || "—"],
    ];
    if (row.children.length !== cells.length) {
      row.replaceChildren(...cells.map(([className, value]) => stripCell(className, value)));
      return;
    }
    cells.forEach(([className, value], index) => {
      const cell = row.children[index];
      cell.className = className;
      cell.textContent = value;
    });
  }

  function syncStripList(items, archived) {
    const body = el["aircraft-strip-body"];
    const empty = el["strip-empty"];
    if (!items.length) {
      state.stripRows.clear();
      body.replaceChildren();
      empty.hidden = false;
      empty.textContent = state.search
        ? "Ничего не найдено"
        : (archived ? "Архив пуст" : "Нет активных бортов");
      return;
    }
    empty.hidden = true;
    const seen = new Set();
    items.forEach((aircraft, index) => {
      const key = archived ? archiveKey(aircraft) : aircraft.icao;
      seen.add(key);
      let row = state.stripRows.get(key);
      if (!row) {
        row = fillStripRow(aircraft, archived);
        state.stripRows.set(key, row);
      } else {
        updateStripRow(row, aircraft, archived);
      }
      const current = body.children[index];
      if (current !== row) body.insertBefore(row, current || null);
    });
    [...state.stripRows.keys()].forEach((key) => {
      if (seen.has(key)) return;
      state.stripRows.get(key)?.remove();
      state.stripRows.delete(key);
    });
  }

  function renderAircraftList() {
    const liveItems = [...state.aircraft.values()]
      .filter(shouldShowContact)
      .filter(matchesSearch)
      .sort((a, b) => {
        const da = aircraftDistance(a);
        const db = aircraftDistance(b);
        return (da ?? Infinity) - (db ?? Infinity) ||
          text(a.callsign, a.icao).localeCompare(text(b.callsign, b.icao));
      });
    const archiveItems = [...state.archived.values()]
      .filter(shouldShowContact)
      .filter(matchesSearch)
      .sort((a, b) => text(b.lost_at, "").localeCompare(text(a.lost_at, "")));
    const archived = state.stripMode === "archive";
    const items = archived ? archiveItems : liveItems;
    el["visible-count"].textContent = String(items.length);
    syncStripList(items.slice(0, LIST_LIMIT), archived);
  }

  async function loadTypeCatalog() {
    const previous = typeCatalogSignature();
    try {
      const payload = await fetchJson("/api/aircraft-types");
      state.typeCatalog = new Map(
        (payload.types || []).map((entry) => [text(entry.icao, "").toUpperCase(), entry]),
      );
      state.typeCatalogVersion = finite(payload.version) ?? state.typeCatalogVersion;
      setTypeCatalogStatus("");
    } catch (error) {
      setTypeCatalogStatus(`Не удалось загрузить справочник типов: ${error.message}`);
    }
    if (typeCatalogSignature() === previous) return;
    renderTypeCatalog();
    renderAircraftList();
    refreshAircraftMarkers();
  }

  function setTypeCatalogStatus(message) {
    state.typeCatalogError = message || "";
    if (!el["type-catalog-hint"]) return;
    el["type-catalog-hint"].textContent = state.typeCatalogError ||
      "Если ADS-B не дал тип, подставляется запись по 24-битному адресу. Хранится в JSON, без базы.";
    el["type-catalog-hint"].classList.toggle("error-state", Boolean(state.typeCatalogError));
  }

  function refreshAircraftMarkers() {
    state.aircraft.forEach((aircraft) => {
      const lat = latNum(aircraft.lat ?? aircraft.latitude);
      const lon = lonNum(aircraft.lon ?? aircraft.lng ?? aircraft.longitude);
      if (lat !== null && lon !== null) updateMarker(aircraft, lat, lon);
    });
  }

  function typeCatalogSignature() {
    return JSON.stringify(
      [...state.typeCatalog.entries()]
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([icao, entry]) => [icao, entry.type_code, entry.type_desc || ""]),
    );
  }

  function renderTypeCatalog() {
    const items = [...state.typeCatalog.values()].sort((a, b) =>
      text(a.icao, "").localeCompare(text(b.icao, "")),
    );
    el["type-catalog-count"].textContent = String(items.length);
    if (!items.length) {
      el["type-catalog-list"].replaceChildren(emptyNode("Нет ручных типов"));
      return;
    }
    const fragment = document.createDocumentFragment();
    items.forEach((entry) => {
      const row = document.createElement("div");
      row.className = "type-catalog__item";
      const icao = document.createElement("code");
      icao.textContent = text(entry.icao, "").toUpperCase();
      const type = document.createElement("strong");
      const desc = text(entry.type_desc, "");
      type.textContent = desc ? `${entry.type_code} · ${desc}` : entry.type_code;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.dataset.removeIcao = text(entry.icao, "").toUpperCase();
      remove.textContent = "Удалить";
      row.append(icao, type, remove);
      fragment.append(row);
    });
    el["type-catalog-list"].replaceChildren(fragment);
  }

  async function submitTypeCatalog(event) {
    event.preventDefault();
    const icao = el["type-catalog-icao"].value.trim().toUpperCase();
    const typeCode = el["type-catalog-type"].value.trim().toUpperCase();
    const typeDesc = el["type-catalog-desc"].value.trim();
    try {
      const entry = await fetchJson("/api/aircraft-types", {
        method: "POST",
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify({
          icao,
          type_code: typeCode,
          type_desc: typeDesc || null,
        }),
      });
      state.typeCatalog.set(text(entry.icao, icao).toUpperCase(), entry);
      el["type-catalog-form"].reset();
      el["type-catalog-icao"].focus();
      setTypeCatalogStatus("");
      renderTypeCatalog();
      renderAircraftList();
      refreshAircraftMarkers();
    } catch (error) {
      setTypeCatalogStatus(`Не удалось сохранить тип ВС: ${error.message}`);
    }
  }

  async function removeTypeCatalogEntry(icao) {
    const key = text(icao, "").toUpperCase();
    if (!key) return;
    try {
      await fetchJson(`/api/aircraft-types/${encodeURIComponent(key)}`, {
        method: "DELETE",
      });
      state.typeCatalog.delete(key);
      setTypeCatalogStatus("");
      renderTypeCatalog();
      renderAircraftList();
      refreshAircraftMarkers();
    } catch (error) {
      setTypeCatalogStatus(`Не удалось удалить тип ВС: ${error.message}`);
    }
  }

  function aircraftAltitude(aircraft) {
    return finite(
      aircraft.altitude_ft ?? aircraft.altitude ?? aircraft.alt_baro ?? aircraft.alt_geom,
    );
  }

  function aircraftSpeed(aircraft) {
    return finite(aircraft.speed_kt ?? aircraft.speed ?? aircraft.ground_speed ?? aircraft.gs);
  }

  function aircraftHeading(aircraft) {
    return finite(
      aircraft.heading_deg
      ?? aircraft.calculated_track_deg
      ?? aircraft.track_deg
      ?? aircraft.true_heading_deg
      ?? aircraft.heading,
    );
  }

  function aircraftDistance(aircraft) {
    const explicit = finite(aircraft.distance ?? aircraft.distance_km);
    if (explicit !== null) return explicit;
    const lat = latNum(aircraft.lat ?? aircraft.latitude);
    const lon = lonNum(aircraft.lon ?? aircraft.lng ?? aircraft.longitude);
    if (!state.station || lat === null || lon === null) return null;
    return haversineKm(state.station.lat, state.station.lon, lat, lon);
  }

  function aircraftAzimuth(aircraft) {
    const explicit = finite(aircraft.azimuth_deg ?? aircraft.azimuth);
    if (explicit !== null) return ((explicit % 360) + 360) % 360;
    const lat = latNum(aircraft.lat ?? aircraft.latitude);
    const lon = lonNum(aircraft.lon ?? aircraft.lng ?? aircraft.longitude);
    if (!state.station || lat === null || lon === null) return null;
    return initialBearingDeg(state.station.lat, state.station.lon, lat, lon);
  }

  const EMITTER_TYPES = new Set([
    "ADSB_ICAO", "ADSB_ICAO_NT", "ADSB_OTHER",
    "ADSR_ICAO", "ADSR_OTHER",
    "TISB_ICAO", "TISB_OTHER", "TISB_TRACKFILE",
    "MLAT", "MODE_S", "OTHER",
  ]);
  const CATEGORY_LABELS = {
    A0: "Нет категории",
    A1: "Лёгкое",
    A2: "Небольшое",
    A3: "Среднее",
    A4: "Крупное (B757)",
    A5: "Тяжёлое",
    A6: "Высокоскоростное",
    A7: "Вертолёт",
    B0: "Нет категории",
    B1: "Планер",
    B2: "Дирижабль",
    B3: "Парашютист",
    B4: "Сверхлёгкое",
    B6: "БПЛА",
    B7: "Космический аппарат",
    C0: "Наземный объект",
    C1: "Аэродромная спецтехника",
    C2: "Аэродромный транспорт",
    C3: "Препятствие",
  };

  function catalogType(aircraft) {
    return state.typeCatalog.get(normalizeIcao(aircraft)) || null;
  }

  function adsAircraftTypeCode(aircraft) {
    const code = text(aircraft.type_code ?? aircraft.t, "").toUpperCase();
    return code && !EMITTER_TYPES.has(code) ? code : "";
  }

  function aircraftTypeCode(aircraft) {
    const fromAds = adsAircraftTypeCode(aircraft);
    if (fromAds) return fromAds;
    return text(catalogType(aircraft)?.type_code, "").toUpperCase();
  }

  function formatAircraftType(aircraft, compact = false) {
    const fromAds = adsAircraftTypeCode(aircraft);
    const catalog = catalogType(aircraft);
    const code = fromAds || text(catalog?.type_code, "").toUpperCase();
    const desc = fromAds
      ? text(aircraft.type_desc ?? aircraft.desc, "")
      : text(catalog?.type_desc ?? aircraft.type_desc ?? aircraft.desc, "");
    if (code && desc && !compact) return `${code} · ${desc}`;
    if (code) return code;
    const category = text(aircraft.category, "").toUpperCase();
    if (!category) return "";
    const label = CATEGORY_LABELS[category];
    if (compact) return label || category;
    return label ? `${label} · ${category}` : category;
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

  const formatAltitude = (value, reference) => {
    if (value === null) return "—";
    const feet = `${Math.round(value).toLocaleString("ru-RU")} ft`;
    const source = altitudeReferenceLabel(reference);
    return source ? `${feet} (${source})` : feet;
  };
  const altitudeReferenceLabel = (reference) => {
    if (reference === "baro") return "баро";
    if (reference === "geom") return "геом";
    return "";
  };
  const formatSpeed = (value) => value === null ? "—" : `${Math.round(value)} kt`;
  const formatAzimuth = (value) => {
    if (value === null) return "";
    const deg = ((Math.round(value) % 360) + 360) % 360;
    return `${String(deg).padStart(3, "0")}°`;
  };
  const formatDistance = (value) => value === null ? "—" : `${value < 10 ? value.toFixed(1) : Math.round(value)} км`;
  function formatPosition(aircraft) {
    const lat = latNum(aircraft.lat ?? aircraft.latitude);
    const lon = lonNum(aircraft.lon ?? aircraft.lng ?? aircraft.longitude);
    if (lat === null || lon === null) return "нет координат";
    const parts = [];
    const azimuth = formatAzimuth(aircraftAzimuth(aircraft));
    const distance = aircraftDistance(aircraft);
    if (azimuth) parts.push(azimuth);
    if (distance !== null) parts.push(formatDistance(distance));
    return parts.join(" · ") || "нет координат";
  }
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
  const formatContactTime = (value) => {
    const time = formatUtcTime(value);
    return time === "—" ? "—" : `${time} UTC`;
  };
  function contactStartedAt(aircraft) {
    const explicit = aircraft.started_at ?? aircraft.first_seen ?? aircraft.detected_at;
    if (explicit) return explicit;
    const track = aircraft.track ?? aircraft.trail ?? aircraft.positions ?? aircraft.track_history;
    if (Array.isArray(track) && track.length) {
      const first = track[0];
      if (Array.isArray(first) && first[3]) return first[3];
      if (first?.timestamp) return first.timestamp;
    }
    return aircraft.updated_at || null;
  }
  const formatBlockAltitude = (aircraft) => {
    const value = aircraftAltitude(aircraft);
    if (value === null) return "---";
    const hundreds = String(Math.max(0, Math.round(value / 100))).padStart(3, "0");
    const mark = aircraft.altitude_reference === "geom"
      ? "г"
      : aircraft.altitude_reference === "baro" ? "б" : "";
    return mark ? `${hundreds}${mark}` : hundreds;
  };

  function haversineKm(lat1, lon1, lat2, lon2) {
    const rad = Math.PI / 180;
    const dLat = (lat2 - lat1) * rad;
    const dLon = (lon2 - lon1) * rad;
    const a = Math.sin(dLat / 2) ** 2 +
      Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(dLon / 2) ** 2;
    return 6371 * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  }

  function initialBearingDeg(lat1, lon1, lat2, lon2) {
    const rad = Math.PI / 180;
    const phi1 = lat1 * rad;
    const phi2 = lat2 * rad;
    const dLon = (lon2 - lon1) * rad;
    const y = Math.sin(dLon) * Math.cos(phi2);
    const x = Math.cos(phi1) * Math.sin(phi2) - Math.sin(phi1) * Math.cos(phi2) * Math.cos(dLon);
    return (Math.atan2(y, x) / rad + 360) % 360;
  }

  function wsHeartbeatMs() {
    const value = Number(state.config.websocket_heartbeat_ms);
    return Number.isFinite(value) && value >= 1000 ? value : 10000;
  }

  function stopSocketTimers() {
    clearInterval(state.wsPingTimer);
    clearInterval(state.wsWatchdogTimer);
    state.wsPingTimer = null;
    state.wsWatchdogTimer = null;
  }

  function startSocketTimers() {
    stopSocketTimers();
    const heartbeatMs = wsHeartbeatMs();
    state.lastWsMessageAt = Date.now();
    state.wsPingTimer = window.setInterval(() => {
      if (state.socket?.readyState === WebSocket.OPEN) {
        state.socket.send(JSON.stringify({ type: "ping" }));
      }
    }, heartbeatMs);
    state.wsWatchdogTimer = window.setInterval(() => {
      if (state.socket?.readyState !== WebSocket.OPEN) return;
      if (Date.now() - state.lastWsMessageAt > heartbeatMs * 3) {
        state.socket.close();
      }
    }, Math.min(5000, heartbeatMs));
  }

  function discardSocket() {
    stopSocketTimers();
    const socket = state.socket;
    state.socket = null;
    if (!socket) return;
    socket.removeEventListener("close", scheduleReconnect);
    if (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING) {
      socket.close();
    }
  }

  function resumeSocketIfVisible() {
    if (document.visibilityState !== "visible") return;
    if (state.socket?.readyState === WebSocket.OPEN) return;
    connectAircraftSocket();
  }

  function connectAircraftSocket() {
    clearTimeout(state.reconnectTimer);
    state.reconnectTimer = null;
    discardSocket();
    state.syncMode = "syncing";
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
      startSocketTimers();
      setConnection("connecting", "Синхронизация…");
      void refreshHealth();
    });
    state.socket.addEventListener("message", (event) => {
      state.lastWsMessageAt = Date.now();
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
    if (message?.type === "heartbeat" || message?.type === "pong") {
      return;
    }
    if (Array.isArray(message)) {
      replaceAircraft(message);
      return;
    }
    if (message.type === "snapshot") {
      applySnapshot(message);
      return;
    }
    if (message.type === "resync") {
      requestStreamResync();
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
      applyDelta(message);
    }
  }

  function applyDelta(message) {
    if (state.syncMode !== "live") return;
    const generation = text(message.generation, "");
    const seq = finite(message.seq);
    if (generation && state.syncGeneration && generation !== state.syncGeneration) {
      requestStreamResync();
      return;
    }
    if (seq != null && state.syncMode === "live") {
      if (seq <= state.syncSeq) return;
      if (seq > state.syncSeq + 1) {
        requestStreamResync();
        return;
      }
    }
    (message.upsert || []).forEach((aircraft) => upsertAircraft(aircraft, false));
    const archivedIcaos = new Set(
      (message.archive || []).map((aircraft) => normalizeIcao(aircraft)).filter(Boolean),
    );
    (message.archive_remove || []).forEach((key) => dropArchive(key, false));
    (message.archive || []).forEach((aircraft) => archiveAircraft(aircraft, false));
    (message.remove || []).forEach((icao) => {
      if (!archivedIcaos.has(text(icao, "").toUpperCase())) {
        removeAircraft(icao, false);
      }
    });
    if (seq != null) state.syncSeq = seq;
    if (generation) state.syncGeneration = generation;
    state.syncMode = "live";
    finishAircraftUpdate();
  }

  function requestStreamResync() {
    if (state.resyncing) return;
    state.resyncing = true;
    state.syncMode = "syncing";
    setConnection("connecting", "Синхронизация…");
    connectAircraftSocket();
  }

  function scheduleReconnect() {
    stopSocketTimers();
    if (state.reconnectTimer) return;
    if (state.reconnectAttempt === 0) {
      setConnection("connecting", "Повторное подключение…");
    } else {
      setConnection("offline", "Связь потеряна");
    }
    const delay = state.reconnectAttempt === 0
      ? 250
      : Math.min(30000, 1000 * (2 ** state.reconnectAttempt)) + Math.random() * 500;
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
      const wsOpen = state.socket?.readyState === WebSocket.OPEN;
      if (adsb.status === "online") {
        if (wsOpen && state.syncMode === "live") setConnection("online", "ADS-B работает");
        return;
      }
      const labels = {
        unavailable: "ADS-B: readsb недоступен",
        stale: "ADS-B: данные устарели",
        invalid: "ADS-B: ошибка JSON",
      };
      setConnection("offline", labels[adsb.status] || "ADS-B недоступен");
    } catch (_error) {
      // WebSocket owns the connection indicator if the health endpoint is unavailable.
    }
  }

  async function loadStationStatus() {
    if (!el["station-status"]) return;
    try {
      const payload = await fetchJson("/api/station");
      renderStationStatus(payload);
    } catch (error) {
      if (el["station-hint"]) {
        el["station-hint"].textContent = `Не удалось получить состояние станции: ${error.message}`;
        el["station-hint"].classList.add("error-state");
      }
    }
  }

  function renderStationStatus(payload) {
    const ready = payload.ready === true;
    el["station-ready"].textContent = ready ? "готово" : "нет";
    el["station-hint"].classList.remove("error-state");
    el["station-hint"].textContent =
      "Источники, задачи, SDR, диск и ошибки слоёв. Запись сессии — по запросу, не архив за неделю.";
    const adsb = payload.adsb || {};
    const source = payload.source || {};
    const gis = payload.gis || {};
    const radio = payload.radio || {};
    const host = payload.host || {};
    const session = payload.session || {};
    const tasks = payload.tasks || {};
    const positions = payload.positions || {};
    const wrap = el["station-status"];
    wrap.replaceChildren();
    appendStationBlock(wrap, "Готовность", [
      ["Состояние", payload.status || (ready ? "ok" : "degraded")],
      ["Live / ready", `${payload.live === true ? "live" : "—"} / ${ready ? "ready" : "not ready"}`],
      ["Последний пакет", payload.last_batch_at ? formatStationTime(payload.last_batch_at) : "—"],
    ]);
    appendStationBlock(wrap, "ADS-B", [
      ["Источник", `${payload.source_mode || "—"} · ${source.status || adsb.status || "—"}`],
      ["JSON", `${adsb.status || "—"} · возраст ${adsb.json_age_s ?? "—"} с · сообщений ${adsb.messages ?? "—"}`],
      ["Борта", `${adsb.aircraft ?? positions.live ?? "—"} живых, с позицией ${positions.with_position ?? "—"}`],
      ["Возраст позиции", formatPositionAge(positions)],
    ], adsb.status && adsb.status !== "online");
    appendStationBlock(wrap, "Задачи",
      Object.keys(tasks).length
        ? Object.entries(tasks)
        : [["задачи", "нет"]],
      Object.values(tasks).some((value) => value && value !== "running"),
    );
    appendStationBlock(wrap, "SDR", [
      ["Роль ADS-B", radio.adsb || "нет"],
      ["Роль VHF", radio.vhf || "выкл."],
      ["Serials", (radio.serials || []).join(", ") || "нет приёмников"],
      ["Дубли serial", radio.duplicate_serials ? "да" : "нет"],
    ], Boolean(radio.duplicate_serials));
    appendStationBlock(wrap, "Хост", [
      ["Диск", host.disk_free_gb != null ? `${host.disk_free_gb} / ${host.disk_total_gb} ГБ свободно` : "—"],
      ["CPU", host.cpu_temp_c != null ? `${host.cpu_temp_c} °C` : "—"],
    ]);
    const gisErrors = Array.isArray(gis.errors) ? gis.errors : [];
    appendStationBlock(wrap, "GIS", [
      ["Версия", `v${gis.version ?? 0} · последняя годная v${gis.last_good_version ?? 0}`],
      ["Загрузка", `${gis.load_ms ?? 0} мс · слоёв ${gis.layer_count ?? 0} · объектов ${gis.feature_count ?? 0} · зон ${gis.geofence_count ?? 0}`],
      ["Ошибки", gisErrors.length ? gisErrors.map(formatGisError).join("; ") : "нет"],
    ], gisErrors.length > 0);
    appendStationBlock(wrap, "ACARS", [
      ["Приёмник", radio.enabled ? "готов" : "нет"],
      ["Частоты", formatAcarsFrequencies(radio.frequencies_mhz)],
      ["Сообщений", String(radio.count ?? 0)],
      ["Последнее", radio.last_at ? formatStationTime(radio.last_at) : "—"],
    ]);
    renderAcarsQualityStrip(payload);
    if (session.recording) {
      el["session-status"].textContent =
        `Идёт запись: ${session.batches || 0} пакетов, ${session.updates || 0} обновлений`;
    } else if (session.events) {
      el["session-status"].textContent =
        `Запись остановлена: ${session.events} пакетов, ${session.updates || 0} обновлений`;
    } else {
      el["session-status"].textContent = "Запись выключена.";
    }
    el["session-status"].classList.remove("error-state");
  }

  function appendStationBlock(wrap, title, rows, isError = false) {
    const block = document.createElement("section");
    block.className = "station-block";
    const heading = document.createElement("h3");
    heading.textContent = title;
    const list = document.createElement("dl");
    list.className = "station-status";
    rows.forEach(([name, value]) => {
      const dt = document.createElement("dt");
      dt.textContent = name;
      const dd = document.createElement("dd");
      dd.textContent = value || "—";
      if (isError) dd.classList.add("is-error");
      list.append(dt, dd);
    });
    block.append(heading, list);
    wrap.append(block);
  }

  function formatStationTime(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : `${formatUtcTime(date)} UTC`;
  }

  function formatPositionAge(positions) {
    if (positions.newest_position_age_s == null) return "нет координат";
    const newest = positions.newest_position_age_s;
    const oldest = positions.oldest_position_age_s;
    return `свежее ${newest} с · старше ${oldest} с`;
  }

  function formatGisError(item) {
    const id = item.id || "слой";
    const reason = item.reason || item.error || "ошибка";
    return item.kept_previous ? `${id}: ${reason} (оставлена предыдущая версия)` : `${id}: ${reason}`;
  }

  function formatAcarsFrequencies(values) {
    const freqs = Array.isArray(values) ? values : (state.config.acars?.frequencies_mhz || []);
    return freqs.length ? freqs.map((item) => `${item}`).join(" · ") : "—";
  }

  function renderAcarsQualityStrip(payload) {
    if (!el["acars-quality"]) return;
    const adsb = payload.adsb || {};
    const radio = payload.radio || {};
    const positions = payload.positions || {};
    const list = document.createElement("dl");
    list.className = "quality-strip";
    [
      ["ADS-B", `${adsb.status || "—"} · возраст JSON ${adsb.json_age_s ?? "—"} с · бортов ${adsb.aircraft ?? "—"}`],
      ["Позиции", formatPositionAge(positions)],
      ["ACARS", radio.enabled ? `сообщений ${radio.count ?? 0}` : "приёмник не готов"],
      ["Частоты", formatAcarsFrequencies(radio.frequencies_mhz)],
      ["SDR", `ADS-B ${radio.adsb || "—"} · VHF ${radio.vhf || "выкл."}`],
      ["Последнее", radio.last_at ? formatStationTime(radio.last_at) : "—"],
    ].forEach(([name, value]) => {
      const row = document.createElement("div");
      row.className = "quality-strip__row";
      const dt = document.createElement("dt");
      dt.textContent = name;
      const dd = document.createElement("dd");
      dd.textContent = value;
      row.append(dt, dd);
      list.append(row);
    });
    el["acars-quality"].replaceChildren(list);
  }

  async function controlSession(action) {
    try {
      const payload = await fetchJson(`/api/station/session/${action}`, { method: "POST" });
      el["session-status"].textContent = payload.recording
        ? "Запись начата."
        : "Запись остановлена.";
      void loadStationStatus();
    } catch (error) {
      el["session-status"].textContent = `Не удалось изменить запись: ${error.message}`;
      el["session-status"].classList.add("error-state");
    }
  }

  async function downloadSession() {
    try {
      const payload = await fetchJson("/api/station/session");
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "adsb-session.json";
      link.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      el["session-status"].textContent = `Не удалось скачать сессию: ${error.message}`;
    }
  }

  async function downloadDiagnostics() {
    try {
      const payload = await fetchJson("/api/station/diagnostics");
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "airmon-diagnostics.json";
      link.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      if (el["station-hint"]) {
        el["station-hint"].textContent = `Не удалось скачать диагностику: ${error.message}`;
        el["station-hint"].classList.add("error-state");
      }
    }
  }

  async function replaySession() {
    if (!window.confirm("Воспроизвести последнюю записанную сессию в текущий трекер?")) return;
    try {
      const payload = await fetchJson("/api/station/session/replay", { method: "POST" });
      el["session-status"].textContent =
        `Воспроизведено обновлений: ${payload.applied || 0}`;
    } catch (error) {
      el["session-status"].textContent = `Не удалось воспроизвести: ${error.message}`;
      el["session-status"].classList.add("error-state");
    }
  }

  async function uploadSessionFile() {
    const input = el["session-file"];
    const file = input?.files?.[0];
    if (input) input.value = "";
    if (!file) return;
    try {
      const parsed = JSON.parse(await file.text());
      const events = Array.isArray(parsed.events) ? parsed.events : parsed;
      if (!Array.isArray(events) || !events.length) {
        throw new Error("в файле нет events[]");
      }
      if (!window.confirm(`Воспроизвести ${events.length} пакетов из файла в текущий трекер?`)) return;
      const payload = await fetchJson("/api/station/session/replay", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ events }),
      });
      el["session-status"].textContent =
        `Воспроизведено из файла: ${payload.applied || 0} обновлений`;
      el["session-status"].classList.remove("error-state");
    } catch (error) {
      el["session-status"].textContent = `Не удалось загрузить сессию: ${error.message}`;
      el["session-status"].classList.add("error-state");
    }
  }

  const JOURNAL_LIMIT = 400;
  const JOURNAL_CATCHUP_PAGES = 8;

  async function loadJournal() {
    if (state.journalMode === "raw") await loadRawMessages();
    else if (state.journalMode === "geofence") await loadGeofenceEvents();
    else await loadDecodedMessages();
  }

  function mergeJournalItems(existing, incoming) {
    if (!incoming.length) return existing;
    const knownIds = new Set(existing.map((item) => item.id));
    return existing.concat(incoming.filter((item) => !knownIds.has(item.id))).slice(-JOURNAL_LIMIT);
  }

  async function clearJournal() {
    const rawMode = state.journalMode === "raw";
    state.decodedJournalEpoch += 1;
    state.rawJournalEpoch += 1;
    state.geofenceJournalEpoch += 1;
    try {
      const payload = await fetchJson(
        rawMode ? "/api/adsb/raw/clear" : "/api/adsb/messages/clear",
        { method: "POST" },
      );
      if (rawMode) {
        state.rawMessages = [];
        state.lastRawId = 0;
        if (payload.generation) state.rawGeneration = payload.generation;
      } else {
        state.journalEvents = [];
        state.lastEventId = 0;
        state.geofenceEvents = [];
        state.lastGeofenceId = 0;
        if (payload.generation) {
          state.eventGeneration = payload.generation;
          state.geofenceGeneration = payload.generation;
        }
      }
      state.journalTruncated = false;
      renderJournal();
    } catch (error) {
      state.journalError = `Не удалось очистить журнал: ${error.message}`;
      renderJournal();
    }
  }

  function applyJournalGeneration(payload, kind) {
    const generation = text(payload?.generation, "");
    if (!generation) return "ok";
    if (kind === "raw") {
      if (!state.rawGeneration) {
        state.rawGeneration = generation;
        return "ok";
      }
      if (state.rawGeneration === generation) return "ok";
      state.rawGeneration = generation;
      state.rawMessages = [];
      state.lastRawId = 0;
      return "reset";
    }
    if (!state.eventGeneration) {
      state.eventGeneration = generation;
      return "ok";
    }
    if (state.eventGeneration === generation) return "ok";
    state.eventGeneration = generation;
    state.journalEvents = [];
    state.lastEventId = 0;
    return "reset";
  }

  function applyGeofenceGeneration(payload) {
    const generation = text(payload?.generation, "");
    if (!generation) return "ok";
    if (!state.geofenceGeneration) {
      state.geofenceGeneration = generation;
      return "ok";
    }
    if (state.geofenceGeneration === generation) return "ok";
    state.geofenceGeneration = generation;
    state.geofenceEvents = [];
    state.lastGeofenceId = 0;
    return "reset";
  }

  async function loadDecodedMessages() {
    const fetchId = ++state.decodedJournalEpoch;
    let afterId = state.lastEventId;
    for (let page = 0; page < JOURNAL_CATCHUP_PAGES; page += 1) {
      try {
        const payload = await fetchJson(
          `/api/adsb/messages?after_id=${afterId}&limit=${JOURNAL_LIMIT}`,
        );
        if (fetchId !== state.decodedJournalEpoch) return;
        const status = applyJournalGeneration(payload, "decoded");
        if (status === "reset") {
          afterId = 0;
          continue;
        }
        const events = Array.isArray(payload.events) ? payload.events : [];
        const cursor = finite(payload.next_after_id);
        if (cursor !== null) state.lastEventId = cursor;
        state.journalTruncated = Boolean(payload.truncated);
        const hadError = Boolean(state.journalError);
        state.journalError = "";
        if (events.length) {
          state.journalEvents = mergeJournalItems(state.journalEvents, events);
          renderJournal();
        } else if (!state.journalEvents.length || hadError) {
          renderJournal();
        }
        afterId = state.lastEventId;
        if (!(payload.has_more && afterId > 0 && payload.mode === "since")) break;
      } catch (error) {
        if (fetchId !== state.decodedJournalEpoch) return;
        state.journalError = `Ошибка загрузки журнала ADS-B: ${error.message}`;
        renderJournal();
        return;
      }
    }
  }

  async function loadGeofenceEvents() {
    const fetchId = ++state.geofenceJournalEpoch;
    let afterId = state.lastGeofenceId;
    for (let page = 0; page < JOURNAL_CATCHUP_PAGES; page += 1) {
      try {
        const payload = await fetchJson(
          `/api/geofence/events?after_id=${afterId}&limit=${JOURNAL_LIMIT}`,
        );
        if (fetchId !== state.geofenceJournalEpoch) return;
        const status = applyGeofenceGeneration(payload);
        if (status === "reset") {
          afterId = 0;
          continue;
        }
        const events = Array.isArray(payload.events) ? payload.events : [];
        const cursor = finite(payload.next_after_id);
        if (cursor !== null) state.lastGeofenceId = cursor;
        state.journalTruncated = Boolean(payload.truncated);
        const hadError = Boolean(state.journalError);
        state.journalError = "";
        if (events.length) {
          state.geofenceEvents = mergeJournalItems(state.geofenceEvents, events);
          renderJournal();
        } else if (!state.geofenceEvents.length || hadError) {
          renderJournal();
        }
        afterId = state.lastGeofenceId;
        if (!(payload.has_more && afterId > 0 && payload.mode === "since")) break;
      } catch (error) {
        if (fetchId !== state.geofenceJournalEpoch) return;
        state.journalError = `Ошибка загрузки журнала геофенса: ${error.message}`;
        renderJournal();
        return;
      }
    }
  }

  async function loadRawMessages() {
    const fetchId = ++state.rawJournalEpoch;
    let afterId = state.lastRawId;
    for (let page = 0; page < JOURNAL_CATCHUP_PAGES; page += 1) {
      try {
        const payload = await fetchJson(
          `/api/adsb/raw?after_id=${afterId}&limit=${JOURNAL_LIMIT}`,
        );
        if (fetchId !== state.rawJournalEpoch) return;
        const status = applyJournalGeneration(payload, "raw");
        if (status === "reset") {
          afterId = 0;
          continue;
        }
        const messages = Array.isArray(payload.messages) ? payload.messages : [];
        const cursor = finite(payload.next_after_id);
        if (cursor !== null) state.lastRawId = cursor;
        state.journalTruncated = Boolean(payload.truncated);
        const hadError = Boolean(state.journalError);
        state.journalError = "";
        if (messages.length) {
          state.rawMessages = mergeJournalItems(state.rawMessages, messages);
          renderJournal();
        } else if (!state.rawMessages.length || hadError) {
          renderJournal();
        }
        afterId = state.lastRawId;
        if (!(payload.has_more && afterId > 0 && payload.mode === "since")) break;
      } catch (error) {
        if (fetchId !== state.rawJournalEpoch) return;
        state.journalError = `Ошибка загрузки сырых ADS-B сообщений: ${error.message}`;
        renderJournal();
        return;
      }
    }
  }

  function renderJournal() {
    const rawMode = state.journalMode === "raw";
    const geofenceMode = state.journalMode === "geofence";
    const items = rawMode
      ? state.rawMessages
      : (geofenceMode ? state.geofenceEvents : state.journalEvents);
    const list = el["journal-list"];
    const stickToNewest = list.scrollTop < 32;
    const previousHeight = list.scrollHeight;
    const previousTop = list.scrollTop;
    el["journal-count"].textContent = String(items.length);
    const defaultHint = rawMode
      ? "Все строки с порта readsb 30002. AVR разбирается, остальное показывается как есть. Время — UTC. «Очистить» стирает журнал на станции."
      : geofenceMode
        ? "Вход и выход из зон. Версия каталога, тип высоты и гистерезис выхода. Повторный вход в ту же зону не дублируется. Время — UTC."
        : "Изменения декодированных данных бортов. Время записей — UTC. «Очистить» стирает журнал на станции.";
    const truncated = state.journalTruncated
      ? " Кольцевой журнал вытеснил более старые записи."
      : "";
    el["journal-hint"].textContent = state.journalError || `${defaultHint}${truncated}`;
    el["journal-hint"].classList.toggle("error-state", Boolean(state.journalError));
    if (!items.length) {
      list.replaceChildren(
        emptyNode(
          state.journalError
            ? (rawMode
              ? "Поток сырых ADS-B сообщений недоступен"
              : (geofenceMode ? "Журнал геофенса недоступен" : "Журнал ADS-B недоступен"))
            : (rawMode
              ? "Ожидание сырых сообщений…"
              : (geofenceMode ? "Ожидание событий геофенса…" : "Ожидание декодированных сообщений…")),
          Boolean(state.journalError),
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
        label.textContent = [
          icao || (event.df != null ? `DF${event.df}` : "сырой кадр"),
          callsign,
        ].filter(Boolean).join(" ");
        const details = document.createElement("dl");
        details.className = "journal-entry__fields";
        rawJournalRows(event).forEach(([name, value]) => {
          const row = document.createElement("div");
          row.className = "journal-entry__row";
          const dt = document.createElement("dt");
          dt.textContent = name;
          const dd = document.createElement("dd");
          dd.textContent = value;
          row.append(dt, dd);
          details.append(row);
        });
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
      if (geofenceMode) {
        const details = document.createElement("dl");
        details.className = "journal-entry__fields";
        geofenceJournalRows(event).forEach(([name, value]) => {
          const row = document.createElement("div");
          row.className = "journal-entry__row";
          const dt = document.createElement("dt");
          dt.textContent = name;
          const dd = document.createElement("dd");
          dd.textContent = value;
          row.append(dt, dd);
          details.append(row);
        });
        entry.append(details);
      }
      fragment.append(entry);
    });
    list.replaceChildren(fragment);
    if (stickToNewest) list.scrollTop = 0;
    else list.scrollTop = previousTop + (list.scrollHeight - previousHeight);
  }

  function geofenceJournalRows(event) {
    const rows = [];
    const add = (name, value) => {
      if (value === null || value === undefined || value === "") return;
      rows.push([name, String(value)]);
    };
    add("Зона", event.zone);
    add("Ключ", event.geofence_key);
    add("Слой", event.layer_id);
    add("Каталог", event.catalog_version != null ? `v${event.catalog_version}` : "");
    if (event.altitude_ft != null) {
      const ref = event.altitude_reference === "geom" ? "геом" : (event.altitude_reference === "baro" ? "баро" : "");
      add("Высота", `${event.altitude_ft} ft${ref ? ` (${ref})` : ""}`);
    }
    if (event.hysteresis_leave_after != null) {
      add(
        "Гистерезис",
        event.kind === "geofence_leave"
          ? `${event.hysteresis_misses ?? event.hysteresis_leave_after}/${event.hysteresis_leave_after} промаха`
          : `выход после ${event.hysteresis_leave_after} промахов`,
      );
    }
    return rows;
  }

  function rawJournalRows(event) {
    const rows = [];
    const add = (name, value) => {
      if (value === null || value === undefined || value === "") return;
      rows.push([name, String(value)]);
    };
    const acas = event.acas_vs != null || event.acas_ra != null;
    add("Тип", event.df_label || (event.df != null ? `DF${event.df}` : "сырой кадр"));
    if (event.altitude_ft != null) add("Высота", `${event.altitude_ft} ft`);
    else if (acas) add("Высота", "нет данных");
    if (event.squawk) add("Код ответчика", `A${event.squawk}`);
    add("Положение воздух/земля", event.acas_vs);
    add("Текущий уровень чувствительности ACAS", event.acas_sl);
    add("Информация ответа «воздух–воздух»", event.acas_ri);
    if (event.df === 0) add("Возможность кросс-линка", event.acas_cc);
    add("Действующие конфликтные ситуации", event.acas_ra);
    add("Дополнения к RA", event.acas_rac);
    add("Индикатор прекращения RA", event.acas_rat);
    add("Угроза", event.acas_threat);
    const distance = finite(event.distance_km);
    if (distance != null) {
      add("Дальность", distance < 10 ? `${distance.toFixed(1)} км` : `${Math.round(distance)} км`);
    }
    if (rows.length <= 1 && event.text) {
      String(event.text).split(" · ").slice(1).forEach((part) => add("Данные", part));
    }
    return rows;
  }

  async function loadLayers() {
    try {
      const payload = await fetchJson("/api/layers");
      const layers = Array.isArray(payload) ? payload : (payload.layers || []);
      const version = finite(payload?.version);
      renderGisDiagnostics(payload);
      renderLayerList(layers, payload?.errors || []);
      if (version !== null) state.layerCatalogVersion = version;
      await refreshEnabledLayers(layers);
    } catch (error) {
      console.error("Ошибка загрузки списка слоёв:", error);
      renderGisDiagnostics(null, error);
      if (!el["custom-layers"].querySelector(".option")) {
        el["custom-layers"].replaceChildren(emptyNode("Не удалось загрузить слои", true));
      }
    }
  }

  function renderGisDiagnostics(payload, error = null) {
    if (!el["gis-diagnostics"]) return;
    if (error || !payload) {
      el["gis-diagnostics"].textContent =
        `Не удалось загрузить каталог: ${error?.message || "нет ответа"}`;
      el["gis-diagnostics"].classList.add("error-state");
      return;
    }
    const errors = Array.isArray(payload.errors) ? payload.errors : [];
    el["gis-diagnostics"].classList.toggle("error-state", errors.length > 0);
    const reasons = errors.map(formatGisError).join("; ");
    el["gis-diagnostics"].textContent = [
      `версия v${payload.version ?? 0}`,
      `последняя годная v${payload.last_good_version ?? 0}`,
      `${payload.load_ms ?? 0} мс`,
      `объектов ${payload.feature_count ?? 0}`,
      `зон ${payload.geofence_count ?? 0}`,
      errors.length ? `ошибки: ${reasons}` : "ошибок нет",
    ].join(" · ");
  }

  function renderLayerList(layers, errors = []) {
    const incoming = new Set(layers.map((layerInfo) => text(layerInfo.id, "")).filter(Boolean));
    [...state.customLayers.keys()].forEach((id) => {
      if (incoming.has(id)) return;
      const entry = state.customLayers.get(id);
      if (entry?.leaflet) state.map.removeLayer(entry.leaflet);
      state.customLayers.delete(id);
    });
    const fragment = document.createDocumentFragment();
    if (!layers.length && !errors.length) {
      el["custom-layers"].replaceChildren(emptyNode("Нет доступных слоёв"));
      return;
    }
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
    errors.forEach((item) => {
      const note = document.createElement("p");
      note.className = "journal-hint error-state";
      note.textContent = formatGisError(item);
      fragment.append(note);
    });
    el["custom-layers"].replaceChildren(fragment);
  }

  async function refreshEnabledLayers(layers) {
    for (const layerInfo of layers) {
      const id = text(layerInfo.id, "");
      const entry = state.customLayers.get(id);
      if (!id || !entry) continue;
      const version = finite(layerInfo.catalog_version ?? layerInfo.version);
      if (version !== null && version === entry.version) continue;
      const checked = { checked: true, disabled: false };
      if (entry.leaflet) state.map.removeLayer(entry.leaflet);
      state.customLayers.delete(id);
      await toggleCustomLayer(id, checked, layerInfo);
    }
  }

  function rememberCustomLayer(id, leaflet, layerInfo) {
    state.customLayers.set(id, {
      leaflet,
      info: layerInfo,
      version: finite(layerInfo.catalog_version ?? layerInfo.version) ?? state.layerCatalogVersion,
    });
  }

  async function toggleCustomLayer(id, input, layerInfo) {
    if (!input.checked) {
      const entry = state.customLayers.get(id);
      if (entry?.leaflet) state.map.removeLayer(entry.leaflet);
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
        rememberCustomLayer(id, layer, layerInfo);
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
            featureLayer.bindTooltip(plainTooltip(title));
          }
        },
      }).addTo(state.map);
      const info = {
        ...layerInfo,
        catalog_version: finite(payload.version) ?? layerInfo.catalog_version,
      };
      rememberCustomLayer(id, layer, info);
    } catch (error) {
      console.error(`Ошибка загрузки слоя ${id}:`, error);
      input.checked = false;
    } finally {
      input.disabled = false;
    }
  }

  async function loadAcarsMessages() {
    const fetchId = ++state.acarsEpoch;
    let afterId = state.lastAcarsId;
    for (let page = 0; page < JOURNAL_CATCHUP_PAGES; page += 1) {
      try {
        const payload = await fetchJson(
          `/api/acars?after_id=${afterId}&limit=${JOURNAL_LIMIT}`,
        );
        if (fetchId !== state.acarsEpoch) return;
        const generation = text(payload?.generation, "");
        if (generation && state.acarsGeneration && generation !== state.acarsGeneration) {
          state.acarsMessages = [];
          state.lastAcarsId = 0;
          afterId = 0;
          state.acarsGeneration = generation;
          continue;
        }
        if (generation) state.acarsGeneration = generation;
        const messages = Array.isArray(payload.messages) ? payload.messages : [];
        const cursor = finite(payload.next_after_id);
        if (cursor !== null) state.lastAcarsId = cursor;
        state.acarsTruncated = Boolean(payload.truncated);
        const hadError = Boolean(state.acarsError);
        state.acarsError = "";
        if (messages.length) {
          state.acarsMessages = mergeJournalItems(state.acarsMessages, messages);
          renderAcarsMessages();
        } else if (!state.acarsMessages.length || hadError) {
          renderAcarsMessages();
        }
        afterId = state.lastAcarsId;
        if (!(payload.has_more && afterId > 0 && payload.mode === "since")) break;
      } catch (error) {
        if (fetchId !== state.acarsEpoch) return;
        state.acarsError = `Не удалось загрузить ACARS: ${error.message}`;
        renderAcarsMessages();
        return;
      }
    }
  }

  async function clearAcarsMessages() {
    state.acarsEpoch += 1;
    try {
      const payload = await fetchJson("/api/acars/clear", { method: "POST" });
      state.acarsMessages = [];
      state.lastAcarsId = 0;
      state.acarsTruncated = false;
      state.acarsError = "";
      if (payload.generation) state.acarsGeneration = payload.generation;
      renderAcarsMessages();
    } catch (error) {
      state.acarsError = `Не удалось очистить ACARS: ${error.message}`;
      renderAcarsMessages();
    }
  }

  function renderAcarsMessages() {
    const list = el["acars-list"];
    if (!list) return;
    const stickToNewest = list.scrollTop < 32;
    const previousHeight = list.scrollHeight;
    const previousTop = list.scrollTop;
    const items = state.acarsMessages;
    if (el["acars-count"]) el["acars-count"].textContent = String(items.length);
    const defaultHint =
      "Декодер acarsdec на втором RTL (serial 0118) шлёт JSON по UDP. Голос остаётся на отдельном приёмнике. Время — UTC.";
    const truncated = state.acarsTruncated
      ? " Кольцевой журнал вытеснил более старые записи."
      : "";
    if (el["acars-hint"]) {
      el["acars-hint"].textContent = state.acarsError || `${defaultHint}${truncated}`;
      el["acars-hint"].classList.toggle("error-state", Boolean(state.acarsError));
    }
    if (!items.length) {
      list.replaceChildren(
        emptyNode(
          state.acarsError ? "Поток ACARS недоступен" : "Ожидание сообщений ACARS…",
          Boolean(state.acarsError),
        ),
      );
      return;
    }
    const fragment = document.createDocumentFragment();
    [...items].reverse().forEach((message) => {
      const entry = document.createElement("article");
      entry.className = "journal-entry";
      const timestamp = document.createElement("time");
      const date = new Date(message.timestamp);
      const valid = !Number.isNaN(date.getTime());
      if (valid) timestamp.dateTime = date.toISOString();
      timestamp.textContent = valid ? `${formatUtcTime(date)} UTC` : "—";
      const identity = document.createElement("strong");
      identity.textContent = [
        text(message.flight, ""),
        text(message.tail, ""),
        message.label ? `L${message.label}` : "",
      ].filter(Boolean).join(" ") || "без позывного";
      const body = document.createElement("p");
      body.textContent = text(message.text, text(message.summary, "Пустое сообщение"));
      const details = document.createElement("dl");
      details.className = "journal-entry__fields";
      acarsRows(message).forEach(([name, value]) => {
        const row = document.createElement("div");
        row.className = "journal-entry__row";
        const dt = document.createElement("dt");
        dt.textContent = name;
        const dd = document.createElement("dd");
        dd.textContent = value;
        row.append(dt, dd);
        details.append(row);
      });
      entry.append(timestamp, identity, body, details);
      fragment.append(entry);
    });
    list.replaceChildren(fragment);
    if (stickToNewest) list.scrollTop = 0;
    else list.scrollTop = previousTop + (list.scrollHeight - previousHeight);
  }

  function acarsRows(message) {
    const rows = [];
    const add = (name, value) => {
      if (value === null || value === undefined || value === "") return;
      rows.push([name, String(value)]);
    };
    add("Рейс", message.flight);
    add("Борт", message.tail);
    add("Метка", message.label);
    add("Mode", message.mode);
    add("Блок", message.block_id);
    add("№", message.msgno);
    if (message.frequency_mhz != null) add("Частота", `${message.frequency_mhz} МГц`);
    if (message.error) add("Ошибки", message.error);
    if (message.level != null) add("Уровень", `${Number(message.level).toFixed(1)} дБ`);
    return rows;
  }

  function emptyNode(message, isError = false) {
    const node = document.createElement("p");
    node.className = `empty-state${isError ? " error-state" : ""}`;
    node.textContent = message;
    return node;
  }

  function adminHeaders() {
    const token = sessionStorage.getItem(ADMIN_TOKEN_KEY);
    return token ? { "X-Admin-Token": token } : {};
  }

  function askAdminToken() {
    const token = window.prompt("Нужен токен администратора для изменения данных станции.");
    if (!token || !token.trim()) return false;
    sessionStorage.setItem(ADMIN_TOKEN_KEY, token.trim());
    return true;
  }

  async function fetchJson(url, options = {}) {
    const { headers: extraHeaders, retryAuth = true, timeoutMs = 8000, ...rest } = options;
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(url, {
        cache: "no-store",
        ...rest,
        signal: controller.signal,
        headers: { Accept: "application/json", ...adminHeaders(), ...extraHeaders },
      });
      if ((response.status === 401 || response.status === 403) && retryAuth && askAdminToken()) {
        return fetchJson(url, { ...options, retryAuth: false });
      }
      if (!response.ok) {
        throw new Error(await errorMessage(response));
      }
      const contentType = response.headers.get("content-type") || "";
      if (!contentType.includes("application/json")) return {};
      return response.json();
    } catch (error) {
      if (error?.name === "AbortError") {
        throw new Error("тайм-аут запроса");
      }
      throw error;
    } finally {
      window.clearTimeout(timer);
    }
  }

  async function errorMessage(response) {
    try {
      const body = await response.json();
      const detail = body?.detail;
      if (typeof detail === "string" && detail) return detail;
      if (detail && typeof detail === "object") {
        if (typeof detail.save_error === "string" && detail.save_error) return detail.save_error;
        if (typeof detail.message === "string" && detail.message) return detail.message;
      }
    } catch (_error) {
      /* keep HTTP status text */
    }
    return `${response.status} ${response.statusText}`;
  }
})();
