#!/bin/sh
set -eu

cd "$(dirname "$0")/.."
set -a
. ./.env.production
set +a

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_root="${BACKUP_DIR:-./backups}"
target="$backup_root/$timestamp"
mkdir -p "$target"

docker compose --env-file .env.production -f docker-compose.prod.yml exec -T postgres \
  pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc > "$target/postgres.dump"
docker run --rm -v compost_minio-data:/source:ro -v "$(cd "$target" && pwd):/backup" alpine:3.22 \
  tar -czf /backup/minio-data.tar.gz -C /source .
docker run --rm -v compost_firmware-data:/source:ro -v "$(cd "$target" && pwd):/backup" alpine:3.22 \
  tar -czf /backup/firmware-data.tar.gz -C /source .

(cd "$target" && sha256sum postgres.dump minio-data.tar.gz firmware-data.tar.gz > SHA256SUMS)

if [ -n "${BACKUP_AGE_RECIPIENT:-}" ]; then
  command -v age >/dev/null 2>&1 || { echo "age is required when BACKUP_AGE_RECIPIENT is set" >&2; exit 1; }
  tar -czf - -C "$target" . | age -r "$BACKUP_AGE_RECIPIENT" -o "$target.age"
  rm -rf "$target"
fi

find "$backup_root" -mindepth 1 -maxdepth 1 -mtime "+${BACKUP_RETENTION_DAYS:-30}" -exec rm -rf -- {} +
echo "Backup created and checksummed: $target"
