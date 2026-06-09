# Telegram Jobs Monitor MVP

MVP читает новые посты из публичных Telegram-каналов через веб-страницы `https://t.me/s/<channel>`, а также может забирать вакансии с `hh.ru` через официальный API поиска вакансий. Затем он определяет PM-вакансии, считает fit с резюме и отправляет подходящие посты в целевой канал.

## Важное ограничение

Этот режим не требует `api_id` и `api_hash` с `my.telegram.org`, но работает только с публичными каналами, доступными по `https://t.me/s/<channel>`.

Для публикации подходящих вакансий нужен обычный Telegram bot token. Бота нужно добавить админом в целевой канал.

## Быстрый запуск

Нужен Python 3.9 или новее.

1. Создайте бота через `@BotFather` и добавьте его админом в целевой канал.
2. Установите зависимости:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install .
```

3. Создайте `.env`:

```bash
cp .env.example .env
```

4. Заполните `.env` и `config.yaml`.
5. Запустите:

```bash
tg-jobs-monitor
```

При первом запуске существующие посты будут обработаны и сохранены в SQLite, но не опубликованы, если `publish_on_first_run: false`.

## Постоянный запуск через GitHub Actions

Для GitHub Actions используются два workflow:

- `.github/workflows/hourly-monitor.yml` -> группа A, `CONFIG_PATH=config-a.yaml`
- `.github/workflows/hourly-monitor-b.yml` -> группа B, `CONFIG_PATH=config-b.yaml`

Обычно их удобно запускать внешним cron-триггером с разницей в 30 минут:

- workflow A примерно в `08:14`, `09:14`, ... `23:14` по Москве
- workflow B примерно в `08:44`, `09:44`, ... `23:44` по Москве

Перед включением добавьте в GitHub repository secrets:

- `TELEGRAM_BOT_TOKEN`: token бота от `@BotFather`.
- `OPENAI_API_KEY`: необязательно, если нужен LLM-анализ.
- `HH_USER_AGENT`: строка для HH API в формате вроде `TGPar/1.0 (you@example.com)`.
- `RESUME_TEXT`: текст резюме, если нужно сравнение вакансии с вашим профилем и короткое summary перед публикацией.

Опционально можно добавить repository variable:

- `OPENAI_MODEL`: например `gpt-4.1-mini`.

SQLite-база дедупликации хранится в GitHub Actions cache (`data/processed.sqlite3`). Первый запуск помечает уже видимые старые посты как обработанные и не публикует их, если `publish_on_first_run: false`.

Запустить вручную можно в GitHub:

```text
Actions -> Telegram jobs monitor A -> Run workflow
Actions -> Telegram jobs monitor B -> Run workflow
```

## Постоянный запуск через Docker

На сервере:

```bash
cp .env.example .env
# заполните .env и config.yaml
docker compose up -d --build
```

Контейнер использует `restart: unless-stopped` и хранит SQLite-базу в `./data`.

## Постоянный запуск через systemd

Пример unit-файла лежит в `deploy/systemd/telegram-jobs-monitor.service`.

Типовой сценарий:

```bash
sudo mkdir -p /opt/telegram-jobs-monitor
sudo cp -R . /opt/telegram-jobs-monitor
cd /opt/telegram-jobs-monitor
python3 -m venv .venv
source .venv/bin/activate
pip install .
sudo cp deploy/systemd/telegram-jobs-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-jobs-monitor
```

## LLM-анализ

Если `OPENAI_API_KEY` задан и `llm.enabled: true`, бот просит LLM вернуть строго JSON-решение:

- является ли сообщение вакансией;
- соответствует ли роль;
- соответствует ли сфера;
- соответствует ли уровень;
- причина принятия или отклонения.

Если ключа нет или LLM недоступна, используется осторожная эвристика по ключевым словам. В спорных случаях пост отклоняется с причиной в логах.

Если задан `RESUME_TEXT` или локальный `resume.md`, LLM также сравнивает вакансию с резюме и добавляет короткое summary в публикуемый пост.

## Дедупликация

SQLite хранит:

- источник и ID сообщения;
- хеш текста для защиты от дублей между каналами;
- решение фильтра;
- причину решения;
- ID пересланного/созданного сообщения.

База по умолчанию: `data/processed.sqlite3`.

## Настройка

Главные параметры в `config.yaml`:

- `source_channels`: список каналов-источников.
- `destination_channel`: канал для подходящих вакансий.
- `criteria.roles/domains/levels`: критерии фильтрации.
- `poll_interval_seconds`: период проверки.
- `recent_messages_limit`: сколько последних сообщений смотреть на первом проходе.
- `publish_on_first_run`: публиковать ли подходящие старые посты при первом запуске.
- `dry_run`: логировать решения без отправки.

Для split-запуска через GitHub Actions используются `config-a.yaml` и `config-b.yaml` с теми же параметрами, но разными группами каналов.

## HH.ru

Для поиска вакансий на `hh.ru` используется официальный API вакансий HeadHunter. По данным официальной документации HH, если приложение использует только поиск вакансий, отдельная авторизация пользователя на hh.ru не требуется, но запросы должны содержать `User-Agent` / `HH-User-Agent`. Источники: [HeadHunter API](https://dev.hh.ru/), [OpenAPI docs](https://api.hh.ru/openapi/redoc).

Параметры HH находятся в секции `hh`:

- `enabled`: включить поиск по HH
- `per_page`: сколько вакансий брать за один запрос
- `pages`: сколько страниц пройти
- `request_delay_seconds`: пауза между HH-поисками
- `searches`: список поисковых запросов и источников

В split-режиме HH сейчас включен только в `config-a.yaml`, чтобы не было дублей и лишней нагрузки.
