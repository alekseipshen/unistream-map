# ЮНИСТРИМ — обменные пункты у метро (Москва)

Внутренний справочник: 16 отделений АО КБ «ЮНИСТРИМ» на карте Москвы + все станции метро/МЦК/МЦД.
Выбор станции → ближайшие отделения, время пешком, адрес, «как найти», график, телефон и живые курсы валют.

- Данные отделений и курсы: официальный API unistream.ru (`/api/poses/exchange/{id}`)
- Курсы подтягиваются в браузере при открытии страницы и каждые 10 минут (CORS у API открыт)
- Станции метро: api.hh.ru/metro/1
- Карта: Leaflet + OpenStreetMap (всё self-hosted, кроме тайлов)

## Обновить статические данные (адреса, график, состав отделений)

```bash
python3 scripts/build_data.py --fetch   # тянет всё заново из API
git add -A && git commit -m "refresh data" && git push
```

Состав отделений: `scripts/branch-ids.json` (номер офиса → id в системе Юнистрим).
Найти id нового офиса: `https://api7.unistream.com/api/v1/poses/search?location=55.75,37.62&radius=25000&maxResults=200&filter.agent=11153`

## Конкуренты и их курсы

В карточке отделения — сводка «лучше всех покупают / дешевле всех продают» в радиусе 1 км;
красным подсвечено то, что выгоднее нашего. На карте: красная точка = обыгрывает нас, серая = нет.

- **Курсы конкурентов**: banki.ru, эндпоинт `/products/currencyNodejsApi/getExchangesCoordinates/`
  (один запрос на валюту отдаёт все обменные пункты Москвы с координатами и курсом).
  Обязателен заголовок `X-Requested-With: XMLHttpRequest`, иначе 404.
- ⚠️ **banki.ru отдаёт данные только на российский IP** (иначе anti-bot заглушка), поэтому HTTP-запрос
  выполняется на RuVDS через ssh — `scripts/remote_fetch_banki.py`, а обработка и git push на Hetzner.
- Обновление: systemd timer `unistream-competitors.timer` на Hetzner, каждые 2 часа 09:10–21:10 МСК.
  Коммитит `data/competitors.js` только при реальном изменении курсов. При сбое пишет в топик Alert (7113).

```bash
python3 scripts/collect_competitors.py --dry-run   # проверить сбор без записи
systemctl start unistream-competitors.service      # разовый прогон
journalctl -u unistream-competitors.service -n 30  # что было в последний раз
```

Точки без публикуемых курсов (слой «Другие банки рядом») — из OpenStreetMap, меняются редко:

```bash
python3 scripts/collect_osm_poi.py    # раз в месяц-два
```
