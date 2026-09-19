# Развёртывание ComPost

## Проверки состояния и наблюдаемость

- `GET /health/live` подтверждает, что процесс API запущен.
- `GET /health/ready` проверяет БД, оба S3-хранилища и ML-сервис; production healthcheck использует этот маршрут.
- `GET /metrics` отдаёт счётчики HTTP по шаблонам маршрутов. Caddy закрывает этот endpoint снаружи, его следует забирать только из внутренней сети мониторинга.
- Каждый ответ содержит `X-Request-ID`; сервер пишет структурированный JSON-журнал запросов.
- `TRUSTED_PROXY_CIDRS` должен содержать только адреса доверенных reverse proxy. `X-Forwarded-For` от остальных клиентов игнорируется rate limiter.

### Grafana и Prometheus

Compose запускает Prometheus, проверочный экспортёр и Grafana вместе с приложением. Готовая панель **ComPost services** показывает доступность API, веба, ML, медиа, телеметрии, обеих БД, обоих S3-хранилищ и шлюза; частоту HTTP-запросов и ошибок 5xx; очередь исходящих событий, возраст старейшего задания обучения и время с последнего heartbeat обоих фоновых процессов. Prometheus хранит метрики 15 дней в `prometheus-data`, настройки Grafana — в `grafana-data`. Конфигурация сбора и панель находятся в `monitoring/`.

- Локально: после `docker compose up -d --build` откройте `http://127.0.0.1:3000` и войдите как `admin` с паролем `compost-local` (или значением `GRAFANA_ADMIN_PASSWORD`).
- В production задайте отдельный длинный случайный `GRAFANA_ADMIN_PASSWORD` в `.env.production`. Grafana слушает **только** `127.0.0.1:3000` сервера. Для доступа с другого компьютера используйте SSH-туннель: `ssh -L 3000:127.0.0.1:3000 user@server`, затем откройте `http://127.0.0.1:3000`. Не публикуйте порт Grafana, Prometheus или `/metrics` в Интернет.
- Правила в `monitoring/alerts.yml` отмечают недоступные цели, неудачные проверки, очередь исходящих событий больше 100, задания обучения в очереди старше 10 минут и heartbeat фонового процесса старше двух минут. Их состояние видно на панели. **Уведомления наружу не настроены:** для них потребуется отдельно подключить получателя уведомлений.

Проверки PostgreSQL и шлюза подтверждают доступность TCP-порта, проверки S3 и HTTP-сервисов — ответ endpoint. Они не измеряют внутреннюю загрузку БД или контейнеров. `telemetry-worker` и `ml-trainer` записывают heartbeat в БД каждые 15 секунд; API отдаёт время последнего сигнала в `/metrics`. Очереди дополняют heartbeat: процесс может отвечать, но не обрабатывать задания.

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
