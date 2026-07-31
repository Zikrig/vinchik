Нагрузочный контур поднимается отдельным Compose-проектом, не поверх обычного бота.

Из корня репо:

```powershell
docker compose -p vinchik-loadtest `
  -f docker-compose.yml `
  -f docker-compose.loadtest.yml `
  --profile loadtest up --build `
  --abort-on-container-exit --exit-code-from loadgen loadgen
```

По умолчанию: webhook, ступени **5 / 10 / 25 / 40** RPS, режим `capacity`.

## Polling

Бот должен long-poll'ить mock, а loadgen — пушить апдейты в очередь:

```powershell
$env:LOADTEST_DELIVERY = "polling"
$env:LOADTEST_USE_WEBHOOK = "false"
docker compose -p vinchik-loadtest `
  -f docker-compose.yml `
  -f docker-compose.loadtest.yml `
  --profile loadtest up --build `
  --abort-on-container-exit --exit-code-from loadgen loadgen
```

Оба флага обязательны и должны совпадать (`webhook`↔`true`, `polling`↔`false`).

Что делает контур:

- базовый `docker-compose.yml` + оверлей `docker-compose.loadtest.yml`
- проект `vinchik-loadtest` — своя БД/сеть
- Telegram → mock; metrics на `:8081`
- webhook: POST на `/webhook/bot`; polling: `POST /__mock__/push` + bot `getUpdates`
- loadgen: prepare → load_test

После прогона:

```powershell
docker compose -p vinchik-loadtest `
  -f docker-compose.yml `
  -f docker-compose.loadtest.yml down
```

Результат: `ignored/loadtest/results/latest.json`.

Selftest mock:

```powershell
docker compose -p vinchik-loadtest `
  -f docker-compose.yml `
  -f docker-compose.loadtest.yml `
  run --rm telegram-mock python -m loadtest.selftest
```

Режим лимитов Telegram: `$env:LOADTEST_RUN_MODE = "telegram-realistic"`.

Важно: не подставлять реальный `BOT_TOKEN` — в override фиктивный токен и mock API.
