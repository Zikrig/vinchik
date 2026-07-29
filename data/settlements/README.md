# Settlements dump (portable)

Файл **`settlements.csv.gz`** (~8 MB) — справочник населённых пунктов **только Таджикистан + Россия**.

Сейчас в дампе: **~195k мест**, **~586k имён/алиасов** (полные GeoNames `TJ.zip` + `RU.zip`, feature class P).

**Отображаемое имя** (`display_name`) предпочитает **кириллицу** (русский / тоҷикӣ), если такой алиас есть; латиница GeoNames остаётся только для поиска и как запасной вариант.

Копируй папку `data/settlements/` вместе с проектом на новый сервер. При старте бота, если таблица `settlements` пуста, дамп импортируется в Postgres автоматически (первый импорт может занять несколько минут).

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
