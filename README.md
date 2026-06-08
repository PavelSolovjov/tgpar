# Telegram Jobs Monitor MVP

MVP читает новые посты из публичных Telegram-каналов через веб-страницы `https://t.me/s/<channel>`, определяет вакансии Project/Delivery Manager в fintech/crypto/web3/payments/blockchain на middle/senior уровне и отправляет подходящие посты в целевой канал.

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
