# CLODE — vinchik

Telegram dating bot (aiogram 3.29) + FastAPI admin. Postgres, Redis FSM.

## Куда лезть

| Задача | Путь |
|--------|------|
| Точка входа бота | `bot.py` |
| Веб-админка | `web.py`, `webapp/`; шапка/навигация — `templates/_admin_topbar.html`, `_admin_nav.html`, `_switch_compact.html`; число+ползунок — `_field_slider.html` |
| Список аккаунтов (поиск/фильтры; `is_test` по умолч. `false`) | `webapp/` → `/accounts`, `services/accounts.py` |
| Карточка аккаунта (лайки∥премиум в ряд; лайки: реакции/отправленные/лимиты за сегодня UTC) | `/accounts/{tg_id}`; премиум активен только если `premium_until > now` (`is_premium`); снять → `2004-01-01` |
| Баны / подозрительные | `/bans`; `is_blocked` + `is_suspicious` + `suspicious_reason`; сообщения 💌 |
| Обязательные каналы | `services/channels.py`; веб `/channels`; бот `/admin` → 📢 Каналы (@ник / t.me / forward); юзер: Настройки → Каналы |
| Карта пользователей (≤200, random sample + админы красным; клик = центр спавна тестов; reload после create/clear; тестовые на карте по умолч. скрыты, чекбокс / `?include_test=1`) | `/accounts` (Leaflet), `GET /accounts/map-markers`, `map_markers(include_test=)` |
| Гео админа / тестовые юзеры | `services/admin_tools.py` (`create_test_users` + optional center_lat/lon), `services/media.py`; веб: тесты на `/accounts`, свитч видимости также в настройках дашборда |
| Трекинг-ссылки (deep-link + клики) | `services/tracking_links.py`, `handlers/admin_links.py`, веб `/links`; код `?start=` — явный или латиница из названия (не random); модели `TrackingLink` / `TrackingClick` |
| Справочник НП (текст+координаты) | `data/settlements/settlements.csv.gz` (**только TJ+RU**), `services/settlements*.py`, `scripts/build_settlements_dump.py` |
| Тестовое фото | `data/photos/men/`, `data/photos/women/` (по полу); fallback `data/test.png` |
| Роутеры | `handlers/` |
| Бизнес-логика | `services/` |
| Модели БД | `database/models.py` |
| Тексты UI | `locales/ru.py`, `locales/tg.py` (кнопка tg первой) |
| Клавиатуры | `keyboards/inline.py` |
| Конфиг | `config/settings.py`, `.env` (`ADM_LINK` — URL веб-админки в тексте `/admin`) |
| Нагрузочный тест / временные probes | `loadtest/`, `docker-compose.loadtest.yml`, `services/bot_factory.py`, `services/performance.py`, `middlewares/performance.py`; результаты/заметки — `ignored/loadtest/` |
| ТЗ | `TZ.md` |

## Инварианты

- Кнопки пользователя — inline (кроме request_location и **reply-клавиатуры ленты** ❤️💌👎 / ⚠️⭐🚪).
- Гео в анкете: GPS **или** текст → поиск по `settlements` (**только TJ+RU**). Алиасы (в т.ч. исторические) — только для поиска; в UI везде `display_name` (современное имя). Ранжирование: score → точное имя → population. Дамп `data/settlements/settlements.csv.gz`; после обновления: `docker compose exec bot python scripts/import_settlements.py`. Веб: `GET /settlements/search`.
- Без нумерации на кнопках.
- Лимит лайков: default 50, сутки **UTC**; у мужчин без Премиум при исчерпании **нельзя смотреть ленту**. Резерв слота + запись реакции — одна транзакция; реакции одного отправителя сериализуются блокировкой строки `users`.
- Женщины и Премиум — без лимита.
- Лента: взаимный looking_for; диагональ км×возраст: волна 0 = 10км±2; волна 1 = 10км±5 и 25км±2; волна 2 = 10км±10 + 25км±5 + 50км±2; … Каждый `next_profile` с нуля по текущим кандидатам. Внутри волны: премиум → |Δвозраст| → ближе км. Цель карточки в FSM `BrowseStates.viewing` (`browse_target`).
- Радиус ленты: default и жёсткий максимум **500 км** во всех точках ввода и чтения настройки.
- После ❤️/👎/💌 пара **взаимно** скрывается из ленты на `profile_reshow_days` (default **60**; **0** = навсегда). 🚪 снимает reply-клавиатуру, **не** пишет в `likes`.
- 💌: одно сообщение — текст **или** голосовое **или** кружок (без вложений); payload в `likes.message_payload` (JSONB); промпт + «Отмена» (`msg:cancel`) — **одним** сообщением (reply-клавиатура ленты снимается служебным удаляемым сообщением); после успешной отправки / отмены inline «Отмена» снимается с промпта (`msg_prompt_message_id`).
- Анкета: **1–3 фото** обязательно (`photo_file_ids` JSONB + `photo_file_id` = первое); кнопка «Без фото» нет; в ленте album / одно фото (текст без фото — только старые анкеты).
- Реклама Премиум (`premium_benefits`): после регистрации сначала `premium_promo`, через **5 сек** — первая анкета ленты; при лимите лайков (+ кнопка ⭐), в меню «Премиум».
- После истечения окна анкета может снова попасть в ленту; новая реакция обновляет ту же строку `likes`.
- Отключённая анкета (`is_active=False` после 🚫 или из админки) **не включается сама**: при заходе в ленту бот спрашивает и ждёт кнопку «Включить анкету» (`profile:enable`).
- Проверка обязательных каналов: недоступный канал (бот не админ / канал удалён / сбой API) **пропускается** с логом, пользователя не блокирует; отказ только когда Telegram явно говорит, что юзера нет в канале.
- Реактивация: только через 1 / 3 / 7 суток после `last_activity_at` (не спамить).
- Симпатии: батч-уведомление — новое сообщение ≤1/30 мин; внутри окна текст того же сообщения обновляется (счётчик N), без тишины. «Посмотреть» → очередь карточек лайкнувших (`browse_source=likes`) с ❤️/👎/💌 как в ленте; `is_seen` при показе карточки; взаимный лайк → ссылка `t.me/username` (если есть).
- Админка бота `/admin`: в корне — счётчики парней/девушек/всего + ссылка из `ADM_LINK` на веб; заявки по одной со счётчиком; премиум-юзеры с датой + листание; **🔗 Ссылки** (CRUD + `t.me/bot?start=code` в `<code>` + статистика переходов); soft-launch 🟢/🔴; настройки (лимит, радиус, повтор, карта, время проверки, менеджер, контакт поддержки, **приветственный пост**); **обязательные каналы** (список / вкл-выкл / удалить / добавить по @нику, t.me-ссылке или пересланному сообщению; перед добавлением — бот должен быть админом канала). Баны — веб `/bans`. Кнопки на пуше чека — `adm:rok` / `adm:rno` (только закрыть заявку на месте); очередь «следующая / нет заявок» — только `adm:ok` / `adm:no` из `/admin` → Заявки.
- Веб «Основное» (`/`): в шапке на всех страницах — счётчики по полу; настройки (компактные свитчи soft-launch / тестовые в ленте; лимиты; оплата; welcome; гео). Отдельные страницы: `/accounts` (+ тестовые), `/channels`, `/links` (диаграмма + пресеты/календарь), `/premium` (заявки + активные), `/bans`. POST форм — AJAX (toast). Карточка аккаунта: в шапке главный сигнал — `tg_id`; пол/ищет — розово-голубые сегменты; флаги — цветные toggle; теги с пиктограммами. Анкета — серый блок; клик по панели целиком снимает серость и открывает все поля; username read-only; язык в анкете.
- Обязательные каналы: бесплатный юзер перед лентой видит список + кнопки «Я подписался» | «Премиум»; премиум — без проверки; в Настройках — просмотр списка. Бот должен быть админом каналов (`get_chat_member`).
- Админка веб: карта оплаты, гео, тестовые юзеры (`/accounts`), `/bans`, `/accounts/{tg_id}`, `/channels`, `/links`, `/premium`, контакт поддержки (`support_contact`), **приветственный пост** (`welcome_photo_file_id` + `welcome_text`; превью `/settings/welcome/photo`).
- Тестовые юзеры: `User.is_test`, негативные `tg_id`; создание до **1000** за раз + **макс. радиус спавна** (км) в веб-форме; фото случайно из `data/photos/men|women` по полу (`local:`), одинаковые файлы стараются не ставить ближе ~8 км; fallback `data/test.png`; свич «Тестовые в ленте» = setting `test_users_visible` + массовый `Profile.is_active`. Числовые поля админки — number + прогрессивный горизонтальный ползунок (`_field_slider.html`).
- Жалобы: кнопка в ленте; >5 уникальных за 3 мес → `is_blocked`; в тексте бана — контакт поддержки из настроек; разбан в админке с фото/анкетой.
- «Поделиться ботом» — `t.me/share/url?url=…&text=…` (диалог шаринга), не прямой чат с ботом.
- Неполная анкета при `browse:start` → `begin_profile_flow` (FSM возраста), не голый `ask_age` с меню.
- Подозрительные (тихо): >150 лайков/сутки UTC, >150 сообщений/сутки, >3 раскладки в одном 💌, ≥2 жалобы за 3 мес → `is_suspicious` + `suspicious_reason`; фон `moderation_loop` + хук после лайка/жалобы; страница `/bans`.
- Запуск только Docker (без локального venv).
- По умолчанию polling; webhook — `USE_WEBHOOK=true` + HTTPS; хост-порт webhook **:8181** (внутри контейнера 8081).
- Без домена (типичный polling): веб-админка по IP — `WEB_PUBLISH=0.0.0.0:8080`, `WEB_ROOT_PATH=` (пусто), `ADM_LINK=http://IP:8080`. С доменом/nginx (+ Let's Encrypt): `WEB_PUBLISH=127.0.0.1:8180`, `WEB_ROOT_PATH=/vinchik`, `ADM_LINK=https://krigz.com/vinchik`; сниппет — `deploy/nginx-vinchik.snippet.conf`.
- Нагрузочный контур: `TELEGRAM_API_BASE_URL` переключает aiogram на mock,
  `WEBHOOK_HANDLE_IN_BACKGROUND=false` измеряет полный handler,
  `PERFORMANCE_METRICS_ENABLED=true` включает `/__performance__/*`; в production
  custom URL и probes выключены. Жёсткий профиль сидов: кандидаты
  `tg_id≥9100000000` / `is_test=False` (notify), 1–3 fake photo file_id,
  3 канала, у bot `CHANNEL_MEMBERSHIP_CACHE_SECONDS=0`, ступени **5/10/15/25**
  по 20 с; delivery `LOADTEST_DELIVERY=webhook|polling` (+ `LOADTEST_USE_WEBHOOK`).
- Защита hot path: одновременно выполняется не больше
  `UPDATE_CONCURRENCY_LIMIT` handlers (default 24); положительная проверка
  обязательных каналов кэшируется на `CHANNEL_MEMBERSHIP_CACHE_SECONDS`
  (default 300), список каналов — на `ACTIVE_CHANNELS_CACHE_SECONDS` (default
  30). Изменение каналов в bot-процессе сбрасывает оба кэша; изменение через web
  видно bot-процессу после TTL списка.
- `last_activity_at` записывается не чаще раза в 5 минут; реакции обновляют его
  в своей транзакции. Hot path ❤️/👎 не перезагружает пользователя и лимит
  повторно после успешной реакции.
- Батч-уведомление получателю лайка запускается после commit в отдельной задаче
  со своей DB-session и не задерживает выдачу следующей карточки отправителю.
  Входящий `NOT EXISTS` ленты поддерживает индекс
  `ix_likes_to_from_created (to_user_id, from_user_id, created_at)`.
- `/start` (`handlers/start.py`): аргумент deep-link (`?start=code`) пишет клик в `tracking_clicks` если код есть; если задан приветственный пост (фото) — он вместо `welcome` / `welcome_bilingual`; иначе тексты из локалей; выбор языка только если `language_chosen=False`; иначе продолжение регистрации / меню. У готовой анкеты — `main_menu_help` (что делают 6 кнопок) + меню. Флаг ставится в `set_language`; у готовых анкет — автозаполнение для старых строк.
- Вне FSM любое личное сообщение → главное меню (`handlers/fallback.py`).
- Хендлеры не используют `assert` для входных данных: юзер берётся через `callback_context` / `message_user` / `ensure_user` (`handlers/common.py`), они же чинят пропавшую строку `users`. Протухшая кнопка (нет тела сообщения) → алерт `stale_button`. В админке экран перерисовывает `_redraw` (правка на месте, иначе новое сообщение).
- Терминальные ответы (оплата, премиум, пустая лента → `empty_feed_kb`, soft-launch, лайки, reengage, лимит) — с inline-меню; заголовок хаба — `main_menu_title` (не «☰»). Снятие reply-kb — `drop_reply_keyboard` (без «.» / «OK» / «🚪»).
- Оплата Премиум: кнопка «Отправить чек» → FSM `PremiumStates.awaiting_receipt` (фото/документ + «Отмена», кнопка снимается после файла); `receipt_file_id`/`receipt_kind` в `premium_orders`; одна pending-заявка на пользователя (повтор «Купить» возвращает её); пересылка админам; веб `/orders/{id}/receipt` + превью на `/premium`.
- Массовых рассылок нет.
- Сообщения про лимит / like_sent слать через `bot.send_message(user_id)`, не через `callback.message.answer` (фото / InaccessibleMessage). Последний лайк дня: одно сообщение «лайк отправлен» + текст лимита.

## Ловушки

- Код бота/веба в образе: после правок нужен `docker compose up -d --build` (volume только `./data`).
- Бот должен быть админом обязательных каналов.
- `callback_data` ≤ 64 байт.
- Nominatim только при сохранении GPS-гео (текстовый ввод берёт координаты из `settlements`).
- Дамп населённых пунктов лежит в `data/` (volume) — без `settlements.csv.gz` текстовый поиск гео не работает.
- `session.get(User, …, options=[selectinload])` ненадёжен (identity map) — грузить через `load_user_with_profile` / `select`+`selectinload`.
- После `rollback` / `commit` ORM-объекты expire → lazy load в async = MissingGreenlet; не трогать expired instance. `start_browse` всегда перезагружает user+profile через `load_user_with_profile`.
- Лента выбирает карточку одним SQL: bbox → точная дистанция → диагональная волна; реакции исключаются через `NOT EXISTS`. Feed/likes/reports/reengage индексы создаются в `init_db`.
- Настройки БД кэшируются в процессе на 30 сек (между bot/web допустима такая задержка); пул каждого процесса задаётся `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT_SECONDS`, `DB_POOL_RECYCLE_SECONDS`.
- Reengage: не слать тестовым (`is_test` / `tg_id<=0`).
- Лента: кнопка на старой карточке после удаления тестовых → `record_action` проверяет, что `to_user` ещё есть (иначе None, без IntegrityError); lock отправителя — с `selectinload(User.profile)`.
- БД: в `.env` только `POSTGRES_*`; URL собирает `config/settings.py`.
- В `.env` без `$` — Compose портит пароль при подстановке.
- Webhook без публичного HTTPS не работает.
- Rate-limit входа в веб берёт `X-Real-IP` только от сетей `WEB_TRUSTED_PROXY_IPS`; для nginx на Docker-host добавь его bridge CIDR (обычно `172.16.0.0/12`).
- Порт web задаётся `WEB_PUBLISH` (default `127.0.0.1:8180` под nginx). Webhook-listener бота по-прежнему `127.0.0.1:8181:8081`.
- Альбом фото приходит несколькими апдейтами параллельно: правки `draft_photos` в FSM только под `_photo_draft_lock` (`handlers/profile.py`).
- `callback.message.from_user` — это **бот**, а не пользователь; ник берём из `callback.from_user`.
- `callback.message` у кнопки старше 48 ч — это `InaccessibleMessage` (нет ни `.text`, ни `.from_user`), поэтому проверка идёт через `isinstance(..., Message)`, а не `if callback.message`.
- Протухший Telegram `file_id` фото (смена токена бота / удаление файла) → `wrong file identifier`; `show_my_profile` / лента ловят `TelegramBadRequest` и показывают анкету текстом.
