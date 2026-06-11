# #55 MinIO setup (prod + test) — Faz 24 artifact storage

Meeting ses chunk'ları ve transcript artefaktları için S3-uyumlu depo.

## Kurulum (host-compose)
1. Host'ta env değerlerini ayarla (ESO'dan veya vault'tan; commit ETME):
   `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD` (prod) / `MINIO_TEST_ROOT_USER`, `MINIO_TEST_ROOT_PASSWORD` (test)
2. `docker compose --profile test up -d` → test :9100 (console :9101)
3. Doğrula: `curl http://localhost:9100/minio/health/ready` → 200
4. Bucket'lar (mc ile): `meeting-audio`, `transcripts`, `audit-archive`
   (audit 7 yıl retention — #32 ile uyumlu lifecycle policy uygula).
5. Prod aynı akış, `--profile prod`, ayrı kimlikler.

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
- Retention: meeting-audio pilot sonrası silinir (consent şartı, #34);
  audit-archive 7 yıl (#32).
