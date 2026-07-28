# CLODE — vinchik

Telegram dating bot (aiogram 3.29) + FastAPI admin. Postgres, Redis FSM.

## Куда лезть

| Задача | Путь |
|--------|------|
| Точка входа бота | `bot.py` |
| Веб-админка | `web.py`, `webapp/` |
| Список аккаунтов (поиск/фильтры) | `webapp/` → `/accounts`, `services/accounts.py` |
| Карточка аккаунта (правка / лайки) | `/accounts/{tg_id}`, `update_account` / `clear_user_likes` |
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
- Лента: взаимный looking_for; сначала все круги км с возрастом **±2**, потом все круги с **±5**, затем ±10 / любой; внутри ячейки: премиум → |Δвозраст| → ближе км.
- После ❤️/👎/💌 анкета **больше не показывается** в ленте этому зрителю (строка в `likes`). 💤 кнопки снимает, **не** пишет в `likes`.
- Снова увидеть можно только если админ удалил лайки пары. Настройка `profile_reshow_days` в UI пока есть, на ленту не влияет.
- Реактивация: только через 1 / 3 / 7 суток после `last_activity_at` (не спамить).
- Симпатии: батч-уведомление ≤1/30 мин; матч не обязателен.
- Админка бота `/admin`: заявки Премиум, премиум-юзеры, soft-launch, настройки (лимит/радиус). Баны и оплата — веб `:8180`.
- Админка веб: карта оплаты, гео, тестовые юзеры, баны, `/accounts` (+ карточка `/accounts/{tg_id}`).
- Тестовые юзеры: `User.is_test`, негативные `tg_id`, фото `data/test.png`; видимость всех сразу — галочка в веб-админке (`is_active`).
- Жалобы: кнопка в ленте; >5 уникальных за 3 мес → `is_blocked`; разбан в админке с фото/анкетой.
- Запуск только Docker (без локального venv).
- По умолчанию polling; webhook — `USE_WEBHOOK=true` + HTTPS; хост-порт webhook **:8181** (внутри контейнера 8081).
- Вне FSM любое личное сообщение → главное меню (`handlers/fallback.py`).
- Терминальные ответы (оплата отмечена, премиум включён, пустая лента, soft-launch, список лайков, reengage, лимит) — с `main_menu_kb`.
- Массовых рассылок нет.

## Ловушки

- Бот должен быть админом обязательных каналов.
- `callback_data` ≤ 64 байт.
- Nominatim только при сохранении гео.
- `session.get(User, …, options=[selectinload])` ненадёжен (identity map) — грузить через `load_user_with_profile` / `select`+`selectinload`.
- После `rollback` ORM-объекты expire → lazy load в async = MissingGreenlet; не трогать expired instance.
- Reengage: не слать тестовым (`is_test` / `tg_id<=0`).
- БД: в `.env` только `POSTGRES_*`; URL собирает `config/settings.py`.
- В `.env` без `$` — Compose портит пароль при подстановке.
- Webhook без публичного HTTPS не работает.
