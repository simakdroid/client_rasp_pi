# Raspberry Pi Air Monitor

Версия **2.0**. Локальное модульное приложение для Raspberry Pi 5: ADS‑B через `readsb`,
авиационный VHF AM через `rtl_airband`/Icecast и интерактивная GIS-карта.

## Что изменилось в 2.0

Операторская панель станции показывает источники, возраст позиции, фоновые задачи,
роли SDR, диск, температуру, ошибки GIS и записи покрытия. Запись ADS‑B-сессии
идёт в память по запросу (не архив за неделю): её можно скачать, загрузить JSON
и воспроизвести в трекер для регрессии. Журнал геофенса хранит версию зоны,
тип высоты и гистерезис выхода без повторных входов. Каталог слоёв сообщает
причину отказа, число объектов, время загрузки и последнюю годную версию.
Роза покрытия считается историческим максимумом дальности по высотным поясам
и часам, а не гарантированной зоной приёма. Диагностический пакет отдаёт версии
ПО, обезличенную конфигурацию и последние ошибки без токенов, паролей и stream URL.
Вкладка «Качество приёма» сводит ADS‑B и VHF (dBFS). Добавлены admin-токен,
live/ready, атомарная запись файлов и hotplug RTL‑SDR.

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

- один совместимый RTL‑SDR — открывается как индекс `0` и целиком назначается
  `readsb`, даже если EEPROM serial пуст; радио отключено;
- два RTL‑SDR — `1090` используется для ADS‑B, `0118` для VHF AM;
- при неоднозначной конфигурации из нескольких устройств без preferred serial
  readsb не стартует, чтобы случайно не занять VHF-приёмник.

Поток ADS‑B можно брать из атомарно обновляемого JSON или с SBS‑1 TCP/30003.
Поля даты и времени SBS интерпретируются в `AIRMON_SBS_TIMEZONE` (по умолчанию UTC)
и сохраняются уже в UTC. Beast TCP/30005 намеренно остаётся входом `readsb`, а не Python-кода:
декодирование Mode‑S/CPR уже корректно и существенно эффективнее реализовано в
`readsb`. Backend получает готовые позиции, ведёт ограниченные треки, считает
WGS‑84 расстояние/курс и проверяет полигоны Shapely. После потери борт с тем
же позывным и сквоком возвращается в тот же контакт, если пауза не длиннее
трёх TTL; иначе и при смене опознавания начинается новый контакт.

Leaflet и VectorGrid входят в проект локально. Для загрузки интерфейса CDN не
требуется; интернет нужен только внешней подложке OSM, если локальная MBTiles
подложка не настроена.

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
Пользовательские сектора в git не входят: кладите их в `data/layers/` на станции
или в `AIRMON_LAYERS_DIR` (после установки это `/opt/adsb-vhf/data/layers/`).
Полигоны по умолчанию участвуют в geofencing. Граница входит в зону
(`shapely.covers()`): точка на контуре считается внутри. `code` попадает в
формуляр борта. Если несколько кодов пересекаются, побеждает больший
`control_priority` (УДР/ДЗ обычно 30, секторы РПИ — 20); при равенстве —
меньшая площадь (более локальная зона), затем лексикографически меньший ключ
`layer_id:name`. Политика отдаётся в `GET /api/layers` как `geofence_policy`.
Идентификатор слоя — имя файла без расширения: `[A-Za-z0-9][A-Za-z0-9._-]{0,79}`.
Дубликаты stem (`ctr.geojson` и `ctr.mbtiles`) не перезаписывают первый слой:
второй файл попадает в `errors`. Пример свойств:

```json
{
  "name": "Сектор 1",
  "code": "С1",
  "control_priority": 20,
  "geofence": true,
  "min_alt_ft": 5000,
  "min_alt_exclusive": true,
  "color": "#e57373"
}
```

`min_alt_exclusive: true` соответствует формулировке «выше FL…»: нижняя граница
не входит в зону, верхняя (`max_alt_ft`) входит.

KML-конвертер специально ограничен Point/LineString/Polygon, включая отверстия
`innerBoundaryIs`. Сложные KML, KMZ, стили и reprojection лучше заранее
преобразовать через GDAL:
`ogr2ogr -f GeoJSON output.geojson input.kml`. Входные координаты должны быть
WGS‑84 (EPSG:4326). Координаты GeoJSON вне ±180/±90 отбрасываются вместе с
объектом. MBTiles должен содержать таблицы (или представления) `metadata` и
`tiles`; поддерживаются только png/jpeg/jpg/webp/pbf/mvt. Неизвестный `format`
не попадает в каталог (ошибка слоя), а не отдаётся как PBF. Тайлы — TMS;
gzip-сжатый PBF отдаётся с `Content-Encoding: gzip`. Для PBF клиент использует
Leaflet.VectorGrid.

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
- `GET /api/aircraft` — снимок бортов (`generation`, `seq`, полные треки).
- `GET /api/adsb/messages` — журнал декодированных обновлений. `after_id=0`
  отдаёт последние N (`mode: latest`); `after_id>0` — последовательное чтение
  без пропусков (`mode: since`). Есть `generation`, `latest_id`,
  `next_after_id` и `truncated`, если кольцо вытеснило старые записи.
- `GET /api/adsb/raw` — тот же контракт для сырых AVR/Mode‑S с readsb TCP/30002.
- `WS /ws/aircraft` — snapshot/delta с одной парой `generation`+`seq`; `resync`
  требует новое соединение, а не параллельный REST.
- `GET /api/layers` — GIS-каталог одной версии: `version`, `last_good_version`,
  `load_ms`, `feature_count`, `errors[].reason` / `kept_previous`.
  `GET /api/layers/{id}` отдаёт уже разобранный GeoJSON этой версии.
- `GET /api/tiles/{id}/{z}/{x}/{y}` — локальный MBTiles.
- `GET /api/coverage` — роза исторического максимума дальности (`kind:
  historical_max_range`), не гарантированная зона приёма. Есть `altitude_bands`
  и `hourly`. `range_updates` — сколько раз обновлялся максимум
  в бине, `observations` — наблюдения с новой позицией; `samples` — синоним
  `range_updates`. `heard_at` — последний приём, `updated_at` — последнее
  обновление максимума. `saved` / `save_error` / `load_error` отделяют память
  от диска; повреждённый файл уходит в `*.bad`.
- `GET /api/geofence/events` — журнал входа/выхода из зон: версия каталога,
  тип высоты, гистерезис; повторный вход в ту же зону не дублируется.
- `GET /api/station` — панель станции: источники, возраст позиции, задачи,
  SDR-роли, диск/температура, GIS и покрытие.
- `GET /api/station/diagnostics` — пакет поддержки: версии ПО, обезличенная
  конфигурация, последние ошибки GIS/покрытия. Без токенов, паролей и
  stream URL.
- `POST /api/station/session/start|stop` и `GET /api/station/session` —
  запись ADS‑B в память (не архив за неделю). `POST /api/station/session/replay`
  воспроизводит последнюю запись или JSON `events[]` из файла.
- `GET /api/aircraft-types` — ручной справочник ICAO→тип (fallback, если ADS‑B
  не дал тип). Писатель файла — сам процесс; внешние правки подхватываются
  фоновым `refresh`, а не GET/lookup. `POST`/`DELETE` требуют админ-токен.
- `GET /api/radio/channels` — частоты, stream URL и опциональная активность.
  Loopback в `stream_url` подменяется на Host запроса, если UI открыт не с
  localhost; порт Icecast сохраняется. HTTPS-страница + HTTP Icecast — mixed
  content. Уровни `level_dbfs` — качество VHF, не наличие Icecast mount.
- `GET /api/health` — readiness процесса.

Активность VHF берётся не из Icecast (наличие mount не означает открытый
squelch), а из Prometheus-файла `rtl_airband`: backend сравнивает
`channel_activity_counter` между обновлениями и отдаёт `level_dbfs`.

HTTP по умолчанию слушает только loopback. Локальный `.env` задаёт
`AIRMON_HOST` / `AIRMON_PORT` для `uvicorn` разработчика; на станции bind
берётся из `BACKEND_HOST` / `BACKEND_PORT` в `/etc/adsb-vhf/backend.env`.
Перед доступом из LAN задайте `AIRMON_ADMIN_TOKEN` (заголовок `X-Admin-Token` для изменяющих маршрутов),
добавьте reverse proxy, ограничьте Origin/Host и явный firewall; один лишь
CORS не является защитой. `GET /api/health` разделяет `live` (процесс отвечает)
и `ready` (ingestion/broadcast живы, источник не молчит).
