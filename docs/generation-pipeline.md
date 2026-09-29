# Пайплайн генерации

Ядро PresD можно использовать через web-приложение или независимо через Python
CLI. В обоих случаях обработка проходит через анализ шаблона, построение плана и
детерминированную сборку результата.

## 1. Модель шаблона

`template_model` декомпозирует PPTX и описывает адресуемое содержимое каждого
слайда. Для текстовых блоков, изображений, таблиц, графиков, media и вложенных
групп сохраняются тип, нормализованные координаты, точный текст и `shape_id`.
Линии и фигуры без контента пропускаются.

```bash
python -m template_model analyze \
  --template input.pptx \
  --output presentation_model
```

Результат:

```text
presentation_model/
├── source.pptx
├── presentation.json
└── slides/
    ├── slide_001/
    │   ├── metadata.json
    │   └── preview.png
    └── slide_002/
        ├── metadata.json
        └── preview.png
```

`presentation.json` содержит сведения об исходнике и индекс слайдов, а каждый
`metadata.json` — структуру конкретного слайда. Каталог создаётся атомарно и не
перезаписывается, если уже существует и не пуст.

Превью строятся через LibreOffice и `pdftoppm`. Ошибка рендера не мешает сохранить
структурные метаданные.

### VLM-классификация

Для семантического описания слайдов включите VLM:

```bash
python -m template_model analyze \
  --template input.pptx \
  --output presentation_model_vlm \
  --vlm
```

Анализ добавляет `classification`, `tags`, `description` и `confidence`. Ошибка
отдельного слайда записывается в `vlm.status` и `vlm.error` и не отменяет
структурный результат. Визуальная модель наследует `LLM_*`, если отдельные
`VLM_*` не заданы.

Корневой `main.py` остаётся обратно совместимой обёрткой, но предпочтительный
вызов — `python -m template_model`.

## 2. Семантический каталог

Перед генерацией `content_planner` группирует низкоуровневые фигуры в логические
слоты. Связанные элементы таблиц, графиков, карточек и профилей становятся одним
слотом, а похожие слайды объединяются в семейства вариантов.

```bash
python -m content_planner build-catalog \
  --template-dir presentation_model \
  --workers 4
```

Каталог кэшируется рядом с моделью шаблона. Он перестраивается при изменении PPTX
или версии prompt. Смена только модели планировщика не инвалидирует готовый
каталог.

Нормализация технических ограничений выполняется кодом. Например, единый
`slide_class` внутри family не доверяется ответу модели.

## 3. Контент-план

Планировщик получает бриф, материалы, диапазон количества слайдов и при
необходимости структурированные данные:

```json
{
  "brief": "Представить продукт и команду продуктовому комитету.",
  "content_package": "- Анна Лебедева — руководитель продукта.\n- Платформой пользуются 42 команды.",
  "slide_count": {
    "min": 5,
    "max": 7
  }
}
```

Генерация плана:

```bash
python -m content_planner generate \
  request.json \
  --template-dir presentation_model \
  --output plan.json
```

По умолчанию pipeline:

1. анализирует входные материалы;
2. строит общий outline;
3. параллельно генерирует содержимое слайдов;
4. запускает три профильные проверки;
5. выполняет до двух раундов глобальных или послайдовых исправлений;
6. канонизирует технические поля и выполняет детерминированную валидацию.

`plan.json` содержит выбранное семейство, номер исходного слайда и назначения
логических слотов с точными `shape_id`. Каждый заполненный слот ссылается через
`source_refs` на использованные фрагменты входных данных.

Проверки блокируют результат при неизвестных `shape_id`, превышении вместимости,
пропуске обязательных фактов и числах без подтверждённого источника. Числа
сравниваются после нормализации разделителей, поэтому `2 800` соответствует
`2800`.

### Быстрый режим

Для черновика можно отключить ансамбль и семантическую критику:

```bash
python -m content_planner generate \
  request.json \
  --template-dir presentation_model \
  --output plan.json \
  --fast
```

Структурная ревизия и детерминированные проверки источников, чисел, структуры и
вместимости остаются включёнными. В web-интерфейсе этому соответствует
переключатель «Быстрый режим».

### Демонстрационный запрос

```bash
python -m content_planner demo \
  --template-dir presentation_model \
  --output content_planner/output/it_team_plan.json
```

Тот же сценарий доступен как обычный JSON:

```bash
python -m content_planner generate \
  content_planner/examples/it_team_request.json \
  --template-dir presentation_model \
  --output content_planner/output/it_team_plan.json
```

## Структурированные данные

`PlanningRequest.structured_assets` принимает объекты типов `dataset`, `diagram`
и `pictogram_grid`. Web-приложение извлекает datasets из CSV, XLSX, JSON,
Markdown- и DOCX-таблиц.

`visual_hint` поддерживает режимы:

- `auto` — выбрать визуализацию по данным и возможностям шаблона;
- `none` — не превращать данные в визуальный объект;
- `force` — потребовать совместимую визуализацию и завершиться ошибкой, если её
  невозможно разместить.

Chart-datasets обрабатываются отдельно: модель выбирает только место слайда в
narrative и его заголовок. Код выбирает chart/content_visual canvas, делит данные
максимум по 12 категорий и создаёт нативный PowerPoint chart. Table, diagram и
pictogram grid используют слотные контейнеры шаблона.

Для универсального контейнера задайте фигуре имя или alt text `presd:visual` либо
более узкое значение, например `presd:visual:chart,table`.

## Изображения

Для image-слотов поддерживаются операции:

- `keep` — оставить изображение шаблона;
- `replace` — использовать переданный файл;
- `generate` — запросить новое изображение через интеграцию;
- `clear` — очистить слот.

Внешность сотрудников без переданных фотографий не генерируется. Если в брифе
явно назван отсутствующий файл, web-pipeline может создать нейтральный placeholder
и отразить это в предупреждениях.

## 4. Сборка PPTX

`presentation_builder` копирует выбранные слайды вместе с оформлением, допускает
повторное использование одного шаблонного слайда и применяет операции по точным
`shape_id`.

```bash
python -m presentation_builder build \
  --template-dir presentation_model \
  --plan plan.json \
  --assets-dir assets \
  --output presentation.pptx
```

Относительные `asset_ref` разрешаются только внутри `--assets-dir`. Результат
публикуется атомарно и не перезаписывает существующий файл.

Рядом с PPTX создаётся каталог QA с `report.json` и превью. Можно явно задать
PDF и каталог проверки:

```bash
python -m presentation_builder build \
  --template-dir presentation_model \
  --plan plan.json \
  --assets-dir assets \
  --output presentation.pptx \
  --pdf-output presentation.pdf \
  --qa-dir presentation.qa
```

Ошибка LibreOffice фиксируется как предупреждение и не отменяет корректный PPTX.
Текущие сборщик и планировщик используют каталог и план версии `5.0`; после
изменения шаблона их нужно построить заново.

Действие `generate` для изображений подключается через Python API:

```python
from presentation_builder import PresentationBuilder

report = PresentationBuilder(image_generator=my_generator).build(
    "presentation_model",
    "plan.json",
    "presentation.pptx",
    assets_dir="assets",
)
```

Без реализации `ImageGenerator` операция `generate` завершается строгой ошибкой.

## Связанные разделы

- [Архитектура](architecture.md)
- [Конфигурация](configuration.md)
- [Разработка и тестирование](development.md)
