# Raspberry Pi OS Bookworm 64-bit: ADS-B + VHF

Конфигурация рассчитана на Raspberry Pi OS Bookworm 64-bit и два RTL2832U:

- ADS-B 1090 МГц — EEPROM serial `1090`;
- авиационный VHF AM — EEPROM serial `0118`.

Если подключён только один RTL‑SDR, launcher открывает индекс `0` даже при
пустом EEPROM serial, отдаёт его `readsb`, а `rtl_airband` не запускается.
Общие udev-правила дают сервису доступ к такому донглу. При двух устройствах роли снова
фиксируются по serial: `1090` для ADS‑B и `0118` для VHF. После горячего
изменения состава приёмников выполните
`sudo systemctl restart readsb-adsb rtl-airband`; при загрузке выбор выполняется
автоматически.

RTL-SDR открывается через `libusb`. Путь вида `/dev/bus/usb/…` меняется и не
является корректным идентификатором для readsb. Правила udev дают доступ группе
`plugdev` и создают systemd-метаданные, а приложения выбирают приёмник по
строковому EEPROM serial. В частности, `0118` — строка с ведущим нулём, не число.

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

- устанавливает правила udev и блокирует DVB-драйвер ядра;
- создаёт системных пользователей сервисов;
- копирует приложение в `/opt/adsb-vhf` и создаёт Python venv;
- устанавливает unit-файлы и шаблоны конфигурации;
- не перезаписывает уже созданные env-файлы с секретами и `/etc/default/readsb-adsb`;
- не запускает сервисы до настройки координат, частот и паролей.

После перезагрузки ещё раз проверьте `rtl_test -t`. Если вывод содержит
`Kernel driver is active`, проверьте `/etc/modprobe.d/blacklist-rtl-sdr.conf` и
выполните ещё одну перезагрузку.

## 5. readsb

Отредактируйте `/etc/default/readsb-adsb`, если координаты станции ещё не заданы:

```bash
sudo nano /etc/default/readsb-adsb
```

Проверьте `--lat` и `--lon`. Повторный `install.sh` этот файл не затирает.
`ADSB_PREFERRED_SERIAL=1090` применяется при двух приёмниках; при одном
launcher выбирает единственное устройство. Не указывайте `/dev`-путь. JSON создаётся в
`/run/readsb`, Beast TCP — на порту `30005`. readsb обычно слушает сетевые
порты на всех интерфейсах; ограничьте доступ firewall, если Pi не находится в
доверенной сети.

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

Скопированный шаблон `/etc/rtl_airband.conf.in` использует serial `0118` и три
примерные AM-частоты, `centerfreq = 118.600` МГц и полосу `2.56` МГц.
Обязательно замените частоты на разрешённые локальные значения и сдвиньте
center frequency так, чтобы все каналы оставались в общей полосе. Для далёких
частот нужен режим сканирования или дополнительный приёмник.

Настройте секретный env-файл:

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
передаётся аргументом процесса. `stats_filepath` обновляет Prometheus-файл
примерно раз в 15 секунд; backend сравнивает `channel_activity_counter` и
показывает активность squelch и текущий dBFS без выдачи UI системных прав.

## 7. Backend

`deploy/install.sh` копирует приложение в `/opt/adsb-vhf`, создаёт `.venv`,
устанавливает production-зависимости и оставляет сервисы выключенными до
настройки. Отредактируйте реальные координаты, пути и список Icecast-потоков:

```bash
sudo nano /etc/adsb-vhf/backend.env
```

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
sudo systemctl reset-failed readsb-adsb
sudo systemctl restart readsb-adsb adsb-vhf-backend
systemctl --user restart adsb-kiosk.service
```

Env-файлы и `/etc/default/readsb-adsb` с локальными координатами и секретами
не перезаписываются. Установщик
сам перезапускает уже активные системные сервисы; явные команды выше также
снимают возможный `start-limit`, оставшийся после старого цикла ошибок.

## 8. Chromium kiosk на Bookworm

Raspberry Pi OS Bookworm Desktop обычно использует Wayfire/Wayland. Добавьте
строку из `deploy/chromium/wayfire-autostart.ini` в существующую секцию
`[autostart]` файла `~/.config/wayfire.ini` графического пользователя. Не
создавайте вторую секцию `[autostart]`.

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
- DVB-модуль ядра всё ещё захватил USB-устройство;
- пользователь сервиса не состоит в `plugdev`;
- частоты VHF не помещаются в одну полосу при `multichannel`;
- Icecast не принимает source credentials;
- `ExecStart` backend не соответствует фактической структуре приложения.
