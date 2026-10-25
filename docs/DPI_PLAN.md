# Часть 2: DPI-рулетка (Logitech G PRO X Superlight 2)

Статус (2026-10-25): реализован путь 1 (HID++ напрямую) в `app/modules/dpi/`. Протокол
сверен с документацией и чужими реализациями, но **на реальной мышке не проверен**: первый
запуск на стрим-ПК = кнопка «Проверить мышь» в панели, затем «Применить» с тестовым значением.
Ниже — исходное исследование, на которое опирается реализация.

## Как менять DPI программно — варианты по убыванию надёжности

### 1. HID++ 2.0 напрямую в мышь (рекомендуемый основной путь)
* Мышь: receiver LIGHTSPEED `046D:C54D`, провод `046D:C09B`, WPID `0x40A9`, device index 1 через ресивер (0xFF по проводу).
* DPI задаётся фичей **0x2202 Extended Adjustable DPI**, функция 6 `setSensorDpiParameters`
  `[sensor=0, dpiX BE16, dpiY BE16, lod]` в long-репорте 0x11 (20 байт). Индекс фичи искать
  в рантайме через ROOT.getFeature (на виденных экземплярах 0x09). LOD 1..3 (0 отклоняется).
* **Обязательное условие:** мышь должна быть в HOST-режиме (0x8100 Onboard Profiles, fn1
  `setOnboardMode(0x02)`); в onboard-режиме запись отвергается с ошибкой 0x05.
* Windows открывает vendor-коллекции (usage page 0xFF00, usage 0x0002 для long) в shared-режиме,
  поэтому `hidapi` работает параллельно с запущенным G HUB (так делают LGSTrayBattery,
  better-logihub, mouseutility). Python: `pip install hidapi`.
* Сетка допустимых DPI (читается с устройства, fn2): шаг 1 (100–200), 2 (200–500), 5 (500–1000),
  **10 (1000–2000), 20 (2000–5000)**, 50 (5000–10000)… до 44000 (прошивка ≥ 32.3.26).
  Рулетка должна «прилипать» к сетке, значение 1234 будет отвергнуто.
* Мышь шлёт событие 0x2202 ev0 при каждом изменении DPI — им можно подтверждать результат.

### 2. WebSocket-API G HUB (`ws://localhost:9010`, subprotocol `json`)
* Конверт `{msgId, verb: GET|SET|SUBSCRIBE, path, payload}`, без токена, нужен заголовок `Origin: file://`.
* В бинаре агента есть пути `/mouse/{devId}/dpi`, `/mouse/{devId}/dpi_table`, `/mouse/{devId}/dpi_shift`,
  но **ни один публичный проект не делает SET DPI** через него — только чтение батареи. Форматы
  SET выведены из бинаря и требуют проверки; сам UI G HUB меняет DPI через SET `/card`
  (`mouseSettings.dpiTable`) по named pipe, а не по 9010.
* Способ узнать точный формат: запустить `lghub.exe --remote-debugging-port=9222`, в консоли
  `globalThis.setDebugFlag(true)` и подвигать слайдер DPI — UI залогирует SENT/RECEIVED.

### 3. Lua в G HUB (`SetMouseDPITable`)
В строках агента есть «function is not implemented in G HUB» — скорее всего мёртвый путь.
Проверка занимает 5 минут (скрипт в профиле), но рассчитывать на него не стоит.

### 4. Запасные
* Переключение onboard-профилей (0x8100 fn3/fn12) — работает только при включённом
  «On-Board Memory Mode» в G HUB, даёт до 25 заранее заданных значений.
* Правка `settings.db` + перезапуск G HUB — 10–40 с, для живой рулетки непригодно.

## Главная ловушка: G HUB «отбивает» DPI
G HUB заново применяет DPI профиля при смене активного профиля (alt-tab CS2 ↔ рабочий стол),
при переподключении/пробуждении мыши и при своём выходе (возвращает onboard-режим).
Решения: в G HUB включить **Settings → Persistent Profile** (закрепить профиль CS2), а утилита
подписывается на `/devices/state/changed` и `/profile/active/changed`/`/profile/active` и
переприменяет значение; либо после каждой записи читать fn5 и повторять при расхождении.

## Интеграция в это приложение
* `app/modules/dpi/__init__.py` — интерфейс `DpiBackend.set_dpi(value)`; реализовать
  `HidppBackend` (hidapi) и опционально `GhubWsBackend`.
* Маршруты уже зарезервированы: `/api/dpi/status`, `/overlay/dpi`. Добавить
  `POST /api/dpi/spin` → сервер выбирает значение (снэп к сетке), публикует SSE-событие
  `dpi_spin {target, value, durationMs, revolutions}`; оверлей крутит колесо до заданного
  сегмента (библиотека spin-wheel, `spinToItem`), по `onRest` показывает число; DPI
  применяется по окончании анимации (настраиваемая задержка).
* Хоткей `dpi_spin` добавить в `HotkeySettings`/`HotkeyManager` (архитектура позволяет).

## Эксперимент на стрим-ПК (сначала он)
1. `pip install hidapi`; перечислить `hid.enumerate(0x046D, 0xC54D)`, открыть путь с
   usage_page 0xFF00 / usage 2.
2. ROOT.getFeature(0x2202) и (0x8100) → индексы; setOnboardMode(2); fn6 → 1600; fn5 → проверить.
3. С запущенным G HUB: alt-tab в CS2 и обратно, усыпить/разбудить мышь, открыть окно G HUB —
   фиксировать, когда DPI сбрасывается. Повторить с Persistent Profile.
4. Если всё ок — реализовать `HidppBackend`, иначе — исследовать WS `/mouse/{id}/dpi`.
