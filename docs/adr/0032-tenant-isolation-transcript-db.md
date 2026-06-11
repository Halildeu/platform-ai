# ADR-0032: Transcript/DB Katmanında Tenant İzolasyonu

**Durum:** DRAFT — #65 (R-MT-1) mitigation'ının kalan yarısı. Kabulü
operatör/maintainer kararı.

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
   alanını **NOT NULL** taşır. Tek-tenant MVP'de sabit değer yazılır
   (`workcube`), ama alan ilk günden vardır — retroactive migration yok.
2. **Nesne anahtarı kuralı (MinIO):** `s3://meetings/{tenant_id}/{meeting_id}/...`
   — tenant prefix'i anahtarın İLK segmenti; bucket policy'leri prefix bazlı
   kesilebilir (KVKK veri ayrımı da bundan beslenir).
3. **Sorgu kuralı:** Servis katmanındaki her okuma yolu tenant filtresinden
   geçer (row-level guard); "tenant'sız okuma" API'si tanımlanmaz.
4. **Sınır:** Cross-tenant rapor/analitik gereksinimi çıkarsa ayrı, açıkça
   yetkilendirilmiş bir aggregate servis ister — varsayılan yol DEĞİL.

## Sonuçlar

- Retroactive multi-tenant maliyeti "tüm şema refactor"dan "konfig + onboarding"
  seviyesine iner (#65 impact'inin sönümlenmesi).
- MVP'de ek karmaşıklık ~sıfır: tek sabit değer + prefix kuralı.
- KVKK: tenant bazlı silme/ihracat (md.11 hakları) prefix/filtre ile
  uygulanabilir hale gelir.

## Kabul kriterleri (#65 kapanışı için)

- [ ] Bu ADR ACCEPTED'a yükseltilir (operatör onayı)
- [ ] MinIO anahtar şablonu deploy/minio/README'ye işlenir
- [ ] İlk kalıcı transcript yazan PR `tenant_id NOT NULL` ile gelir
