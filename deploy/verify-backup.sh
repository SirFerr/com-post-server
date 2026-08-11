#!/bin/sh
set -eu

target="${1:?usage: verify-backup.sh BACKUP_DIRECTORY}"
cd "$target"
sha256sum -c SHA256SUMS
pg_restore --list postgres.dump >/dev/null
tar -tzf minio-data.tar.gz >/dev/null
tar -tzf firmware-data.tar.gz >/dev/null
echo "Backup checksums and archive structure are valid"
