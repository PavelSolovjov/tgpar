# Telegram Jobs Monitor MVP

MVP читает новые посты из заданных Telegram-каналов, определяет вакансии Project/Delivery Manager в fintech/crypto/web3/payments/blockchain на middle/senior уровне и отправляет подходящие посты в целевой канал.

## Важное ограничение Telegram

Обычный Telegram Bot API не может читать произвольные каналы-источники. Поэтому проект использует:

- `Telethon` user session для чтения каналов, к которым имеет доступ ваш Telegram-аккаунт.
- Telegram bot token для публикации в целевой канал, если `forward_with_user: false`.

## Быстрый запуск

Нужен Python 3.9 или новее.

1. Создайте Telegram API credentials: `api_id` и `api_hash` на <https://my.telegram.org/apps>.
2. Создайте бота через `@BotFather` и добавьте его админом в целевой канал.
3. Установите зависимости:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install .
```

4. Создайте `.env`:

```bash
cp .env.example .env
```

5. Заполните `.env` и `config.yaml`.
6. Запустите:

```bash
tg-jobs-monitor
```

При первом запуске Telethon попросит код входа в Telegram для user session.

## Постоянный запуск через Docker

На сервере:

```bash
cp .env.example .env
# заполните .env и config.yaml
docker compose up -d --build
```

Контейнер использует `restart: unless-stopped`, хранит SQLite-базу в `./data` и Telegram-сессии в `./sessions`.

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
- `dry_run`: логировать решения без отправки.
- `forward_with_user`: настоящая пересылка через user client вместо публикации копии ботом.
