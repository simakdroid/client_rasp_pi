# Raspberry Pi Air Monitor

Локальное модульное приложение для Raspberry Pi 5: ADS‑B через `readsb`,
авиационный VHF AM через `rtl_airband`/Icecast и интерактивная GIS-карта.

## 1. Архитектура

Выбран локальный Web‑UI (FastAPI + Leaflet + Vanilla JS), потому что он требует
меньше памяти, чем PySide6/QWebEngine, обновляется независимо от Chromium и
может открываться с любого устройства локальной сети, если администратор
осознанно изменит адрес bind.

```text
RTL-SDR serial=1090 ─► readsb ─► /run/readsb/aircraft.json
                                     │
                                     ▼ (poll 750 ms)
                              FastAPI / asyncio
                            ┌────────┼──────────┐
                            │ tracker│ GIS      │
                            │ + delta│ geofence │
                            └────────┼──────────┘
                                     │ WebSocket/REST
                                     ▼
                             Chromium Kiosk/Leaflet
                                     ▲
RTL-SDR serial=0118 ─► rtl_airband ─► Icecast HTTP audio
```

`readsb` и `rtl_airband` остаются отдельными systemd-сервисами. Web-процесс не
получает root-доступ и не управляет systemd. Это изолирует сбой UI от
радиоприёма и исключает переключение не того USB-донгла.

Режим приёмников определяется автоматически:

- один совместимый RTL‑SDR — весь приёмник назначается `readsb`, радио отключено;
- два RTL‑SDR — `1090` используется для ADS‑B, `0118` для VHF AM;
- при неоднозначной конфигурации из нескольких устройств без preferred serial
  readsb не стартует, чтобы случайно не занять VHF-приёмник.

Поток ADS‑B можно брать из атомарно обновляемого JSON или с SBS‑1 TCP/30003.
Beast TCP/30005 намеренно остаётся входом `readsb`, а не Python-кода:
декодирование Mode‑S/CPR уже корректно и существенно эффективнее реализовано в
`readsb`. Backend получает готовые позиции, ведёт ограниченные треки, считает
WGS‑84 расстояние/курс и проверяет полигоны Shapely.

WebSocket отправляет начальный `snapshot`, затем `delta` с массивами `upsert`
и `remove` каждые 500–1000 мс. В `upsert` история не дублируется: новые точки
идут в `track_append`. Медленному клиенту приходит `resync`, после чего он
заново запрашивает снимок. Это не даёт очередям и трафику неограниченно расти.

## 2. Структура проекта

```text
.
├── app/
│   ├── adsb.py            # readsb JSON и SBS-1 ingestion
│   ├── broadcast.py       # fan-out WebSocket-дельт
│   ├── config.py          # настройки из AIRMON_* / .env
│   ├── gis.py             # GeoJSON, KML, MBTiles, geofencing
│   ├── main.py            # FastAPI, REST/WS, фоновые задачи
│   ├── models.py          # нормализованные модели
│   ├── radio.py           # каталог каналов/индикатор активности
│   ├── tracker.py         # состояния, треки, курс, высотная скорость
│   └── static/            # Leaflet Web-UI
├── data/layers/           # пользовательские GeoJSON/KML/MBTiles
├── deploy/                # udev, systemd, выбор RTL-SDR, rtl_airband, kiosk
├── docs/                  # установка Raspberry Pi OS
├── tests/
├── .env.example
└── pyproject.toml
```

## 3. GIS-слои

Скопируйте `.geojson`, `.json`, `.kml` или `.mbtiles` в каталог слоёв. Он
перечитывается раз в 30 секунд. GeoJSON должен быть `FeatureCollection`.
Полигоны по умолчанию участвуют в geofencing; свойства:

```json
{
  "name": "CTR",
  "geofence": true,
  "min_alt_ft": 0,
  "max_alt_ft": 12000,
  "color": "#ff7800"
}
```

KML-конвертер специально ограничен Point/LineString/Polygon. Сложные KML,
KMZ, стили и reprojection лучше заранее преобразовать через GDAL:
`ogr2ogr -f GeoJSON output.geojson input.kml`. Входные координаты должны быть
WGS‑84 (EPSG:4326). MBTiles поддерживает TMS-схему и форматы png/jpeg/webp/pbf;
для PBF клиент использует Leaflet.VectorGrid.

## 4. Локальный запуск разработчика

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
# Для запуска вне Raspberry Pi измените путь AIRMON_READSB_JSON_PATH.
uvicorn app.main:app --host 127.0.0.1 --port 8080
```

Откройте `http://127.0.0.1:8080`. Проверка:

```bash
pytest
ruff check .
curl http://127.0.0.1:8080/api/health
```

Полная настройка донглов, Icecast, сервисов и Kiosk приведена в
[`docs/raspberry-pi-setup.md`](docs/raspberry-pi-setup.md).

## 5. API-контракт

- `GET /api/config` — станция и URL подложек.
- `GET /api/aircraft` — полный снимок бортов и треков.
- `WS /ws/aircraft` — снимок и дельты.
- `GET /api/layers` / `GET /api/layers/{id}` — GIS-каталог/GeoJSON.
- `GET /api/tiles/{id}/{z}/{x}/{y}` — локальный MBTiles.
- `GET /api/radio/channels` — частоты, stream URL и опциональная активность.
- `GET /api/health` — readiness процесса.

Активность VHF берётся не из Icecast (наличие mount не означает открытый
squelch), а из Prometheus-файла `rtl_airband`: backend сравнивает
`channel_activity_counter` между обновлениями и отдаёт `level_dbfs`.

HTTP по умолчанию слушает только loopback. При доступе из LAN следует
добавить reverse proxy, аутентификацию и явный firewall; один лишь CORS не
является защитой.
