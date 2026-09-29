# Архитектура

PresD состоит из web-интерфейса, API, двух хранилищ состояния, общего файлового
хранилища и трёх специализированных worker-процессов. Долгие операции выполняются
асинхронно, поэтому перезапуск HTTP-запроса не влияет на уже поставленное задание.

## Общая схема

```mermaid
flowchart LR
    Browser[Браузер] --> Frontend[React + Nginx]
    Frontend --> API[FastAPI]

    API --> DB[(PostgreSQL)]
    API --> Redis[(Redis / RQ)]
    API --> Storage[(user-data volume)]

    Redis --> Analysis[template-analysis worker]
    Redis --> Enrichment[template-enrichment worker]
    Redis --> Generation[generation worker]

    Analysis --> TM[template_model]
    Enrichment --> VLM[VLM + каталог]
    Generation --> CP[content_planner]
    CP --> PB[presentation_builder]

    TM --> Storage
    VLM --> Storage
    PB --> Artifacts[PPTX / PDF / previews / QA]
    Artifacts --> Storage
```

## Компоненты

### Frontend

React-приложение предоставляет четыре основных экрана:

- создание презентации;
- библиотека шаблонов;
- история заданий;
- состояние и результат конкретного задания.

В production-сборке статические файлы раздаёт Nginx. Он же проксирует `/api` в
FastAPI и поддерживает поток событий о ходе задания.

### API

FastAPI отвечает за:

- вход и выход пользователей;
- загрузку, просмотр, переанализ и удаление шаблонов;
- валидацию брифа и входных файлов;
- создание, просмотр и отмену заданий;
- выдачу PPTX, PDF и превью;
- постановку фоновых операций в очереди RQ.

Самостоятельной регистрации нет. Разрешённые логины и исходные пароли задаются в
`WEBAPP_ALLOWED_CLIENTS_JSON`; при первом успешном входе пользователь создаётся в
базе, а пароль сохраняется как Argon2id-хеш.

### PostgreSQL

База хранит пользователей, сессии, шаблоны, задания, приложенные файлы и события
прогресса. Крупные бинарные файлы в PostgreSQL не записываются: модели шаблонов и
результаты находятся в файловом volume, а таблицы содержат относительные пути.

### Redis и RQ

Redis хранит очереди фоновых задач. Работа разделена между тремя процессами:

| Очередь | Назначение |
| --- | --- |
| `template-analysis` | Проверка PPTX, извлечение структуры и базовый каталог |
| `template-enrichment` | VLM-анализ слайдов и обновление семантического каталога |
| `generation` | Извлечение материалов, планирование, сборка и QA |

Worker восстанавливает один раз незавершённое задание после инфраструктурного
перезапуска. Повторный системный сбой фиксируется как ошибка; для необязательного
визуального обогащения уже готовая базовая модель остаётся доступной.

### Файловое хранилище

Docker volume `user-data` подключён к API и workers. В нём находятся загруженные
PPTX, нормализованные материалы, модели шаблонов, планы и конечные артефакты.
Пути проверяются относительно корня хранилища, чтобы входные `asset_ref` не могли
обратиться к произвольному файлу на хосте.

## Анализ шаблона

```mermaid
sequenceDiagram
    participant U as Пользователь
    participant A as API
    participant Q as Redis/RQ
    participant W as Analysis workers
    participant S as Storage

    U->>A: Загружает PPTX
    A->>S: Сохраняет исходник
    A->>Q: Ставит structural analysis
    Q->>W: template-analysis
    W->>S: metadata + previews + base catalog
    W-->>A: Шаблон доступен
    A->>Q: Ставит optional enrichment
    Q->>W: template-enrichment
    W->>S: VLM metadata + enriched catalog
```

Базовая версия шаблона становится доступной до завершения необязательного
VLM-обогащения. Это уменьшает время до первой генерации и позволяет работать с
текстовой моделью без поддержки изображений.

## Генерация презентации

```mermaid
sequenceDiagram
    participant U as Пользователь
    participant A as API
    participant Q as Redis/RQ
    participant W as Generation worker
    participant L as LLM
    participant S as Storage

    U->>A: Бриф, файлы, шаблон
    A->>S: Сохраняет запрос и assets
    A->>Q: Ставит generation job
    Q->>W: Запускает обработку
    W->>W: Извлекает текст и datasets
    W->>L: Строит и проверяет план
    W->>W: Собирает PPTX и запускает QA
    W->>S: PPTX, PDF, previews, report
    A-->>U: Прогресс и ссылки на результат
```

Прогресс записывается как события и передаётся интерфейсу через Server-Sent
Events. При недоступности потока frontend переключается на периодический polling.
Отмена задания отправляет команду RQ и проверяется самим pipeline в контрольных
точках.

## Сетевые границы

При локальном запуске наружу опубликован только frontend на `127.0.0.1:8080`.
В production публикуются порты gateway `80` и `443`. API, PostgreSQL и Redis
работают во внутренних Docker-сетях. Исходящий доступ к модельному API получают
worker-контейнеры; frontend и база в нём не нуждаются.

Подробнее:

- [Пайплайн генерации](generation-pipeline.md)
- [Конфигурация](configuration.md)
- [Production-развёртывание](deployment.md)
