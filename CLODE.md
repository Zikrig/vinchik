# CLODE — vinchik

Telegram dating bot (aiogram 3.29) + FastAPI admin. Postgres, Redis FSM.

## Куда лезть

| Задача | Путь |
|--------|------|
| Точка входа бота | `bot.py` |
| Веб-админка | `web.py`, `webapp/` |
| Список аккаунтов (поиск/фильтры) | `webapp/` → `/accounts`, `services/accounts.py` |
| Карточка аккаунта (блоки: премиум / лайки / аккаунт / анкета) | `/accounts/{tg_id}`; премиум активен только если `premium_until > now` (`is_premium`); снять → `2004-01-01` |
| Карта пользователей (≤50, админ красным) | `/accounts` (Leaflet), `map_markers()` |
| Гео админа / тестовые юзеры | `services/admin_tools.py`, `services/media.py` |
| Тестовое фото | `data/test.png` |
| Роутеры | `handlers/` |
| Бизнес-логика | `services/` |
| Модели БД | `database/models.py` |
| Тексты UI | `locales/ru.py`, `locales/tg.py` (кнопка tg первой) |
| Клавиатуры | `keyboards/inline.py` |
| Конфиг | `config/settings.py`, `.env` |
| ТЗ | `TZ.md` |

## Инварианты

- Кнопки пользователя — inline (кроме request_location).
- Без нумерации на кнопках.
- Лимит лайков: default 50, сутки **UTC**; у мужчин без Премиум при исчерпании **нельзя смотреть ленту**.
- Женщины и Премиум — без лимита.
- Лента: взаимный looking_for; диагональ км×возраст: волна 0 = 10км±2; волна 1 = 10км±5 и 25км±2; волна 2 = 10км±10 + 25км±5 + 50км±2; … Каждый `next_profile` с нуля по текущим кандидатам. Внутри волны: премиум → |Δвозраст| → ближе км.
- После ❤️/👎/💌 пара **взаимно** скрывается из ленты на `profile_reshow_days` (default **60**; **0** = навсегда). 💤 кнопки снимает, **не** пишет в `likes`.
- После истечения окна анкета может снова попасть в ленту; новая реакция обновляет ту же строку `likes`.
- Реактивация: только через 1 / 3 / 7 суток после `last_activity_at` (не спамить).
- Симпатии: батч-уведомление ≤1/30 мин; матч не обязателен.
- Админка бота `/admin`: заявки по одной со счётчиком; премиум-юзеры с датой + листание; soft-launch 🟢/🔴; настройки. Баны/аккаунты — веб.
- Веб-дашборд: блок «Режимы» (soft-launch / тесты) с зелёно-красными свитчами; каналы — тот же индикатор. POST форм админки — AJAX без перезагрузки (toast).
- Админка веб: карта оплаты, гео, тестовые юзеры, баны, `/accounts` (+ карточка `/accounts/{tg_id}`).
- Тестовые юзеры: `User.is_test`, негативные `tg_id`, фото `data/test.png`; свич «Тестовые в ленте» = setting `test_users_visible` + массовый `Profile.is_active`.
- Жалобы: кнопка в ленте; >5 уникальных за 3 мес → `is_blocked`; разбан в админке с фото/анкетой.
- Запуск только Docker (без локального venv).
- По умолчанию polling; webhook — `USE_WEBHOOK=true` + HTTPS; хост-порт webhook **:8181** (внутри контейнера 8081).
- Вне FSM любое личное сообщение → главное меню (`handlers/fallback.py`).
- Терминальные ответы (оплата отмечена, премиум включён, пустая лента, soft-launch, список лайков, reengage, лимит) — с `main_menu_kb`.
- Массовых рассылок нет.
- Сообщения про лимит / like_sent слать через `bot.send_message(user_id)`, не через `callback.message.answer` (фото / InaccessibleMessage).

## Ловушки

- Код бота/веба в образе: после правок нужен `docker compose up -d --build` (volume только `./data`).
- Бот должен быть админом обязательных каналов.
- `callback_data` ≤ 64 байт.
- Nominatim только при сохранении гео.
- `session.get(User, …, options=[selectinload])` ненадёжен (identity map) — грузить через `load_user_with_profile` / `select`+`selectinload`.
- После `rollback` ORM-объекты expire → lazy load в async = MissingGreenlet; не трогать expired instance.
- Reengage: не слать тестовым (`is_test` / `tg_id<=0`).
- БД: в `.env` только `POSTGRES_*`; URL собирает `config/settings.py`.
- В `.env` без `$` — Compose портит пароль при подстановке.
- Webhook без публичного HTTPS не работает.
