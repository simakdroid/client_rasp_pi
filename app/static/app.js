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
    radioChannels: [],
    playingChannelId: null,
    playingChannelName: "",
    radioPlayback: "idle",
    radioError: "",
    journalError: "",
    listCards: new Map(),
    archiveCards: new Map(),
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
    setRadioAvailable(Boolean(state.config.radio?.enabled));
    createMap();
    connectAircraftSocket();
    document.addEventListener("visibilitychange", resumeSocketIfVisible);
    window.setTimeout(() => {
      if (state.syncMode !== "live") void loadInitialAircraft();
    }, 2500);
    const pollRadio = startPolling(loadRadioChannels, 5000);
    const pollCoverage = startPolling(loadCoverage, 5000);
    const pollTypes = startPolling(loadTypeCatalog, 5000);
    const pollJournal = startPolling(loadJournal, 1000);
    const pollHealth = startPolling(refreshHealth, 5000);
    const pollLayers = startPolling(loadLayers, 15000);
    const pollStation = startPolling(loadStationStatus, 5000);
    void pollLayers();
    void pollStation();
    void pollRadio();
    void pollCoverage();
    void pollTypes();
    void pollJournal();
    void pollHealth();
    el["reload-radio"].addEventListener("click", () => void pollRadio());
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
      "visible-count", "aircraft-search", "aircraft-list",
      "archive-section", "archive-count", "archive-list",
      "custom-layers", "radio-list", "radio-audio", "now-playing",
      "ofm-option", "fit-aircraft", "toggle-tracks", "toggle-coverage",
      "coverage-visible", "coverage-stats", "coverage-caption", "coverage-bands", "coverage-hours", "reset-coverage",
      "reload-layers", "gis-diagnostics", "reload-radio", "radio-hint", "radio-quality",
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
    el["fit-aircraft"].addEventListener("click", fitAircraft);
    el["toggle-tracks"].addEventListener("click", toggleTracks);
    el["toggle-coverage"].addEventListener("click", () => setCoverageVisible(!state.coverageVisible));
    el["coverage-visible"].addEventListener("change", () => {
      setCoverageVisible(el["coverage-visible"].checked);
    });
    el["reset-coverage"].addEventListener("click", resetCoverage);
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
    bindRadioAudio();
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
    updateArchiveMarker(merged);
    if (render) finishAircraftUpdate();
  }

  function dropArchive(keyValue, render = true) {
    const key = text(keyValue, "");
    if (!key) return;
    state.archived.delete(key);
    const marker = state.archiveMarkers.get(key);
    if (marker) state.map.removeLayer(marker);
    state.archiveMarkers.delete(key);
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
    const altitude = aircraftAltitude(aircraft);
    const color = altitudeColor(altitude);
    const rotation = finite(
      aircraft.track_deg ?? aircraft.calculated_track_deg ?? aircraft.true_heading_deg ?? aircraft.heading,
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

  function updateArchiveMarker(aircraft) {
    const key = archiveKey(aircraft);
    const lat = latNum(aircraft.lat ?? aircraft.latitude);
    const lon = lonNum(aircraft.lon ?? aircraft.lng ?? aircraft.longitude);
    const existing = state.archiveMarkers.get(key);
    if (lat === null || lon === null) {
      if (existing) state.map.removeLayer(existing);
      state.archiveMarkers.delete(key);
      return;
    }
    const altitude = aircraftAltitude(aircraft);
    const rotation = finite(
      aircraft.track_deg ?? aircraft.calculated_track_deg ?? aircraft.true_heading_deg ?? aircraft.heading,
    ) ?? 0;
    const icon = aircraftIcon(altitudeColor(altitude), rotation, true);
    if (!existing) {
      const marker = L.marker([lat, lon], {
        icon,
        zIndexOffset: Math.round((altitude || 0) / 4) - 200,
        riseOnHover: true,
        opacity: .7,
      }).addTo(state.map);
      marker.on("click", () => selectAircraft(key));
      marker.bindTooltip(createDataBlock(aircraft), dataBlockOptions(aircraft, true));
      marker.bindPopup(createTooltip(aircraft), { className: "aircraft-tooltip" });
      state.archiveMarkers.set(key, marker);
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
      setCoverageStatus(`Не удалось загрузить розу покрытия: ${error.message}`, true);
    }
  }

  async function resetCoverage() {
    if (!window.confirm("Сбросить накопленную розу покрытия?")) return;
    try {
      const payload = await fetchJson("/api/coverage/reset", { method: "POST" });
      state.coverageRevision = -1;
      renderCoverage(payload);
    } catch (error) {
      setCoverageStatus(`Не удалось сбросить розу покрытия: ${error.message}`, true);
    }
  }

  function setCoverageStatus(stats, isError = false) {
    if (!el["coverage-stats"]) return;
    el["coverage-stats"].textContent = stats;
    el["coverage-stats"].classList.toggle("error-state", Boolean(isError));
  }

  function renderCoverage(payload) {
    const persistError = Boolean(payload?.load_error || payload?.save_error);
    if (el["coverage-caption"]) {
      el["coverage-caption"].textContent =
        payload?.caption || "Исторический максимум дальности, не гарантированная зона приёма.";
    }
    setCoverageStatus(formatCoverageStats(payload), persistError);
    renderCoverageBreakdown(el["coverage-bands"], payload?.altitude_bands, "пояса");
    renderCoverageBreakdown(el["coverage-hours"], payload?.hourly, "часы");
    const revision = finite(payload?.revision) ?? 0;
    const points = Array.isArray(payload?.points) ? payload.points.filter((point) => (
      Array.isArray(point) && latNum(point[0]) !== null && lonNum(point[1]) !== null
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
    if (!samples && !(finite(payload?.observations) > 0)) {
      return "Накопление начнётся с первым бортом.";
    }
    const rangeText = maxRange === null ? "—" : formatDistance(maxRange);
    const observations = finite(payload?.observations) ?? samples;
    const updates = finite(payload?.range_updates) ?? samples;
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
    return `макс. ${rangeText} · ${filled} из 360 направлений · ${updates} обновлений максимума · ${observations} наблюдений${started}${persistHint(payload)}`;
  }

  function renderCoverageBreakdown(node, rows, kind) {
    if (!node) return;
    const items = Array.isArray(rows) ? rows.filter((row) => (row.observations || 0) > 0) : [];
    node.hidden = items.length === 0;
    node.replaceChildren();
    items.forEach((row) => {
      const item = document.createElement("li");
      const label = document.createElement("span");
      label.textContent = row.label || row.hour || row.id || kind;
      const value = document.createElement("span");
      const range = finite(row.max_range_km);
      value.textContent = `${row.observations || 0} набл. · ${range === null ? "—" : formatDistance(range)}`;
      item.append(label, value);
      node.append(item);
    });
  }

  function persistHint(payload) {
    const notes = [];
    if (payload?.load_error) notes.push("файл покрытия повреждён, начато заново");
    if (payload?.save_error) notes.push(`не сохранено: ${payload.save_error}`);
    return notes.length ? ` · ${notes.join(" · ")}` : "";
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
      const lat = latNum(live.lat ?? live.latitude);
      const lon = lonNum(live.lon ?? live.lng ?? live.longitude);
      if (lat !== null && lon !== null) updateMarker(live, lat, lon);
      return;
    }
    const archived = state.archived.get(icao);
    if (archived) updateArchiveMarker(archived);
  }

  function matchesSearch(aircraft) {
    const haystack = [
      aircraft.icao, text(aircraft.callsign, ""), text(aircraft.squawk, ""),
      aircraftTypeCode(aircraft), text(aircraft.type_desc, ""),
    ].join(" ").toUpperCase();
    return !state.search || haystack.includes(state.search);
  }

  const LIST_LIMIT = 80;

  function fillAircraftCard(aircraft, archived = false) {
    const card = byId("aircraft-card-template").content.firstElementChild.cloneNode(true);
    card.addEventListener("click", () => {
      selectAircraft(selectionKey(aircraftFromCard(card), archived));
    });
    updateAircraftCard(card, aircraft, archived);
    return card;
  }

  function aircraftFromCard(card) {
    if (card.dataset.key && state.archived.has(card.dataset.key)) {
      return state.archived.get(card.dataset.key);
    }
    return state.aircraft.get(card.dataset.icao) || { icao: card.dataset.icao };
  }

  function updateAircraftCard(card, aircraft, archived = false) {
    card.dataset.icao = aircraft.icao;
    if (archived) card.dataset.key = archiveKey(aircraft);
    card.classList.toggle("is-selected", state.selectedIcao === selectionKey(aircraft, archived));
    card.classList.toggle("is-archived", archived);
    card.style.setProperty("--aircraft-color", altitudeColor(aircraftAltitude(aircraft)));
    card.querySelector(".aircraft-card__flight strong").textContent = text(
      aircraft.callsign, "без позывного",
    );
    card.querySelector(".aircraft-card__flight small").textContent =
      text(aircraft.icao, "").toUpperCase();
    const typeLabel = formatAircraftType(aircraft);
    const typeNode = card.querySelector('[data-metric="type"]');
    typeNode.textContent = typeLabel || "не определён";
    typeNode.classList.toggle("is-unknown", !typeLabel);
    const altitude = aircraftAltitude(aircraft);
    card.querySelector('[data-metric="altitude"]').textContent =
      altitude === null ? "не определена" : formatAltitude(altitude, aircraft.altitude_reference);
    card.querySelector('[data-metric="speed"]').textContent = formatSpeed(aircraftSpeed(aircraft));
    card.querySelector('[data-metric="position"]').textContent = formatPosition(aircraft);
    const squawk = text(aircraft.squawk, "").trim();
    card.querySelector('[data-metric="squawk"]').textContent = squawk || "—";
    card.querySelector('[data-metric="started"]').textContent =
      formatContactTime(contactStartedAt(aircraft));
    const lostRow = card.querySelector('[data-metric-row="lost"]');
    if (archived) {
      lostRow.hidden = false;
      card.querySelector('[data-metric="lost"]').textContent =
        formatContactTime(aircraft.lost_at);
    } else {
      lostRow.hidden = true;
    }
    card.querySelector(".aircraft-card__zones").textContent = text(aircraft.sector, "");
  }

  function syncCardList(list, items, cards, keyOf, archived) {
    if (list.querySelector(".empty-state") && !list.querySelector(".aircraft-card")) {
      list.replaceChildren();
    }
    const seen = new Set();
    items.forEach((aircraft, index) => {
      const key = keyOf(aircraft);
      seen.add(key);
      let card = cards.get(key);
      if (!card) {
        card = fillAircraftCard(aircraft, archived);
        cards.set(key, card);
      } else {
        updateAircraftCard(card, aircraft, archived);
      }
      const current = list.children[index];
      if (current !== card) list.insertBefore(card, current || null);
    });
    [...cards.keys()].forEach((key) => {
      if (seen.has(key)) return;
      cards.get(key)?.remove();
      cards.delete(key);
    });
  }

  function replaceKeepingScroll(list, node) {
    const top = list.scrollTop;
    list.replaceChildren(node);
    list.scrollTop = top;
  }

  function renderAircraftList() {
    const items = [...state.aircraft.values()]
      .filter(matchesSearch)
      .sort((a, b) => {
        const da = aircraftDistance(a);
        const db = aircraftDistance(b);
        return (da ?? Infinity) - (db ?? Infinity) ||
          text(a.callsign, a.icao).localeCompare(text(b.callsign, b.icao));
      });
    el["visible-count"].textContent = String(items.length);
    const shown = items.slice(0, LIST_LIMIT);
    if (!shown.length) {
      state.listCards.clear();
      replaceKeepingScroll(
        el["aircraft-list"],
        emptyNode(state.search ? "Ничего не найдено" : "Нет активных бортов"),
      );
    } else {
      syncCardList(el["aircraft-list"], shown, state.listCards, (item) => item.icao, false);
      const extra = el["aircraft-list"].querySelector(".list-overflow");
      extra?.remove();
      if (items.length > shown.length) {
        const note = document.createElement("p");
        note.className = "empty-state list-overflow";
        note.textContent = `Показаны ближайшие ${shown.length} из ${items.length}`;
        el["aircraft-list"].append(note);
      }
    }
    renderArchiveList();
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
    state.archived.forEach((aircraft) => updateArchiveMarker(aircraft));
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
      renderArchiveList();
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
      renderArchiveList();
      refreshAircraftMarkers();
    } catch (error) {
      setTypeCatalogStatus(`Не удалось удалить тип ВС: ${error.message}`);
    }
  }

  function renderArchiveList() {
    const items = [...state.archived.values()]
      .filter(matchesSearch)
      .sort((a, b) => text(b.lost_at, "").localeCompare(text(a.lost_at, "")));
    el["archive-section"].hidden = items.length === 0 && !state.search;
    el["archive-count"].textContent = String(items.length);
    if (!items.length) {
      state.archiveCards.clear();
      replaceKeepingScroll(
        el["archive-list"],
        emptyNode(state.search && state.archived.size ? "Ничего не найдено в архиве" : "Архив пуст"),
      );
      if (!state.archived.size) el["archive-section"].hidden = true;
      return;
    }
    syncCardList(el["archive-list"], items.slice(0, LIST_LIMIT), state.archiveCards, archiveKey, true);
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
    const coverage = payload.coverage || {};
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
    appendStationBlock(wrap, "Покрытие", [
      ["Смысл", coverage.caption || "Исторический максимум дальности, не гарантированная зона приёма"],
      ["Роза", `макс. ${coverage.max_range_km ?? 0} км · ${coverage.filled_bins ?? 0}/360 · набл. ${coverage.observations ?? 0}`],
      ["Запись", coverage.save_error || coverage.load_error || (coverage.saved ? "сохранено" : "ожидает запись")],
    ], Boolean(coverage.save_error || coverage.load_error));
    const vhfChannels = Array.isArray(radio.channels) ? radio.channels : [];
    appendStationBlock(wrap, "Радио", [
      ["VHF", radio.enabled ? `каналов ${vhfChannels.length}, активных ${radio.active_channels ?? 0}` : "нет"],
      ["Уровни", vhfChannels.length ? vhfChannels.map(formatRadioQuality).join("; ") : "—"],
    ]);
    renderRadioQualityStrip(payload);
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

  function formatRadioQuality(channel) {
    const name = channel.name || channel.id || "канал";
    const level = channel.level_dbfs == null ? "dBFS —" : `${Number(channel.level_dbfs).toFixed(1)} dBFS`;
    const active = channel.active === true ? "активен" : (channel.active === false ? "тишина" : "н/д");
    return `${name}: ${level}, ${active}`;
  }

  function renderRadioQualityStrip(payload) {
    if (!el["radio-quality"]) return;
    const adsb = payload.adsb || {};
    const radio = payload.radio || {};
    const positions = payload.positions || {};
    const list = document.createElement("dl");
    list.className = "quality-strip";
    const vhfRows = (radio.channels || []).map((channel) => [
      channel.name || channel.id || "VHF",
      formatRadioQuality(channel).replace(/^[^:]+:\s*/, ""),
    ]);
    [
      ["ADS-B", `${adsb.status || "—"} · возраст JSON ${adsb.json_age_s ?? "—"} с · бортов ${adsb.aircraft ?? "—"}`],
      ["Позиции", formatPositionAge(positions)],
      ["VHF", radio.enabled ? `активных ${radio.active_channels ?? 0} / ${(radio.channels || []).length}` : "приёмник не готов"],
      ["SDR", `ADS-B ${radio.adsb || "—"} · VHF ${radio.vhf || "выкл."}`],
      ...vhfRows,
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
    el["radio-quality"].replaceChildren(list);
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

  async function loadRadioChannels() {
    el["reload-radio"].disabled = true;
    try {
      const payload = await fetchJson("/api/radio/channels");
      const channels = Array.isArray(payload) ? payload : (payload.channels || []);
      state.radioChannels = channels;
      state.radioError = "";
      setRadioHint("");
      setRadioAvailable(channels.length > 0 || Boolean(state.config.radio?.enabled));
      renderRadioChannels(channels);
    } catch (error) {
      state.radioError = `Не удалось обновить каналы: ${error.message}`;
      setRadioHint(state.radioError);
      if (state.radioChannels.length) {
        renderRadioChannels(state.radioChannels);
      } else {
        setRadioAvailable(Boolean(state.config.radio?.enabled));
        el["radio-list"].replaceChildren(emptyNode("Не удалось загрузить каналы", true));
      }
    } finally {
      el["reload-radio"].disabled = false;
    }
  }

  function setRadioHint(message) {
    if (!el["radio-hint"]) return;
    el["radio-hint"].hidden = !message;
    el["radio-hint"].textContent = message || "";
    el["radio-hint"].classList.toggle("error-state", Boolean(message));
  }

  function setRadioAvailable(_available) {
    const radioTab = document.querySelector('.tab[data-tab="radio"]');
    if (radioTab) radioTab.hidden = false;
  }

  function renderRadioChannels(channels) {
    if (!channels.length) {
      el["radio-list"].replaceChildren(emptyNode(state.radioError || "Нет доступных VHF-каналов", Boolean(state.radioError)));
      return;
    }
    const fragment = document.createDocumentFragment();
    channels.forEach((channel) => {
      const row = document.createElement("div");
      row.className = "radio-channel";
      const active = channel.active === undefined ? channel.activity : channel.active;
      row.classList.toggle("is-active", active === true);
      row.classList.toggle("is-playing", text(channel.id, "") === text(state.playingChannelId, ""));
      const activity = document.createElement("span");
      activity.className = "radio-channel__activity";
      activity.title = radioActivityLabel(active);
      const info = document.createElement("span");
      info.className = "radio-channel__info";
      const name = document.createElement("strong");
      name.textContent = text(channel.name ?? channel.label, "Канал");
      const frequency = document.createElement("small");
      const frequencyValue = channel.frequency_mhz ?? channel.frequency;
      const level = finite(channel.level_dbfs);
      frequency.textContent = [
        frequencyValue === undefined ? "Частота не указана" : `${frequencyValue} МГц`,
        level === null ? null : `${level.toFixed(1)} dBFS`,
        radioActivityLabel(active),
      ].filter(Boolean).join(" · ");
      info.append(name, frequency);
      const play = document.createElement("button");
      play.type = "button";
      play.className = "radio-channel__play";
      play.textContent = "▶";
      play.title = "Слушать канал";
      const streamUrl = rewriteStreamUrl(channel.stream_url ?? channel.url);
      play.disabled = !streamUrl;
      play.addEventListener("click", () => playRadioChannel(channel, row));
      row.append(activity, info, play);
      fragment.append(row);
    });
    el["radio-list"].replaceChildren(fragment);
    updateNowPlaying();
  }

  function radioActivityLabel(active) {
    if (active === true) return "Есть активность";
    if (active === false) return "Нет активности";
    return "Активность неизвестна";
  }

  function rewriteStreamUrl(url) {
    if (!url) return url;
    try {
      const parsed = new URL(url, location.href);
      const pageHost = location.hostname;
      const localPage = pageHost === "127.0.0.1" || pageHost === "localhost";
      if (!localPage && (parsed.hostname === "127.0.0.1" || parsed.hostname === "localhost")) {
        parsed.hostname = pageHost;
      }
      return parsed.href;
    } catch (_error) {
      return url;
    }
  }

  function bindRadioAudio() {
    const audio = el["radio-audio"];
    if (!audio) return;
    audio.addEventListener("playing", () => {
      state.radioPlayback = "playing";
      updateNowPlaying();
    });
    audio.addEventListener("waiting", () => {
      state.radioPlayback = "waiting";
      updateNowPlaying();
    });
    audio.addEventListener("ended", () => {
      state.radioPlayback = "ended";
      updateNowPlaying();
    });
    audio.addEventListener("error", () => {
      state.radioPlayback = "error";
      updateNowPlaying();
    });
  }

  function updateNowPlaying() {
    if (!el["now-playing"]) return;
    el["now-playing"].classList.toggle("error-state", state.radioPlayback === "error");
    if (!state.playingChannelId) {
      el["now-playing"].textContent = state.radioError || "Поток не выбран";
      return;
    }
    const labels = {
      playing: "",
      waiting: " (буфер…)",
      ended: " (остановлен)",
      error: " (ошибка потока)",
      idle: "",
    };
    el["now-playing"].textContent =
      `${state.playingChannelName || "VHF-канал"}${labels[state.radioPlayback] || ""}`;
  }

  async function playRadioChannel(channel, row) {
    const streamUrl = rewriteStreamUrl(channel.stream_url ?? channel.url);
    if (!streamUrl) return;
    state.playingChannelId = text(channel.id, "");
    state.playingChannelName = text(channel.name ?? channel.label, "VHF-канал");
    state.radioPlayback = "waiting";
    document.querySelectorAll(".radio-channel").forEach((item) => item.classList.remove("is-playing"));
    row.classList.add("is-playing");
    updateNowPlaying();
    const resolved = new URL(streamUrl, location.href).href;
    if (el["radio-audio"].src !== resolved) {
      el["radio-audio"].src = streamUrl;
    }
    try {
      await el["radio-audio"].play();
    } catch (error) {
      state.radioPlayback = "error";
      updateNowPlaying();
    }
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
