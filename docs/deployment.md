# Развёртывание ComPost

## Проверки состояния и наблюдаемость

- `GET /health/live` подтверждает, что процесс API запущен.
- `GET /health/ready` проверяет БД, оба S3-хранилища и ML-сервис; production healthcheck использует этот маршрут.
- `GET /metrics` отдаёт счётчики HTTP по шаблонам маршрутов. Caddy закрывает этот endpoint снаружи, его следует забирать только из внутренней сети мониторинга.
- Каждый ответ содержит `X-Request-ID`; сервер пишет структурированный JSON-журнал запросов.
- `TRUSTED_PROXY_CIDRS` должен содержать только адреса доверенных reverse proxy. `X-Forwarded-For` от остальных клиентов игнорируется rate limiter.

## Требования

- Linux-сервер с Docker Engine и Docker Compose v2;
- домены для API/веба и S3, направленные A/AAAA-записями на сервер;
- открытые входящие порты 80 и 443;
- для обучения без GPU желательно не менее 8 ГБ RAM и достаточно диска под датасет и веса.

## Первый запуск

```bash
cp .env.production.example .env.production
# заполнить все секреты и домены
docker compose --env-file .env.production -f docker-compose.prod.yml config
docker compose --env-file .env.production -f docker-compose.prod.yml up -d --build
docker compose --env-file .env.production -f docker-compose.prod.yml exec api python -m app.seed
```

Caddy автоматически получает и обновляет TLS-сертификаты. Перед запуском API выполняется `alembic upgrade head`; runtime не изменяет production-схему через `create_all`. Bootstrap-администратор создаётся только из `BOOTSTRAP_ADMIN_EMAIL` и `BOOTSTRAP_ADMIN_PASSWORD`. PostgreSQL, фотографии, прошивки, модели и кэш обучения находятся в именованных Docker volumes. Наружу публикуются только 80/443.

## Обновление

```bash
git pull --ff-only
docker compose --env-file .env.production -f docker-compose.prod.yml up -d --build
docker compose --env-file .env.production -f docker-compose.prod.yml ps
```

## ML

`ml-service` выполняет инференс активной ONNX-моделью. `ml-trainer` постоянно обрабатывает задания, созданные в `/web/ml`. До публикации прошедшей контроль модели используется эвристический fallback. В датасет можно явно выбрать ручные примеры и фотографии с завершённой модерацией. Обучение с двумя источниками считается экспериментальным; публикация разрешена только при отдельном `test` split, минимум трёх источниках и прохождении метрик.

При первом обучении `ml-trainer` автоматически подготавливает официальный TACO и предобучает промежуточный детектор загрязнений. Загруженные изображения и веса остаются в `ml-cache`, поэтому последующие запуски используют кэш. Для сервера без исходящего доступа сначала выполните `docker compose run --rm ml-trainer python -m tools.taco_pretrain prepare`. Отключение этапа: `ML_TACO_PRETRAIN=false`.

## Резервное копирование

```bash
chmod +x deploy/backup.sh
BACKUP_DIR=/srv/compost-backups ./deploy/backup.sh
./deploy/verify-backup.sh /srv/compost-backups/<timestamp>
```

Скрипт создаёт SHA-256 manifest и удаляет копии старше `BACKUP_RETENTION_DAYS`. Если задан `BACKUP_AGE_RECIPIENT`, архив шифруется `age`. Копию нужно выгружать на отдельный сервер или объектное хранилище; восстановление периодически проверяется в тестовом окружении.
