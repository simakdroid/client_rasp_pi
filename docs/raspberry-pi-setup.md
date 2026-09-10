# Raspberry Pi OS Bookworm 64-bit: ADS-B + VHF

Конфигурация рассчитана на Raspberry Pi OS Bookworm 64-bit и два RTL2832U.
Единственный каталог приложения на станции — `/opt/adsb-vhf` (не `/opt/air-monitor`).

- ADS-B 1090 МГц — EEPROM serial `1090`;
- авиационный VHF AM — EEPROM serial `0118`.

Serial задаются в `/etc/adsb-vhf/sdr.env` (`deploy/env/sdr.env.example`) и
подхватываются `readsb`, `rtl_airband` и backend. Не дублируйте их в
`/etc/default/readsb-adsb`.

Если подключён только один RTL‑SDR, launcher открывает индекс `0` даже при
пустом EEPROM serial, отдаёт его `readsb`, а `rtl_airband` не запускается.
Общие udev-правила дают группе `rtl-sdr` доступ к такому донглу. При двух
устройствах роли снова фиксируются по serial: `1090` для ADS‑B и `0118` для VHF.

RTL-SDR открывается через `libusb`. Путь вида `/dev/bus/usb/…` меняется и не
является корректным идентификатором для readsb. После `rtl_eeprom -s` строка
USB iSerial должна совпасть с тем, что `rtl_test -t` печатает как `SN:` —
именно этот serial используют `readsb --device` и `rtl_airband`. VID/PID
остаются `0bda` и `2832` или `2838`.

Горячее подключение обрабатывает `adsb-vhf-rtl-hotplug.service`: `SYSTEMD_WANTS`
сам по себе не перезапускает уже работающий readsb, поэтому oneshot заново
выбирает индекс `0` или serial `1090` и включает/выключает `rtl-airband`.

## 1. Подготовка Raspberry Pi

```bash
sudo apt update
sudo apt full-upgrade -y
sudo apt install -y git build-essential cmake pkg-config \
  rtl-sdr librtlsdr-dev libusb-1.0-0-dev \
  libconfig++-dev libfftw3-dev libmp3lame-dev libshout3-dev \
  chromium gettext-base python3-venv
```

Если Icecast должен работать на этом же Raspberry Pi:

```bash
sudo apt install -y icecast2
sudo systemctl enable --now icecast2
```

Пароли источника задаются в `/etc/icecast2/icecast.xml`. Не помещайте их в git.

## 2. Запись уникальных serial

Подключайте только один донгл за раз, чтобы не перепрограммировать другой:

```bash
# Донгл ADS-B:
sudo rtl_eeprom -s 1090

# После полного отключения первого и подключения VHF-донгла:
sudo rtl_eeprom -s 0118
```

После каждой записи физически отключите и снова подключите донгл. Затем
подключите оба и проверьте:

```bash
rtl_test -t
```

В списке должны быть ровно две разные строки `SN: 1090` и `SN: 0118`. Если
заводские serial одинаковы или пусты, устойчиво назначить роли невозможно.
Для интерактивного `rtl_test` добавьте пользователя в группу `rtl-sdr`:
`sudo usermod -aG rtl-sdr "$USER"`.

## 3. Установка readsb и RTLSDR-Airband

Скрипт развёртывания не устанавливает произвольные сторонние сборки. Сначала
проверьте пакеты:

```bash
apt-cache show readsb 2>/dev/null | head
apt-cache show rtl-airband 2>/dev/null | head
```

Если пакет доступен из настроенного доверенного репозитория:

```bash
sudo apt install readsb rtl-airband
```

Иначе соберите из официальных исходников. Для readsb:

```bash
git clone https://github.com/wiedehopf/readsb.git
cd readsb
make -j"$(nproc)" RTLSDR=yes
sudo install -m 0755 readsb /usr/bin/readsb
cd ..
```

Для RTLSDR-Airband:

```bash
git clone https://github.com/rtl-airband/RTLSDR-Airband.git
cd RTLSDR-Airband
mkdir build
cd build
cmake -DPLATFORM=native ..
make -j"$(nproc)"
sudo install -m 0755 rtl_airband /usr/bin/rtl_airband
cd ../..
```

На 64-битной ОС не выбирайте `PLATFORM=rpiv2`: этот вариант включает
несовместимое VideoCore FFT. `native` использует FFTW и подходит для текущей
машины; `generic` можно выбрать вместо него для переносимой сборки.

Сборка readsb должна поддерживать RTL-SDR. Launcher передаёт выбранный serial в
`--device`: для RTL-SDR это селектор EEPROM serial, а не путь устройства.
Проверьте синтаксис конкретной сборки командой `readsb --help`. Если старая
сборка принимает только числовой индекс перечисления, обновите readsb; индексы
`0` и `1` могут меняться после перезагрузки или перестановки USB.

## 4. Развёртывание системных файлов

Из корня проекта:

```bash
sudo sh ./deploy/install.sh pi
sudo reboot
```

`pi` — имя пользователя графического сеанса; замените его при необходимости.
Скрипт:

- устанавливает правила udev, группу `rtl-sdr` и блокирует DVB-драйвер ядра;
- создаёт системных пользователей сервисов;
- копирует приложение в `/opt/adsb-vhf` и создаёт Python venv;
- GIS-слои оставляет только для чтения (`/opt/adsb-vhf/data/layers`);
- покрытие и каталог типов пишет в `/var/lib/adsb-vhf` (systemd `StateDirectory`);
- устанавливает unit-файлы и шаблоны конфигурации, включая `/etc/adsb-vhf/sdr.env`;
- не перезаписывает уже созданные env-файлы с секретами и `/etc/default/readsb-adsb`;
- не запускает сервисы до настройки координат, частот и паролей.

`ReadWritePaths` каталоги не создаёт: `install.sh` делает `mkdir` для
`/var/lib/adsb-vhf` и слоёв.

После перезагрузки ещё раз проверьте `rtl_test -t`. Если вывод содержит
`Kernel driver is active`, проверьте `/etc/modprobe.d/blacklist-rtl-sdr.conf` и
выполните ещё одну перезагрузку.

### 4.1. Горячее подключение RTL-SDR

Сценарии (после reboot с установленными unit-файлами):

| Событие | Ожидание |
| --- | --- |
| Загрузка без донгла | `readsb-adsb` не стартует (`ExecCondition`), радио выключено |
| Вставили один донгл | hotplug запускает readsb на индексе `0` |
| Вставили второй (`1090`+`0118`) | readsb перезапускается на serial `1090`, стартует `rtl-airband` |
| Вынули VHF | `rtl-airband` останавливается, ADS-B остаётся на единственном стике (`0`) |
| Вынули оба | оба сервиса останавливаются |
| Вставили снова после crash loop | `reset-failed` в hotplug; при необходимости `sudo systemctl reset-failed` |

## 5. readsb

Отредактируйте `/etc/default/readsb-adsb`, если координаты станции ещё не заданы:

```bash
sudo nano /etc/default/readsb-adsb
```

Это **только** синтаксис systemd `EnvironmentFile` (`KEY=value`), файл не
является shell-скриптом: без `source`, без `eval`. `start-readsb.sh` собирает
argv при `set -f`. Файл должен принадлежать root. Повторный `install.sh` его
не затирает. Serial берите из `/etc/adsb-vhf/sdr.env`. Не указывайте `/dev`-путь.
JSON создаётся в `/run/readsb`, Beast TCP — на порту `30005`. readsb обычно
слушает сетевые порты на всех интерфейсах; ограничьте доступ firewall, если Pi
не находится в доверенной сети.

Остановите конфликтующий декодер, если он установлен:

```bash
sudo systemctl disable --now readsb.service dump1090-fa.service 2>/dev/null || true
sudo systemctl enable --now readsb-adsb.service
systemctl status readsb-adsb.service
journalctl -u readsb-adsb.service -n 100 --no-pager
```

Проверка данных:

```bash
ls -l /run/readsb/aircraft.json
ss -ltn | grep 30005
```

## 6. VHF AM и Icecast

Каталог каналов — `deploy/radio-channels.json`. Его частоты и mountpoint должны
совпадать с `.env.example`, `deploy/env/backend.env.example` и
`deploy/rtl-airband/rtl_airband.conf.in`. Скопированный шаблон использует
`serial = "${VHF_SERIAL}"`, три AM-частоты 118.1 / 118.5 / 119.1,
`centerfreq = 118.600` МГц и полосу `2.56` МГц. Не смешивайте 125.8 МГц с
диапазоном 118.x на одном стике: они не помещаются в 2.56 МГц вокруг 118.6.
Для далёких частот нужен режим сканирования или дополнительный приёмник.

Настройте секретный env-файл (пароли Icecast остаются здесь, `0600` у
сгенерированного conf; backend читает только `stats.prom`):

```bash
sudo nano /etc/adsb-vhf/rtl-airband.env
sudo chown root:rtl-airband /etc/adsb-vhf/rtl-airband.env
sudo chmod 0640 /etc/adsb-vhf/rtl-airband.env
```

Пример находится в `deploy/env/rtl-airband.env.example`; реального пароля в
репозитории нет. Для беспроблемной подстановки в libconfig используйте пароль
из символов `A-Z`, `a-z`, `0-9`, `.`, `_`, `~`, `-`. Кавычки и обратные слеши
потребуют экранирования в шаблоне.

После изменения частот или env:

```bash
sudo systemctl enable --now rtl-airband.service
systemctl status rtl-airband.service
journalctl -u rtl-airband.service -n 100 --no-pager
curl -I http://127.0.0.1:8000/vhf-118100.mp3
```

Unit перед каждым запуском формирует конфигурацию с секретом только в
`/run/rtl-airband/`, затем запускает `rtl_airband` в foreground. Пароль не
передаётся аргументом процесса. Подставляются `ICECAST_*` и `VHF_SERIAL`,
а готовый `rtl_airband.conf` получает права `0600`, чтобы web-процесс из
группы `rtl-airband` читал `stats.prom`, но не пароль Icecast.
`stats_filepath` обновляет Prometheus-файл примерно раз в 15 секунд; backend
сравнивает `channel_activity_counter` и показывает активность squelch и
текущий dBFS без выдачи UI системных прав.

С телефона или другого ПК в LAN открывайте UI по имени хоста Pi, не по
`127.0.0.1` на клиенте. `GET /api/radio/channels` подменяет loopback в
`stream_url` на Host запроса, порт Icecast `8000` сохраняется. Клиентский
`rewriteStreamUrl` остаётся запасным вариантом. Страница по HTTPS и Icecast по
HTTP — mixed content: браузер заблокирует поток; нужен reverse proxy с TLS
и для UI, и для аудио, либо HTTP на всей станции.

## 7. Backend

`deploy/install.sh` копирует приложение в `/opt/adsb-vhf`, создаёт `.venv`,
устанавливает production-зависимости и оставляет сервисы выключенными до
настройки. Отредактируйте реальные координаты, пути и список Icecast-потоков:

```bash
sudo nano /etc/adsb-vhf/backend.env
```

Production-bind задают `BACKEND_HOST` и `BACKEND_PORT` в этом файле — их
подставляет `uvicorn` в unit. `AIRMON_HOST` / `AIRMON_PORT` из локального
`.env` нужны только разработчику (`uvicorn` из CLI) и systemd не читает.

Каталог типов ВС и роза покрытия пишутся в `/var/lib/adsb-vhf`
(`AIRMON_AIRCRAFT_TYPES_PATH`, `AIRMON_COVERAGE_PATH`). `/opt/adsb-vhf/data`
при `ProtectSystem=strict` только для чтения: слой GIS туда кладут вручную,
а сохранение типов в этот путь даёт `Read-only file system`. Повторный
`install.sh` дописывает недостающие ключи в уже существующий `backend.env`.

Если backend будет доступен из LAN, задайте `AIRMON_ADMIN_TOKEN` и не кладите
его в статику. Изменяющие маршруты (`/api/coverage/reset`, каталог типов,
очистка журналов) требуют заголовок `X-Admin-Token`. Kiosk на loopback без
токена продолжает работать, пока переменная пуста. Токен живёт в
`sessionStorage` браузера, а не в Chromium `--password-store`.

Затем запустите backend:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now adsb-vhf-backend.service
curl http://127.0.0.1:8080/api/health
journalctl -u adsb-vhf-backend.service -n 100 --no-pager
```

Для обновления приложения:

```bash
cd ~/client_rasp_pi
git pull
sudo sh ./deploy/install.sh "$USER"
sudo systemctl reset-failed readsb-adsb rtl-airband
sudo systemctl restart readsb-adsb adsb-vhf-backend
systemctl --user restart adsb-kiosk.service
```

Env-файлы и `/etc/default/readsb-adsb` с локальными координатами и секретами
не перезаписываются. Установщик
сам перезапускает уже активные системные сервисы; явные команды выше также
снимают возможный `start-limit`, оставшийся после старого цикла ошибок.

## 8. Chromium kiosk на Bookworm

Kiosk — **user**-сервис (`adsb-kiosk.service` в `~/.config/systemd/user`), не
system-unit. Физический доступ к киоску не является границей безопасности:
`--password-store=basic` относится только к UI Chromium, админ-токен в него не
кладётся. Отдельный профиль — `KIOSK_PROFILE_DIR` (по умолчанию
`~/.config/adsb-vhf-chromium`). Если backend не отвечает 60 секунд, unit
завершается ошибкой и `Restart=always` повторяет попытку; Chromium без UI не
запускается.

Raspberry Pi OS Bookworm Desktop обычно использует Wayfire/Wayland. Добавьте
строку из `deploy/chromium/wayfire-autostart.ini` в существующую секцию
`[autostart]` файла `~/.config/wayfire.ini` графического пользователя. Не
создавайте вторую секцию `[autostart]`. Chromium использует
`--ozone-platform-hint=auto`, без жёсткого `WAYLAND_DISPLAY=wayland-0`.

Затем от имени этого пользователя:

```bash
systemctl --user daemon-reload
systemctl --user start adsb-kiosk.service
systemctl --user status adsb-kiosk.service
```

Wayfire запускает user-unit при входе в графический сеанс. URL по умолчанию —
`http://127.0.0.1:8080/`. Его можно изменить drop-in-файлом:

```bash
systemctl --user edit adsb-kiosk.service
```

```ini
[Service]
Environment=KIOSK_URL=http://127.0.0.1:8080/
```

Для автоматического входа включите Desktop Autologin через:

```bash
sudo raspi-config
```

Выберите `System Options` → `Boot / Auto Login` → `Desktop Autologin`.

## 9. Итоговая диагностика

```bash
systemctl --failed
systemctl status readsb-adsb rtl-airband adsb-vhf-backend
systemctl --user status adsb-kiosk
journalctl -b -u readsb-adsb -u rtl-airband -u adsb-vhf-backend --no-pager
```

Типовые причины ошибок:

- serial не записан либо оба донгла имеют одинаковый serial;
- USB iSerial не совпадает с `SN:` у `rtl_test -t`;
- DVB-модуль ядра всё ещё захватил USB-устройство;
- пользователь сервиса не состоит в `rtl-sdr`;
- частоты VHF не помещаются в одну полосу при `multichannel`;
- Icecast не принимает source credentials;
- страница открыта по HTTPS, а поток Icecast остался HTTP;
- `ExecStart` backend не соответствует фактической структуре приложения.
