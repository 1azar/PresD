# Production-развёртывание

Production-конфигурация рассчитана на ВМ со статическим публичным IPv4. Наружу
публикуется только gateway с HTTP/HTTPS; API, frontend, PostgreSQL и Redis
остаются в Docker-сетях.

## Требования

- Linux VM со статическим публичным IPv4;
- Git, Docker Engine и Compose plugin;
- DNS-имя не требуется: certbot выпускает короткоживущий IP-сертификат;
- доступный из worker-контейнеров OpenAI-compatible API.

Python и `venv` на хосте не нужны.

## Установка

```bash
git clone <URL-репозитория> presd
cd presd
cp webapp/.env.example webapp/.env
chmod 600 webapp/.env
```

В `webapp/.env` обязательно задайте:

- разные случайные `POSTGRES_PASSWORD` и `REDIS_PASSWORD` из ASCII-букв и цифр;
- `WEBAPP_ALLOWED_CLIENTS_JSON` с разрешёнными логинами и паролями;
- статический публичный IPv4 ВМ в `PUBLIC_IP`;
- рабочий email в `TLS_EMAIL`;
- `LLM_BASE_URL`, `LLM_MODEL` и при необходимости `LLM_API_KEY`;
- отдельные `VLM_*`, если визуальная модель отличается от текстовой.

Настройки описаны в разделе [«Конфигурация»](configuration.md).

Проверка и запуск:

```bash
docker compose \
  --env-file webapp/.env \
  -f webapp/compose.yaml \
  config --quiet

docker compose \
  --env-file webapp/.env \
  -f webapp/compose.yaml \
  up -d --build
```

Не добавляйте `compose.local.yaml`: этот override отключает production-настройки
cookie, публикует frontend напрямую и не запускает полноценный HTTPS-контур.

## Проверка

```bash
docker compose --env-file webapp/.env -f webapp/compose.yaml ps
docker compose --env-file webapp/.env -f webapp/compose.yaml logs certbot
curl -I http://<PUBLIC_IP>
curl https://<PUBLIC_IP>/api/health
```

Первый выпуск сертификата может занять несколько минут. До его получения gateway
отвечает `503` и не разрешает вход по незащищённому HTTP. После выпуска HTTP
перенаправляется на HTTPS.

Certbot проверяет короткоживущий IP-сертификат Let's Encrypt каждые 12 часов.
Gateway подхватывает обновление не позднее чем через минуту.

## Firewall

Разрешите входящие подключения:

| Порт | Источник | Назначение |
| --- | --- | --- |
| `80/tcp` | Все | ACME-проверка и HTTPS redirect |
| `443/tcp` | Все | Web-интерфейс |
| `22/tcp` | Только доверенные IP | SSH |

Закройте остальные входящие порты. Не публикуйте Docker API (`2375/2376`),
PostgreSQL (`5432`), Redis (`6379`) и endpoint локальной модели.

Перед ограничением SSH оставьте текущую сессию открытой и проверьте подключение
во второй сессии.

## Сетевая изоляция

Compose использует четыре сети:

- `edge` — gateway, frontend и certbot;
- `app` — внутренняя связь frontend с API;
- `data` — внутренняя связь API/workers с PostgreSQL и Redis;
- `worker-egress` — исходящий доступ workers к модельному API.

Сети `app` и `data` имеют `internal: true`. Только gateway публикует host-порты
`80` и `443`.

## Данные

Docker volumes хранят:

- PostgreSQL;
- Redis append-only data;
- пользовательские файлы и результаты;
- сертификаты и ACME webroot.

Обычная остановка:

```bash
docker compose --env-file webapp/.env -f webapp/compose.yaml down
```

Команда `docker compose down -v` удаляет volumes вместе с базой, файлами и
сертификатами. Используйте её только для намеренного полного сброса.

Для резервного копирования необходимо сохранять как минимум volumes PostgreSQL и
`user-data`. Проверяйте восстановление резервной копии на отдельном окружении.

## Обновление

После получения новой версии кода пересоберите контейнеры:

```bash
git pull
docker compose \
  --env-file webapp/.env \
  -f webapp/compose.yaml \
  up -d --build --remove-orphans
```

После обновления проверьте `ps`, health endpoint, логи workers и одну тестовую
генерацию. Не удаляйте volumes в процессе обычного обновления.

## Диагностика

```bash
docker compose --env-file webapp/.env -f webapp/compose.yaml ps
docker compose --env-file webapp/.env -f webapp/compose.yaml logs --tail 200 api
docker compose --env-file webapp/.env -f webapp/compose.yaml logs --tail 200 generation-worker
docker compose --env-file webapp/.env -f webapp/compose.yaml logs --tail 200 template-analysis-worker
docker compose --env-file webapp/.env -f webapp/compose.yaml logs --tail 200 template-enrichment-worker
```

Не публикуйте полные логи без проверки: сообщения провайдера и имена входных
файлов могут содержать чувствительные сведения.
