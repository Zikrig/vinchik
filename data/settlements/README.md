# Settlements dump (portable)

Файл **`settlements.csv.gz`** (~15 MB) — справочник населённых пунктов.

Сейчас в дампе: **~214k мест**, **~1.2M имён/алиасов** (GeoNames: TJ+UZ+KG+AF полностью + cities1000 по миру).

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
