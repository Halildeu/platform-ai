# Faz 24 #156 Retention Readiness Gate

Bu gate, VERBIS beyanı ile sistem davranışının aynı olup olmadığını metadata-only
kanıtla kontrol eder. Amaç #156'yı erken kapatmak değil; MinIO lifecycle
tamamlanmışken DB persistence/cleanup eksiklerini go-live öncesi makineyle
yakalamaktır.

## Komut

```bash
python3 scripts/retention_gate.py \
  --evidence docs/evidence/retention-readiness-2026-06-25.json \
  --repo-root .
```

Beklenen mevcut sonuç: `status=blocked`. Bu doğru durumdur; MinIO lifecycle
aktif, fakat DB cleanup katmanı ve VERBIS portal/operator kararı henüz go-live
kabulüne yetmez.

## Status Anlamı

| Status | Anlam |
|---|---|
| `pass` | Tüm required layer'lar aktif, süreler doğru, DB cleanup job + transcript-free imha audit kanıtı var, VERBIS recorded/exempt-confirmed. |
| `blocked` | Kanıt eksik veya bilinçli pending; örn. DB persistence henüz yok. Bu, fake closure engelidir. |
| `fail` | Kanıt şekli hatalı, süre uyumsuz, aktif DB iddiası cleanup/audit refsiz, ya da evidence içinde raw transcript/audio/PII benzeri değer var. |

## Required Layer'lar

| Layer | Mekanizma | Süre | #156 Bağı |
|---|---:|---:|---|
| `minio.meeting-audio` | `s3-lifecycle` | 7 gün | Ham ses azami saklama |
| `minio.transcripts` | `s3-lifecycle` | 365 gün | Ham transcript object retention |
| `minio.audit-archive` | `s3-lifecycle` | 2557 gün | Audit archive retention |
| `db.transcript-records` | `db-cleanup-job` | 365 gün | Transcript DB cleanup, persistence sonrası |
| `db.meeting-intelligence` | `db-cleanup-job` | 730 gün | Özet/karar/aksiyon DB cleanup |
| `db.kvkk-access-log` | `db-cleanup-job` | 730 gün | KVKK m.12 erişim/işleme logu |

## DB Active Kanıt Sınırı

Bir DB layer ancak şu alanlarla `active` sayılır:

- `persistence_ref`
- `cleanup_job_ref`
- `destruction_audit_ref`
- `tenant_id_field=tenant_id`
- `created_at_field=created_at`
- `audit_payload=metadata-only` veya `transcript-free` veya `id-only`

Bu alanlar yoksa gate `fail` döner. Var olmayan DB tablosu için cleanup job
yazmak veya sadece doc cümlesiyle `active` demek kabul değildir.

## Privacy Sınırı

Evidence dosyası ham ses, transcript, summary, karar/aksiyon metni, prompt,
response, katılımcı adı/e-postası/telefonu/IBAN benzeri değer taşıyamaz. Gate
bulguları sensitive value'yu echo etmez.
