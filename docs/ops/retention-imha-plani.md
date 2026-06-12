# #156 — Faz 24 Retention/İmha Planı (VERBIS beyanı ↔ sistem uyumu)

**İlke (No Fake Work):** VERBIS'e yazılan her süre yasal taahhüttür; bu doküman
her taahhüdü bir **otomatik mekanizmaya** ve bir **kanıt yöntemine** bağlar.
Süre kaynağı: #53 veri sorumlusu kararı (2026-06-12).

## Katman → mekanizma → kanıt eşlemesi

| Katman | Beyan | Mekanizma | Durum | Kanıt yöntemi |
|---|---|---|---|---|
| Ham ses (MinIO `meeting-audio`) | transkript sonrası imha, azami 7 gün | (a) S3 lifecycle `f24-audio-7d` (üst sınır, `setup-retention.sh`); (b) proaktif silme: transcript-service transkripti kalıcılaştırınca ses objesini siler | (a) script hazır — operatör apply; (b) transcript-service ile gelir | sentetik obje + 7 gün sonra `mc stat` NoSuchKey; (b) için birim testi |
| Ham transkript (transcript DB) | 1 yıl | retention cleanup job (`created_at + 365g`), idempotent, silme-öncesi audit | ⏳ DB henüz yok — **ilk kalıcılık PR'ının kabul şartı** (ADR-0032 kriter 3 ile birleşik: `tenant_id NOT NULL` + `created_at NOT NULL` + cleanup job AYNI PR'da) | sentetik fixture: eski tarihli kayıt → job sonrası yok; audit satırı var |
| Transkript kopyası (MinIO `transcripts`) | 1 yıl | S3 lifecycle `f24-transcript-1y` (`setup-retention.sh`) | script hazır | `mc ilm rule ls` çıktısı |
| Özet/karar/aksiyon (meeting-ai DB) | 2 yıl | retention cleanup job (`created_at + 730g`) | ⏳ kalıcılık katmanıyla birlikte | sentetik fixture testi |
| Audit | 7 yıl (#32; hukuk teyidi #52'de) | S3 lifecycle `f24-audit-7y` (2557g) | test profilinde uygulanmış (#55); script idempotent yeniden uygular | `mc ilm rule ls` |

## İmha audit kuralı (KVKK md.7 kanıtı)
Her otomatik/proaktif silme `kvkk_audit_event_total{action="retention_delete"}`
metriği + audit log satırı üretir: **yalnız kayıt ID + katman + zaman**
(transcript-free — içerik asla loglanmaz).

## Tasarım kararları
1. **Lifecycle = üst sınır, proaktif silme = normal yol** (ses için): 7 gün
   "hata penceresi"dir; başarılı transkripsiyon sonrası ses beklemez.
2. **DB job'ları kalıcılık PR'ıyla birlikte gelir** — var olmayan tabloya job
   yazmak fake work olur; bunun yerine kural ADR-0032 kabul kriterine bağlandı:
   *ilk kalıcı transcript PR'ı `tenant_id NOT NULL` + `created_at NOT NULL` +
   retention job + imha-audit'i birlikte getirmek zorundadır.*
3. **Idempotent + fail-safe:** job tekrar koşulabilir; silme öncesi audit yazılır,
   audit başarısızsa silme yapılmaz (kanıtsız imha yok).

## Operatör adımları (kabul için)
1. MinIO host'unda: `./deploy/minio/setup-retention.sh test` (sonra `prod`,
   operatör penceresinde) → `mc ilm rule ls` çıktıları #156'ya işlenir
2. Sentetik ses objesi yükle → 7 gün sonra yokluğunu doğrula (takvim hatırlatması)
3. VERBIS portal işlemi (irtibat kişisi) bu dokümanla aynı süreleri girer —
   metin: #53 son yorumundaki final "Diğer:" metni
