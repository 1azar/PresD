# Конфигурация

Шаблон настроек находится в [`webapp/.env.example`](../webapp/.env.example).
Для локальной или production-инсталляции скопируйте его в `webapp/.env` и не
добавляйте получившийся файл в Git.

## Обязательные настройки Compose

| Переменная | Назначение |
| --- | --- |
| `POSTGRES_PASSWORD` | Пароль пользователя PostgreSQL; используйте ASCII-буквы и цифры |
| `REDIS_PASSWORD` | Отдельный пароль Redis |
| `WEBAPP_ALLOWED_CLIENTS_JSON` | JSON-объект `логин → пароль` в одну строку |
| `PUBLIC_IP` | Публичный IPv4 ВМ; для локального запуска — `127.0.0.1` |
| `TLS_EMAIL` | Email для уведомлений Let's Encrypt; локально certbot не запускается |
| `LLM_BASE_URL` | Базовый URL OpenAI-compatible API |
| `LLM_MODEL` | Точный идентификатор текстовой модели у провайдера |

Пример списка пользователей:

```dotenv
WEBAPP_ALLOWED_CLIENTS_JSON={"alice":"long-random-password","bob":"another-long-password"}
```

Логин после удаления из JSON больше не может войти. Изменение пароля в JSON
потребует следующего входа с новым паролем. В базе пароль хранится только как
Argon2id-хеш; исходное значение остаётся в `.env` открытым текстом.

Логин должен содержать от 3 до 80 символов, а пароль — от 8 до 200 символов.

## Текстовая модель

```dotenv
LLM_BASE_URL=http://host.docker.internal:11434/v1
LLM_MODEL=your-model
LLM_API_KEY=not-needed
LLM_PROJECT=
LLM_API_MODE=chat_completions
LLM_STRUCTURED_OUTPUT=native
LLM_TIMEOUT=90
```

| Переменная | Значения и поведение |
| --- | --- |
| `LLM_API_KEY` | Может быть пустой для локального сервера без авторизации |
| `LLM_PROJECT` | Необязательный идентификатор проекта провайдера |
| `LLM_API_MODE` | `chat_completions` или `responses` |
| `LLM_STRUCTURED_OUTPUT` | `native` или `prompt` |
| `LLM_TIMEOUT` | Таймаут одного обращения, 1–300 секунд; по умолчанию 90 |

Используйте `LLM_STRUCTURED_OUTPUT=prompt`, если endpoint не принимает native JSON
Schema. В этом режиме схема добавляется в prompt без специального API-параметра.

Для Python CLI, запущенного прямо на хосте, локальный endpoint обычно имеет адрес
`http://localhost:11434/v1`. Worker-контейнеры должны обращаться к хосту через
`http://host.docker.internal:11434/v1`.

## Визуальная модель

```dotenv
VLM_BASE_URL=
VLM_MODEL=
VLM_API_KEY=
VLM_PROJECT=
VLM_API_MODE=
VLM_TIMEOUT=
```

Пустые `VLM_*` наследуют соответствующие `LLM_*`. Укажите их явно, если для
изображений нужен другой endpoint, модель, ключ, проект, API mode или таймаут.
Если выбранная модель не принимает изображения, отключите VLM при загрузке или
переанализе шаблона.

## Планирование и таймауты

| Переменная | По умолчанию | Допустимый диапазон / назначение |
| --- | ---: | --- |
| `WEBAPP_PLANNER_MAX_WORKERS` | `4` | 1–8 одновременных LLM-вызовов |
| `WEBAPP_PLANNER_STRICT_BUDGET` | `300` | Общий бюджет strict, 60–600 с |
| `WEBAPP_PLANNER_FAST_BUDGET` | `135` | Общий бюджет fast, 30–300 с |
| `WEBAPP_PLANNER_OUTLINE_WAVE_BUDGET` | `105` | Волна outline, 10–300 с |
| `WEBAPP_PLANNER_SLIDE_WAVE_BUDGET` | `105` | Волна генерации слайдов, 10–300 с |
| `WEBAPP_PLANNER_CRITIQUE_WAVE_BUDGET` | `75` | Волна критики, 10–300 с |
| `WEBAPP_GENERATION_JOB_TIMEOUT` | `420` | Защитный timeout задания, 120–1200 с |
| `WEBAPP_TEMPLATE_ENRICHMENT_WORKERS` | `4` | 1–8 параллельных VLM-анализов |
| `WEBAPP_TEMPLATE_VLM_TIMEOUT` | `90` | Таймаут одного VLM-запроса, 10–600 с |

Увеличение числа workers ускоряет независимые запросы, но повышает параллельную
нагрузку и вероятность rate limit со стороны провайдера.

## Внутренние настройки web-приложения

Compose задаёт эти переменные автоматически; менять их обычно не требуется:

| Переменная | Назначение |
| --- | --- |
| `WEBAPP_DATABASE_URL` | SQLAlchemy URL PostgreSQL или SQLite для тестов |
| `WEBAPP_REDIS_URL` | URL Redis с паролем и номером базы |
| `WEBAPP_DATA_ROOT` | Корень пользовательского файлового хранилища |
| `WEBAPP_ALLOWED_ORIGINS` | Разрешённые Origin для изменяющих запросов |
| `WEBAPP_COOKIE_SECURE` | Передавать session cookie только по HTTPS |
| `WEBAPP_WORKER_QUEUE` | Очередь конкретного worker-контейнера |

Ограничения загрузок по умолчанию:

- шаблон — 100 МБ;
- один дополнительный файл — 25 МБ;
- все дополнительные файлы задания — 100 МБ;
- извлечённый текст — 50 000 символов.

## Проверка

Перед запуском проверяйте итоговую Compose-конфигурацию:

```bash
docker compose \
  --env-file webapp/.env \
  -f webapp/compose.yaml \
  -f webapp/compose.local.yaml \
  config --quiet
```

Для production уберите `webapp/compose.local.yaml` из команды.
