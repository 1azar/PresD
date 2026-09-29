# Разработка и тестирование

## Структура репозитория

```text
content_planner/       построение каталога и контент-плана
presentation_builder/ детерминированная сборка PPTX и QA
template_model/       разбор PPTX и VLM-описание слайдов
webapp/backend/       FastAPI, SQLAlchemy, RQ workers
webapp/frontend/      React, TypeScript, Vite
tests/                тесты ядра pipeline
docs/                 документация проекта
```

## Python-окружение

Рекомендуется Python 3.12 и отдельное виртуальное окружение:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r webapp/backend/requirements-dev.txt
```

Для создания PDF и превью вне Docker также нужны LibreOffice Impress и
`pdftoppm` из Poppler. Без них структурные этапы могут работать, но рендеринг и
часть QA будут недоступны или вернут предупреждение.

## Тесты Python

Все тесты:

```bash
pytest
```

Только ядро:

```bash
pytest tests
```

Backend с временной SQLite-базой:

```bash
WEBAPP_DATABASE_URL=sqlite+pysqlite:///./webapp-test.db \
WEBAPP_DATA_ROOT=/tmp/presd-test \
pytest webapp/backend/tests
```

Backend-тесты подменяют очередь синхронной реализацией и не требуют запущенных
PostgreSQL и Redis.

## Frontend

```bash
cd webapp/frontend
npm install
npm test
npm run build
```

Основные команды из `package.json`:

| Команда | Назначение |
| --- | --- |
| `npm run dev` | Vite development server |
| `npm test` | Unit/component tests через Vitest |
| `npm run build` | TypeScript check и production build |
| `npm run test:e2e` | End-to-end тесты Playwright |

Для полного локального приложения предпочтителен Docker Compose из раздела
[«Локальный запуск»](getting-started.md): frontend должен взаимодействовать с API,
Redis workers и хранилищем.

## Работа с CLI

Каждый этап ядра имеет собственную точку входа:

```bash
python -m template_model --help
python -m content_planner --help
python -m presentation_builder --help
```

Полный пример команд описан в [пайплайне генерации](generation-pipeline.md).

## Правила изменения проекта

- Не коммитьте `.env`, API-ключи, пароли и пользовательские презентации.
- При изменении формата каталога или плана синхронно обновляйте модели,
  валидаторы, builder и тестовые fixtures.
- Изменения API сопровождайте backend-тестами и обновлением frontend types.
- Изменения пользовательского сценария покрывайте component или E2E-тестом.
- Новую переменную окружения добавляйте в `.env.example`, Compose и
  [документацию конфигурации](configuration.md).
- Обновляйте документацию в том же Pull Request, что и связанный код.

## Проверки перед Pull Request

Минимальный набор:

```bash
pytest

cd webapp/frontend
npm test
npm run build
```

Если менялись Compose-файлы или переменные окружения:

```bash
docker compose \
  --env-file webapp/.env.example \
  -f webapp/compose.yaml \
  -f webapp/compose.local.yaml \
  config --quiet
```
