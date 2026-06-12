#!/usr/bin/env bash
# #156 KVKK retention — MinIO bucket + lifecycle bootstrap (idempotent).
# VERBIS beyanı ile birebir (#53 kararı, 2026-06-12):
#   meeting-audio   : ham ses — transkript sonrası imha, AZAMİ 7 gün (lifecycle üst sınır;
#                     normal yol transkript-sonrası proaktif silme, transcript-service'te)
#   transcripts     : 1 yıl  (365 gün)
#   audit-archive   : 7 yıl  (#32; hukuk teyidi #52'de — değişirse burada güncellenir)
# Özet/karar/aksiyon (2 yıl) DB'de yaşar — bu script kapsamı dışı (#156 madde 3).
#
# Kullanım (operatör, MinIO host'unda):
#   ./setup-retention.sh test   # test profili (:9100)
#   ./setup-retention.sh prod   # prod profili (:9000)
# Gerekli env: MC_HOST_target olarak kurulur — MINIO_ROOT_USER / MINIO_ROOT_PASSWORD
# (test profili için MINIO_TEST_ROOT_USER / MINIO_TEST_ROOT_PASSWORD).
set -euo pipefail

PROFILE="${1:?usage: setup-retention.sh test|prod}"
case "$PROFILE" in
  test) PORT=9100; USER="${MINIO_TEST_ROOT_USER:?}"; PASS="${MINIO_TEST_ROOT_PASSWORD:?}";;
  prod) PORT=9000; USER="${MINIO_ROOT_USER:?}";      PASS="${MINIO_ROOT_PASSWORD:?}";;
  *) echo "profile must be test|prod" >&2; exit 1;;
esac

ALIAS="f24-$PROFILE"
mc alias set "$ALIAS" "http://127.0.0.1:${PORT}" "$USER" "$PASS"

# Bucket'lar (varsa dokunmaz)
for b in meeting-audio transcripts audit-archive; do
  mc mb --ignore-existing "$ALIAS/$b"
done

# Lifecycle kuralları — mevcut aynı-id kural varsa önce temizle (idempotent)
mc ilm rule remove --id f24-audio-7d      "$ALIAS/meeting-audio" 2>/dev/null || true
mc ilm rule remove --id f24-transcript-1y "$ALIAS/transcripts"   2>/dev/null || true
mc ilm rule remove --id f24-audit-7y      "$ALIAS/audit-archive" 2>/dev/null || true

mc ilm rule add --id f24-audio-7d      --expire-days 7    "$ALIAS/meeting-audio"
mc ilm rule add --id f24-transcript-1y --expire-days 365  "$ALIAS/transcripts"
mc ilm rule add --id f24-audit-7y      --expire-days 2557 "$ALIAS/audit-archive"

echo "--- yürürlükteki kurallar ---"
for b in meeting-audio transcripts audit-archive; do
  echo "[$b]"; mc ilm rule ls "$ALIAS/$b"
done
echo "OK — VERBIS beyanı ile uyumlu lifecycle aktif ($PROFILE)."
