(() => {
  "use strict";

  /*
   * Ожидаемый протокол /ws/aircraft:
   *   {"type":"snapshot","aircraft":[Aircraft, ...]} — полное состояние;
   *   {"type":"upsert","aircraft":Aircraft}          — новый/изменённый борт;
   *   {"type":"remove","icao":"ABC123"}              — удалить борт.
   * Для совместимости snapshot может быть массивом, а delta — объектом
   * {"type":"delta","upsert":[...], "remove":["ABC123", ...]}. Элемент upsert
   * может содержать track_append с новыми точками вместо полной истории.
   * Aircraft обязан содержать
   * icao (или hex), lat, lon; остальные используемые поля необязательны:
   * callsign, altitude/alt_baro, speed/ground_speed, squawk, track/heading,
   * distance, trail/positions (массив точек [lat, lon] или {lat, lon}).
   */

  const state = {
    map: null,
    config: {},
    station: null,
    basemaps: {},
    activeBasemap: null,
    aircraft: new Map(),
    markers: new Map(),
    tracks: new Map(),
    customLayers: new Map(),
    socket: null,
    reconnectTimer: null,
    reconnectAttempt: 0,
    updatesThisSecond: 0,
    tracksVisible: true,
    selectedIcao: null,
    search: "",
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
    await Promise.allSettled([loadInitialAircraft(), loadLayers(), loadRadioChannels()]);
    connectAircraftSocket();
    window.setInterval(updateRate, 1000);
    window.setInterval(loadRadioChannels, 5000);
  }

  function cacheElements() {
    [
      "station-name", "connection", "connection-text", "aircraft-count",
      "message-rate", "visible-count", "aircraft-search", "aircraft-list",
      "custom-layers", "radio-list", "radio-audio", "now-playing",
      "ofm-option", "fit-aircraft", "toggle-tracks", "reload-layers", "reload-radio",
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
    el["reload-layers"].addEventListener("click", loadLayers);
    el["reload-radio"].addEventListener("click", loadRadioChannels);
  }

  function switchTab(name) {
    document.querySelectorAll(".tab").forEach((tab) => {
      tab.classList.toggle("is-active", tab.dataset.tab === name);
    });
    document.querySelectorAll(".tab-panel").forEach((panel) => {
      panel.classList.toggle("is-active", panel.id === `panel-${name}`);
    });
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
      : (Array.isArray(mapConfig.center) ? mapConfig.center : [55.75, 37.62]);

    state.map = L.map("map", {
      center,
      zoom: finite(mapConfig.zoom) ?? 8,
      zoomControl: true,
      preferCanvas: true,
    });

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
      replaceAircraft(aircraft);
    } catch (error) {
      console.error("Ошибка начальной загрузки бортов:", error);
      setConnection("offline", "Начальные данные недоступны");
    }
  }

  function replaceAircraft(items) {
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
    finishAircraftUpdate();
  }

  function upsertAircraft(aircraft, render = true) {
    const icao = normalizeIcao(aircraft);
    const lat = finite(aircraft.lat ?? aircraft.latitude);
    const lon = finite(aircraft.lon ?? aircraft.lng ?? aircraft.longitude);
    if (!icao) return;

    const previous = state.aircraft.get(icao) || {};
    const merged = { ...previous, ...aircraft, icao };
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

  function removeMapObjects(icao) {
    const marker = state.markers.get(icao);
    const track = state.tracks.get(icao);
    if (marker) state.map.removeLayer(marker);
    if (track) state.map.removeLayer(track);
    state.markers.delete(icao);
    state.tracks.delete(icao);
  }

  function finishAircraftUpdate() {
    el["aircraft-count"].textContent = String(state.aircraft.size);
    renderAircraftList();
  }

  function updateMarker(aircraft, lat, lon) {
    const icao = aircraft.icao;
    const altitude = aircraftAltitude(aircraft);
    const color = altitudeColor(altitude);
    const rotation = finite(
      aircraft.track_deg ?? aircraft.calculated_track_deg ?? aircraft.heading,
    ) ?? 0;
    let marker = state.markers.get(icao);

    if (!marker) {
      marker = L.marker([lat, lon], {
        icon: aircraftIcon(color, rotation),
        zIndexOffset: Math.round(altitude || 0),
        title: `${text(aircraft.callsign, icao)} (${icao})`,
      }).addTo(state.map);
      marker.on("click", () => selectAircraft(icao));
      marker.bindTooltip(createTooltip(aircraft), {
        direction: "top",
        offset: [0, -14],
        className: "aircraft-tooltip",
        opacity: 1,
      });
      state.markers.set(icao, marker);
    } else {
      marker.setLatLng([lat, lon]);
      marker.setIcon(aircraftIcon(color, rotation));
      marker.setTooltipContent(createTooltip(aircraft));
      marker.setZIndexOffset(Math.round(altitude || 0));
    }
  }

  function aircraftIcon(color, rotation) {
    return L.divIcon({
      className: "aircraft-marker",
      iconSize: [30, 30],
      iconAnchor: [15, 15],
      html: `<div class="aircraft-marker__plane" style="--rotation:${rotation}deg;color:${color}">
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path fill="currentColor" stroke="#07111d" stroke-width=".7" d="M12 1.2c.8 0 1.35.85 1.55 2.15l.95 6.15 6.8 4.05v1.8l-6.55-1.9-.45 5.05 2.35 1.65v1.35L12 20.6l-4.65.9v-1.35L9.7 18.5l-.45-5.05-6.55 1.9v-1.8L9.5 9.5l.95-6.15C10.65 2.05 11.2 1.2 12 1.2Z"/>
        </svg>
      </div>`,
    });
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
      ["Высота", formatAltitude(aircraftAltitude(aircraft))],
      ["Скорость", formatSpeed(aircraftSpeed(aircraft))],
      ["Squawk", text(aircraft.squawk)],
      ["Дистанция", formatDistance(aircraftDistance(aircraft))],
    ];
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

  function fitAircraft() {
    const positions = [...state.markers.values()].map((marker) => marker.getLatLng());
    if (positions.length === 1) state.map.setView(positions[0], Math.max(state.map.getZoom(), 11));
    if (positions.length > 1) state.map.fitBounds(L.latLngBounds(positions).pad(.12), { maxZoom: 12 });
  }

  function selectAircraft(icao) {
    state.selectedIcao = icao;
    const marker = state.markers.get(icao);
    if (marker) {
      state.map.panTo(marker.getLatLng());
      marker.openTooltip();
    }
    renderAircraftList();
  }

  function renderAircraftList() {
    const fragment = document.createDocumentFragment();
    const items = [...state.aircraft.values()]
      .filter((aircraft) => {
        const haystack = `${aircraft.icao} ${text(aircraft.callsign, "")}`.toUpperCase();
        return !state.search || haystack.includes(state.search);
      })
      .sort((a, b) => {
        const da = aircraftDistance(a);
        const db = aircraftDistance(b);
        return (da ?? Infinity) - (db ?? Infinity) ||
          text(a.callsign, a.icao).localeCompare(text(b.callsign, b.icao));
      });

    items.forEach((aircraft) => {
      const card = byId("aircraft-card-template").content.firstElementChild.cloneNode(true);
      card.dataset.icao = aircraft.icao;
      card.classList.toggle("is-selected", state.selectedIcao === aircraft.icao);
      card.style.setProperty("--aircraft-color", altitudeColor(aircraftAltitude(aircraft)));
      card.querySelector(".aircraft-card__identity strong").textContent = text(aircraft.callsign, aircraft.icao);
      card.querySelector(".aircraft-card__identity small").textContent = aircraft.icao;
      card.querySelector(".aircraft-card__metrics strong").textContent = formatAltitude(aircraftAltitude(aircraft));
      card.querySelector(".aircraft-card__metrics small").textContent =
        `${formatSpeed(aircraftSpeed(aircraft))} · ${formatDistance(aircraftDistance(aircraft))}`;
      card.addEventListener("click", () => selectAircraft(aircraft.icao));
      fragment.append(card);
    });

    el["aircraft-list"].replaceChildren(
      fragment.childNodes.length ? fragment : emptyNode(state.search ? "Ничего не найдено" : "Нет активных бортов"),
    );
    el["visible-count"].textContent = String(items.length);
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
      state.updatesThisSecond += 1;
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
      replaceAircraft(message.aircraft || []);
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
    if (Array.isArray(message.upsert) || Array.isArray(message.remove)) {
      (message.upsert || []).forEach((aircraft) => upsertAircraft(aircraft, false));
      (message.remove || []).forEach((icao) => removeAircraft(icao, false));
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

  function updateRate() {
    el["message-rate"].textContent = String(state.updatesThisSecond);
    state.updatesThisSecond = 0;
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
        style: () => ({
          color: layerInfo.color || "#ffc857",
          weight: finite(layerInfo.weight) ?? 2,
          opacity: finite(layerInfo.opacity) ?? .85,
          fillOpacity: finite(layerInfo.fill_opacity) ?? .15,
        }),
        pointToLayer: (_feature, latlng) => L.circleMarker(latlng, {
          radius: 5,
          color: layerInfo.color || "#ffc857",
          fillOpacity: .65,
        }),
        onEachFeature: (feature, featureLayer) => {
          const title = feature?.properties?.name ?? feature?.properties?.title;
          if (title !== undefined && title !== null) featureLayer.bindTooltip(String(title));
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

  async function fetchJson(url) {
    const response = await fetch(url, {
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    return response.json();
  }
})();
