#!/bin/sh
set -eu

cd "$(dirname "$0")/.."
set -a
. ./.env.production
set +a

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="${BACKUP_DIR:-./backups}/$timestamp"
mkdir -p "$target"

docker compose --env-file .env.production -f docker-compose.prod.yml exec -T postgres \
  pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc > "$target/postgres.dump"
docker run --rm -v compost_minio-data:/source:ro -v "$(cd "$target" && pwd):/backup" alpine \
  tar -czf /backup/minio-data.tar.gz -C /source .
docker run --rm -v compost_firmware-data:/source:ro -v "$(cd "$target" && pwd):/backup" alpine \
  tar -czf /backup/firmware-data.tar.gz -C /source .

echo "Backup created in $target"
