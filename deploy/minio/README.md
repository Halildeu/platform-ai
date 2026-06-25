# #55 MinIO setup (prod + test) — Faz 24 artifact storage

Meeting ses chunk'ları ve transcript artefaktları için S3-uyumlu depo.

> **ADR-0036 boundary:** `meeting-audio` bucket'ı default live path değildir.
> Faz 24 direct-STT / recorder smoke ham sesi kalıcılaştırmadan çalışmalıdır.
> Raw-audio recording/archive ancak ayrı owner/legal/product kararı ve
> `docs/adr/0036-recording-archive-plane.md` gate'leriyle opt-in açılır.

## Kurulum (host-compose)
1. Host'ta env değerlerini ayarla (ESO'dan veya vault'tan; commit ETME):
   `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD` (prod) / `MINIO_TEST_ROOT_USER`, `MINIO_TEST_ROOT_PASSWORD` (test)
2. `docker compose --profile test up -d` → test :9100 (console :9101)
3. Doğrula: `curl http://localhost:9100/minio/health/ready` → 200
4. Bucket'lar (mc ile): `meeting-audio`, `transcripts`, `audit-archive`
5. **Retention lifecycle uygula** (KVKK md.4/md.7 — #156 + #53 süre kararı):
   ```sh
   mc alias set loc http://localhost:9100 "$MINIO_TEST_ROOT_USER" "$MINIO_TEST_ROOT_PASSWORD"
   sh setup-lifecycle.sh loc
   ```
   → meeting-audio 7 gün / transcripts 365 gün (1 yıl) / audit-archive 2557 gün
   (~7 yıl). Idempotent (re-run güvenli). Canlı kanıt 2026-06-12: test MinIO
   3 bucket lifecycle Enabled.
6. Prod aynı akış, `--profile prod`, ayrı kimlikler + `setup-lifecycle.sh`.

## k8s tüketicileri
`eso-externalsecret.yaml` apply edilir; gateway/transcript-service podları
`minio-credentials` secret'ından `MINIO_*` env alır (Faz 22-23 ESO deseni).

## Nesne anahtarı şablonu (ADR-0032 — ZORUNLU)

Kalıcılaşan her meeting/transcript nesnesi tenant-prefix'li yazılır:

```
s3://meetings/{tenant_id}/{meeting_id}/...
```

- `{tenant_id}` = transport'tan gelen gerçek tenantId'nin **ondalık string**
  temsili (`str(tenant_id)`, padding yok; kalıcı katman tipi BIGINT NOT NULL).
  Sentinel/sabit değer YASAK (ADR-0032 karar 1-2).
- Tenant prefix'i anahtarın İLK segmenti — bucket policy + KVKK md.11
  (tenant bazlı silme/ihracat) prefix'ten beslenir.
- "Tenant'sız okuma" yolu tanımlanmaz (ADR-0032 karar 4).

## KVKK notları
- Ses/transcript bucket'ları **yalnız ülke-içi** host'ta (#40 kararı, lokal).
- Erişim yalnız servis hesapları; console insan erişimi audit'lenir.
- **Retention (otomatik imha — `setup-lifecycle.sh`, #156/#53 karar):**
  - meeting-audio: **7 gün** (ham ses — transkript-sonrası imha üst sınırı)
  - transcripts: **365 gün** (1 yıl ham transkript)
  - audit-archive: **2557 gün** (~7 yıl — #32; #52 hukuk teyidi pending)
  - Özet/karar/aksiyon (2 yıl) → meeting-ai **DB** retention (MinIO'da değil;
    DB persistence implement edilince #156 ikinci faz)
- **VERBIS uyumu**: Süreler sekmesi beyanı ↔ bu lifecycle DAVRANIŞI eşleşmeli
  (beyan ↔ gerçek uyumsuzluğu KVKK ihlali). VERBIS 13-İşitsel "Diğer:" metni:
  kamera 1 ay + ses 7 gün + transkript 1 yıl + özet/karar 2 yıl (#53).
- Tam #156 go-live kabulü için MinIO lifecycle tek başına yeterli değildir.
  DB cleanup ve VERBIS kanıtını birlikte doğrulamak için:
  `python3 scripts/retention_gate.py --evidence docs/evidence/retention-readiness-2026-06-25.json --repo-root .`.
