# Server architecture

## Runtime services

Telemetry history now has its own PostgreSQL database and private `/v1/events` contract. The API keeps current composter state and queues `telemetry.recorded` or `composter.deleted` in `outbox_events` in the same transaction. `tools.telemetry_worker` delivers events with row locking; the telemetry service records processed IDs so a retry cannot duplicate a reading. Existing `composter_telemetry` rows can be queued once with `python -m tools.backfill_telemetry` after the new service starts. Keep the legacy table until the backfill and read counts have been verified. The production backup includes the telemetry database.

`telemetry-worker` and `ml-trainer` update `worker_heartbeats` periodically. The API exports heartbeat timestamps, outbox depth and training queue gauges for Prometheus and Grafana.

The media service owns photo object operations and signed URLs through a private token-authenticated HTTP API. Both services are internal to Compose and have no public gateway route.

The public gateway routes `/web` and `/static` to `app.web_service:app`, and all other paths to `app.api_service:app`. Both have independent processes and health checks. `ml_service:app` handles inference; `tools.ml_worker` trains models. The combined `app.main:app` entrypoint remains available for existing tests and local direct runs.

The web and API processes still share the SQLAlchemy models, database, domain operations, authentication secret and storage buckets. The API process alone runs Alembic migrations before startup. This is a deployment split, not yet independent data ownership: avoid deploying incompatible web and API versions together. Moving auth, devices or moderation to separate databases requires explicit inter-service contracts, migrations and end-to-end device tests.

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
