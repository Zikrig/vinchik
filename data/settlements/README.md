# Settlements dump (portable)

Файл **`settlements.csv.gz`** — справочник населённых пунктов **только Таджикистан + Россия**.

Сейчас в дампе: **~195k мест**, **~586k имён/алиасов** (полные GeoNames `TJ.zip` + `RU.zip`, feature class P).

## Зачем столько имён?

В дампе хранятся **все алиасы** (в т.ч. исторические: Ленинград, Петроград) — **только для поиска**.  
Пользователь может набрать «Ленинград», а в UI везде показывается **современное** `display_name` (Санкт-Петербург).

Исторические названия **не удаляем**: иначе поиск по ним перестанет находить город.

## Собрать/обновить дамп (нужен интернет)

```bash
python scripts/build_settlements_dump.py
```

## Принудительный импорт в БД

```bash
python scripts/import_settlements.py
# или
docker compose exec bot python scripts/import_settlements.py
```
