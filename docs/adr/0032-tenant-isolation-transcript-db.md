# ADR-0032: Transcript/DB Katmanında Tenant İzolasyonu

**Durum:** ACCEPTED (2026-06-11) — Halil review iter-1 REVISE (P1 sentinel +
P2 tip) → Zeynep iter-2 absorb (PR #152) → maintainer kabulü. Cross-AI not:
Codex (OpenAI) ikinci görüşü thread `019eb6ea` ("AGREE-with-fix — sentinel
pattern kaldır, tip uyumu netleşsin") — iter-2 her iki şartı karşılar.

## Bağlam

Faz 24.1 MVP tek tenant çalışır; Workcube dışı müşteri girişinde retroactive
multi-tenant ayrımı pahalıdır (#65). Transport katmanı bu riski **zaten
kapattı** — kanıt:

- audio-gateway producer: `tenantId` stream alanlarında zorunlu; partition
  anahtarı `hash(tenantId + sessionId) % 32` (platform-backend #534, MERGED)
- live-stt consumer: aynı alanı okur, `messageId` dedup'ı tenant-bilinçli
  (platform-ai #138, MERGED)

Kalan boşluk: **transcript ve kalıcı veri katmanı** (MinIO nesneleri, ileride
DB şeması, meeting-ai çıktıları) tenant kimliği taşımıyor.

## Karar (önerilen)

1. **Şema kuralı:** Kalıcılaşan her transcript/özet/karar kaydı `tenant_id`
   alanını **NOT NULL** taşır ve değer **transport'tan gelen gerçek
   `tenantId`'dir** (#534 producer'ın JWT `companyId`'den yazdığı alan) —
   olduğu gibi persist edilir; **sentinel/sabit değer YASAK** (iter-1'deki
   `workcube` sabiti kaldırıldı: transport↔persistence tutarsızlığı ve ileride
   sabit→gerçek migration'ı üretirdi — ADR'nin önlediği şeyin ta kendisi).
   MVP'de tek tenant olduğu için değer doğal olarak tekil olur.
2. **Tip kuralı:** Kalıcı katmanda `tenant_id BIGINT NOT NULL` (JWT
   `companyId` numeric); MinIO prefix'inde ondalık string temsili kullanılır
   (`str(tenant_id)`, padding yok).
3. **Nesne anahtarı kuralı (MinIO):** `s3://meetings/{tenant_id}/{meeting_id}/...`
   — tenant prefix'i anahtarın İLK segmenti; bucket policy'leri prefix bazlı
   kesilebilir (KVKK veri ayrımı da bundan beslenir).
4. **Sorgu kuralı:** Servis katmanındaki her okuma yolu tenant filtresinden
   geçer (row-level guard); "tenant'sız okuma" API'si tanımlanmaz.
5. **Sınır:** Cross-tenant rapor/analitik gereksinimi çıkarsa ayrı, açıkça
   yetkilendirilmiş bir aggregate servis ister — varsayılan yol DEĞİL.

## Sonuçlar

- Retroactive multi-tenant maliyeti "tüm şema refactor"dan "konfig + onboarding"
  seviyesine iner (#65 impact'inin sönümlenmesi).
- MVP'de ek karmaşıklık ~sıfır: transport zaten değeri taşıyor; prefix kuralı.
- KVKK: tenant bazlı silme/ihracat (md.11 hakları) prefix/filtre ile
  uygulanabilir hale gelir.

## Kabul kriterleri (#65 kapanışı için)

- [x] Bu ADR ACCEPTED'a yükseltilir (operatör onayı — 2026-06-11)
- [x] MinIO anahtar şablonu deploy/minio/README'ye işlenir (bu PR)
- [ ] İlk kalıcı transcript yazan PR `tenant_id NOT NULL` ile gelir
