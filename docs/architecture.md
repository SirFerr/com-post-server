# Server architecture

## Dependency direction

`API/Web -> domain -> models/database/config`

Инфраструктурные клиенты S3/HTTP находятся рядом с обслуживаемым доменом и не создаются в route-функциях без необходимости.

## Structure

```text
app/
  api/                 JSON API feature routers
  domain/              business operations
  queries/             bounded read-side database queries
  web_support/         shared web concerns
  templates/components reusable Jinja UI
  main.py              application factory only
ml_runtime/
  api.py               inference HTTP contract
  registry.py          active model lifecycle
  preprocessing.py     image tensor preparation
  inference.py         trained and baseline predictions
  storage.py           model/photo object storage
ml_service.py          stable ASGI compatibility entrypoint
```

`app/services.py` временно остаётся compatibility facade. Новый код импортирует конкретный `app.domain.*` модуль.

## Route rule

Route валидирует transport, вызывает domain-operation и формирует response. Сложный SQL-query выделяется в repository/query module. Route не должен одновременно работать с S3, обучать модель и форматировать UI.

## Web UI rule

- навигация и layout — `templates/components`;
- повторяемые статусы, empty states и headers — Jinja macros;
- feature-specific markup — собственный template;
- сложный JavaScript — отдельный файл `static/js/<feature>.js` с настройкой через `data-*`.

## ML rule

Baseline heuristic и production model runtime — разные стратегии inference. Загрузка версии модели, preprocessing и HTTP API не смешиваются в одном файле. Dataset/training pipeline не импортирует FastAPI handlers.
