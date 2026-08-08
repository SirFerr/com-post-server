# Развёртывание ComPost

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

Caddy автоматически получает и обновляет TLS-сертификаты. PostgreSQL, фотографии, прошивки, модели и кэш обучения находятся в именованных Docker volumes. Наружу публикуются только 80/443.

## Обновление

```bash
git pull --ff-only
docker compose --env-file .env.production -f docker-compose.prod.yml up -d --build
docker compose --env-file .env.production -f docker-compose.prod.yml ps
```

## ML

`ml-service` выполняет инференс активной ONNX-моделью. `ml-trainer` постоянно обрабатывает задания, созданные в `/web/ml`. До публикации прошедшей контроль модели используется эвристический fallback. Ручной пример добавляется в ML-разделе с классом, источником и рамками загрязнений; затем создаётся версия датасета, запускается обучение и после проверки метрик публикуется кандидат.

## Резервное копирование

```bash
chmod +x deploy/backup.sh
BACKUP_DIR=/srv/compost-backups ./deploy/backup.sh
```

Копию нужно выгружать на отдельный сервер или объектное хранилище. Периодически проверяйте восстановление PostgreSQL и volumes на тестовом окружении.
