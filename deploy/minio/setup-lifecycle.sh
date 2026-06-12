#!/bin/sh
# Faz 24 retention lifecycle — MinIO bucket otomatik imha (KVKK md.4/md.7)
#
# Kaynak: platform-ai#156 (retention automation) + #53 saklama süresi kararı
# (2026-06-12, veri sorumlusu). VERBIS Süreler beyanı ↔ sistem davranışı
# UYUMU şart: beyan edilen süre sistemde otomatik uygulanmazsa KVKK ihlali
# (beyan ↔ gerçek uyumsuzluğu).
#
# Süreler (katmanlı — en hassas veri en kısa):
#   meeting-audio  :    7 gün  — ham ses (transkript-sonrası imha üst sınırı)
#   transcripts    :  365 gün  — ham transkript (1 yıl)
#   audit-archive  : 2557 gün  — ~7 yıl (#32; #52 hukuk teyidi: 7yıl-gerekli-mi)
#
# NOT: Özet/karar/aksiyon (LLM çıktısı) saklama = 2 yıl → meeting-ai DB'de
# tutulacak (MinIO'da ayrı bucket yok); o katman DB retention cleanup job'u
# ile uygulanır — Faz 24 transcript/meeting-ai DB persistence implement
# edilince (#156 ikinci faz, henüz DB persistence yok).
#
# Kullanım:
#   mc alias set <ALIAS> http://<minio>:9000 <ROOT_USER> <ROOT_PASS>
#   sh setup-lifecycle.sh <ALIAS>          # default ALIAS=loc
# Idempotent: aynı süreli rule zaten varsa eklemez (re-run güvenli).
set -eu

ALIAS="${1:-loc}"

add_if_absent() {
  bucket="$1"; days="$2"
  existing=$(mc ilm rule export "$ALIAS/$bucket" 2>/dev/null \
    | python3 -c "import json,sys
try:
    d=json.load(sys.stdin)
    print(any(r.get('Expiration',{}).get('Days')==$days for r in d.get('Rules',[])))
except Exception:
    print(False)" 2>/dev/null || echo False)
  if [ "$existing" = "True" ]; then
    echo "  $bucket: ${days}g zaten var (skip)"
  else
    mc ilm rule add --expire-days "$days" "$ALIAS/$bucket" >/dev/null
    echo "  $bucket: ${days}g eklendi"
  fi
}

echo "== Faz 24 retention lifecycle uygulanıyor ($ALIAS) =="
add_if_absent meeting-audio 7
add_if_absent transcripts   365
add_if_absent audit-archive 2557

echo "== doğrulama =="
for b in meeting-audio transcripts audit-archive; do
  mc ilm rule export "$ALIAS/$b" 2>/dev/null \
    | python3 -c "import json,sys
d=json.load(sys.stdin)
print('  $b: ' + ', '.join(str(r.get('Expiration',{}).get('Days'))+'g('+r['Status']+')' for r in d.get('Rules',[])))" 2>/dev/null
done
