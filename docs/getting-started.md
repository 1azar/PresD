# Локальный запуск

Основной способ локального запуска PresD — Docker Compose. Все runtime-зависимости,
включая Python, Node.js, PostgreSQL, Redis, LibreOffice и Poppler, находятся внутри
контейнеров.

## Требования

- Docker Engine или Docker Desktop;
- Docker Compose v2;
- OpenAI-compatible API с текстовой моделью;
- для визуального анализа — модель с поддержкой изображений либо отключённая VLM.

Проверить Compose:

```bash
docker compose version
```

## 1. Подготовка окружения

Из корня репозитория создайте локальный файл настроек:

```bash
cp webapp/.env.example webapp/.env
```

Минимальный пример для разработки:

```dotenv
POSTGRES_PASSWORD=localPostgresPassword123
REDIS_PASSWORD=localRedisPassword456
WEBAPP_ALLOWED_CLIENTS_JSON={"admin":"localUiPassword789"}

PUBLIC_IP=127.0.0.1
TLS_EMAIL=local@example.com

LLM_BASE_URL=http://host.docker.internal:11434/v1
LLM_MODEL=your-model
LLM_API_KEY=not-needed
LLM_API_MODE=chat_completions
LLM_STRUCTURED_OUTPUT=native
LLM_TIMEOUT=90
```

Замените демонстрационные пароли перед использованием вне локального компьютера.
Файл `webapp/.env` содержит секреты и не должен попадать в Git.

Если Ollama, vLLM или LM Studio запущен на хосте, контейнеры обращаются к нему по
имени `host.docker.internal`. Модельный сервер должен принимать соединения от
Docker, а не только со своего loopback-интерфейса. Открывать его порт во внешний
firewall не требуется.

## 2. Проверка конфигурации

```bash
docker compose \
  --env-file webapp/.env \
  -f webapp/compose.yaml \
  -f webapp/compose.local.yaml \
  config --quiet
```

Команда завершается без вывода, если YAML и обязательные переменные корректны.

## 3. Запуск

```bash
docker compose \
  --env-file webapp/.env \
  -f webapp/compose.yaml \
  -f webapp/compose.local.yaml \
  up --build postgres redis api generation-worker template-analysis-worker template-enrichment-worker frontend
```

Локальный override публикует только frontend на `127.0.0.1:8080` и отключает
HTTPS-only cookie. Production gateway и certbot при этом не запускаются.

Откройте <http://localhost:8080> и войдите под логином и паролем из
`WEBAPP_ALLOWED_CLIENTS_JSON`.

## 4. Первая презентация

1. Откройте «Шаблоны» и загрузите PPTX с нужным дизайном.
2. Дождитесь окончания структурного анализа и построения каталога.
3. На странице «Создать» введите бриф и добавьте текст или файлы.
4. Выберите готовый шаблон и диапазон количества слайдов.
5. При необходимости включите быстрый режим и запустите генерацию.
6. На странице задания просмотрите события и превью, затем скачайте PPTX или PDF.

Поддерживаемые материалы: TXT, Markdown, PDF, DOCX, CSV, XLSX, JSON, PNG и JPEG.
За один запуск web-интерфейс принимает до десяти дополнительных файлов.

## Проверка и диагностика

Health check API:

```bash
curl http://localhost:8080/api/health
```

Состояние контейнеров:

```bash
docker compose \
  --env-file webapp/.env \
  -f webapp/compose.yaml \
  -f webapp/compose.local.yaml \
  ps
```

Логи generation worker:

```bash
docker compose \
  --env-file webapp/.env \
  -f webapp/compose.yaml \
  -f webapp/compose.local.yaml \
  logs -f generation-worker
```

Если интерфейс открылся, но генерация завершается ошибкой, в первую очередь
проверьте доступность `LLM_BASE_URL` из worker-контейнера, точность `LLM_MODEL` и
поддержку выбранного `LLM_API_MODE`.

## Остановка

Остановить контейнеры, сохранив базу и пользовательские файлы:

```bash
docker compose \
  --env-file webapp/.env \
  -f webapp/compose.yaml \
  -f webapp/compose.local.yaml \
  down
```

Не добавляйте `-v`, если не хотите удалить PostgreSQL, Redis, пользовательские
файлы и другие volumes.

## Дальше

- [Конфигурация](configuration.md)
- [Архитектура](architecture.md)
- [Разработка и тестирование](development.md)
