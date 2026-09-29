# Presentation Studio

Изолированный web-интерфейс для существующего пайплайна `template_model → content_planner → presentation_builder`.

## Запуск на ВМ

На ВМ нужны Git, Docker Engine и Compose plugin. Python и `venv` на хосте не
нужны: все зависимости собираются в Docker-образы.

1. Клонируйте репозиторий и создайте локальный файл секретов:

   ```bash
   git clone <URL-репозитория> presd
   cd presd
   cp webapp/.env.example webapp/.env
   chmod 600 webapp/.env
   ```

2. Отредактируйте `webapp/.env`. Обязательно задайте:

   - разные случайные `POSTGRES_PASSWORD` и `REDIS_PASSWORD` из ASCII-букв и цифр;
   - `WEBAPP_ALLOWED_CLIENTS_JSON` с разрешёнными логинами и паролями;
   - статический публичный IPv4 ВМ в `PUBLIC_IP`;
   - email для уведомлений Let's Encrypt в `TLS_EMAIL`;
   - адрес OpenAI-compatible API и точный идентификатор модели в `LLM_BASE_URL`
     и `LLM_MODEL`; для облачного API также задайте `LLM_API_KEY`.

   Список клиентов — JSON-объект в одну строку:

   ```dotenv
   WEBAPP_ALLOWED_CLIENTS_JSON={"alice":"long-random-password","bob":"another-long-password"}
   ```

   Поддерживаются `LLM_API_MODE=chat_completions` и `responses`. По умолчанию
   используется более широко совместимый `chat_completions`. Если endpoint не
   принимает native JSON Schema, задайте `LLM_STRUCTURED_OUTPUT=prompt`.
   `LLM_PROJECT` необязателен, а локальный сервер без авторизации может
   использовать `LLM_API_KEY=not-needed`.

   Для визуального анализа можно задать отдельные `VLM_BASE_URL`, `VLM_MODEL`,
   `VLM_API_KEY`, `VLM_PROJECT`, `VLM_API_MODE` и `VLM_TIMEOUT`. Пустые значения
   наследуют соответствующие `LLM_*`. Если выбранная модель не поддерживает
   изображения, отключите VLM при загрузке шаблона или настройте `VLM_MODEL`.

   Самостоятельной регистрации нет. При первом успешном входе аккаунт
   создаётся автоматически. Удаление логина из JSON отзывает доступ, а изменение
   пароля потребует нового входа с этим паролем.

3. Проверьте конфигурацию и запустите сервисы:

   ```bash
   docker compose --env-file webapp/.env -f webapp/compose.yaml config --quiet
   docker compose --env-file webapp/.env -f webapp/compose.yaml up -d --build
   ```

   Проверка выпуска сертификата и состояния сервисов:

   ```bash
   docker compose --env-file webapp/.env -f webapp/compose.yaml ps
   docker compose --env-file webapp/.env -f webapp/compose.yaml logs certbot
   curl -I http://<PUBLIC_IP>
   curl https://<PUBLIC_IP>/api/health
   ```

4. После нового `git pull` пересоберите и перезапустите контейнеры:

   ```bash
   docker compose --env-file webapp/.env -f webapp/compose.yaml up -d --build --remove-orphans
   ```

Первый выпуск сертификата может занять несколько минут. До его получения gateway
отвечает `503` и не допускает вход по незащищённому HTTP. После выпуска HTTP перенаправляется
на HTTPS. Короткоживущий IP-сертификат Let's Encrypt автоматически проверяется каждые 12 часов;
gateway подхватывает обновление не позднее чем через минуту.

### Сетевая изоляция

Наружу публикуются только `80/tcp` и `443/tcp` gateway-контейнера. Frontend, API,
PostgreSQL и Redis не имеют опубликованных портов. API и хранилища находятся в
Docker-сетях с `internal: true`. Только worker-контейнеры имеют исходящий доступ
к настроенному модельному API. Certbot имеет исходящий доступ для Let's Encrypt.

В cloud firewall/security group откройте:

- `80/tcp` для всех — нужен для ACME-проверки и HTTPS redirect;
- `443/tcp` для всех — web UI;
- `22/tcp` только с вашего доверенного IP — SSH.

Все остальные входящие порты закройте. Не публикуйте Docker API (`2375/2376`),
PostgreSQL (`5432`) или Redis (`6379`). Перед ограничением SSH не закрывайте текущую
SSH-сессию и проверьте вход во второй.

Пользовательские файлы, база и TLS-сертификаты хранятся в Docker volumes и не пропадают
при обновлении контейнеров. Команда `docker compose down -v` удалит эти данные и для
обычной остановки использоваться не должна.

Пароли в `WEBAPP_ALLOWED_CLIENTS_JSON` хранятся в `.env` в открытом виде по требованию формата,
поэтому не коммитьте этот файл и оставляйте ему права `600`. В PostgreSQL пароли UI
сохраняются только в виде Argon2id-хешей.

## Разработка и тесты

### Локальный запуск в Docker

Локальный override публикует frontend только на loopback-интерфейсе и отключает
HTTPS-only cookie. Gateway и certbot при таком запуске не используются. В локальном
`webapp/.env` задайте `PUBLIC_IP=127.0.0.1`, затем запустите:

```bash
docker compose \
  --env-file webapp/.env \
  -f webapp/compose.yaml \
  -f webapp/compose.local.yaml \
  up --build postgres redis api generation-worker template-analysis-worker frontend
```

Интерфейс будет доступен по адресу <http://localhost:8080>. Для запуска на ВМ
используйте только `webapp/compose.yaml` и укажите в `.env` публичный IPv4 — так
останутся включены gateway, HTTPS и certbot.

Если Ollama, vLLM или LM Studio запущен на хосте, укажите, например,
`LLM_BASE_URL=http://host.docker.internal:11434/v1`. Compose добавляет это имя
хоста worker-контейнерам и на Linux. Модельный сервер должен принимать соединения
не только с собственного loopback-интерфейса; не открывайте его порт во внешний
firewall.

Backend использует отдельные зависимости из `webapp/backend/requirements-dev.txt`; для тестов можно задать SQLite:

```bash
WEBAPP_DATABASE_URL=sqlite+pysqlite:///./webapp-test.db WEBAPP_DATA_ROOT=/tmp/presd-test pytest webapp/backend/tests
```

Frontend:

```bash
cd webapp/frontend
npm install
npm test
npm run build
```

E2E-настройка Playwright включает Chromium, Firefox и WebKit. Режим генерации
`strict` используется по умолчанию: он строит один общий outline, генерирует
слайды параллельно и запускает три профильные проверки качества. Общий бюджет
планирования — 300 секунд, защитный timeout worker — 420 секунд. Волны outline
и слайдов ограничены 105 секундами, критика — 75 секундами.
`WEBAPP_PLANNER_MAX_WORKERS` ограничивает параллельные LLM-вызовы (по умолчанию
4, допустимо 1–8); все бюджеты настраиваются переменными из `.env.example`.
Режим `reliable` включается
переключателем «Быстрый режим» и создаёт один черновик без смысловой критики.
Только быстрый режим может вернуть упрощённый результат со статусом
`needs_review` и предупреждением; strict завершается ошибкой, если модельный
outline или хотя бы один слайд не получен.
Таймаут одного обращения к модели задаётся через `LLM_TIMEOUT` и по
умолчанию равен 90 секундам.
Первичный VLM-анализ выполняется отдельно, кэшируется внутри аккаунта и
обслуживается отдельной очередью `template-analysis`; генерации идут через
очередь `generation`.
