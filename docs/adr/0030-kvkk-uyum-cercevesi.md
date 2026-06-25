# ADR-0030: Faz 24 Meeting Intelligence KVKK Uyum Çerçevesi

**Durum:** ACCEPTED (2026-06-16) — hibrit karar (yönetim + teknik değerlendirme,
#52). Karar: (b) yönetim risk kabulüyle şimdi ilerle + (a) dış hukuk görüşü
paralel async başlatıldı. Cross-AI not: #534/#65 review zinciri + Codex teyitleri.

## Karar (sınır cümlesi — verbatim)

> Pre-production ve kontrollü pilot go-live hazırlığı için kabul edildi. Dış
> hukuk görüşü async başlatıldı (pilot launch'tan en geç 2 hafta önce
> deliverable, pilot go-live gate'i). Hukuk görüşü olumsuz/şartlı gelirse ADR
> revize edilir. Geniş ölçekli production / müşteri rollout, hukuk görüşü +
> yönetim sign-off olmadan yapılamaz. Audit-log retention = KVKK m.12
> erişim/işleme güvenlik kaydıdır (TTK m.82 / VUK m.253 ticari defteri
> DEĞİLDİR); mühendislik varsayımı 2 yıldır; TTK/VUK uygulanabilirliği hukuk
> paketinde explicit soru olarak gönderilmiştir.

## Bağlam
Şirket-içi toplantı sesi → canlı transkript → anonim diarizasyon (voiceprint
YOK) → LLM özet/karar/aksiyon. Tüm işleme **ülke-içi, şirket donanımında**
(#40 RTX 4070; cloud yok). ADR placeholder iken pilot kayıt yasaktı; bu kararla
**pre-production + kontrollü pilot hazırlığı** açılır, geniş production gate'li kalır.

## Kararın gerekçesi (b yolu)
Teknik tedbirlerin tamamı **kodda ve kanıtlı** — yurt dışı aktarım kalemi #54
Option B (Ollama on-prem) ile zaten düştüğünden kalan kapsam dar:

| Tedbir | Kanıt |
|---|---|
| Redaction-before-LLM (env ile kapatılamaz) | meeting-ai `_enforce_kvkk_redaction_boundary` (#49) |
| Transcript-free loglar | stream.py (#30); prod Loki ingestion transcript-free |
| Ülke-içi işleme (yurt dışı aktarım yok) | #54 Option B (Ollama), VERBIS "Yabancı Ülkeler" boş |
| Saklama/imha otomasyonu | MinIO lifecycle (#156/#158): ses 7g / transkript 1y / audit-archive 7y; KVKK m.12 logu 2y (DB, ikinci faz) |
| Anonim diarizasyon | SPEAKER_XX, voiceprint/biyometrik yok (#48) |

## Audit retention — İKİ AYRI katman (karıştırma!)
KVKK m.12 / yönetim netleştirmesi (2026-06-16) ile iki ayrı saklama katmanı:

1. **Audit olay kayıtları (audit-archive)** = **7 yıl.** Dispatcher audit sink;
   denetim/uyuşmazlık ispatı. MinIO `audit-archive` lifecycle 2557g (#158) —
   **değişmez**, scope-limit "yalnız audit olay kayıtları için" korunur.
2. **KVKK m.12 erişim/işleme logu** (kim/ne zaman/hangi kişisel veriye erişti)
   = **2 yıl.** Ayrı katman; meeting-ai DB'de tutulacak (DB persistence henüz
   yok — #156 ikinci faz; implement edilince 2 yıl retention). MinIO'da bucket'ı yok.

İkisi de TTK m.82 / VUK m.253 kapsamındaki **ticari defter/muhasebe kaydı
DEĞİLDİR** (o ayrı bir rejim). TTK/VUK uygulanabilirliği hukuk paketine
**explicit soru** olarak eklendi (aşağıda).

## Saklama süreleri (#53 kararı, sistemde canlı)
| Katman | Süre | Mekanizma |
|---|---|---|
| Ham ses | transkript sonrası imha, azami 7 gün | MinIO lifecycle (#158) |
| Ham transkript | 1 yıl | MinIO lifecycle + DB cleanup (ikinci faz, #156) |
| Özet/karar/aksiyon | 2 yıl | DB cleanup (ikinci faz, #156) |
| Audit olay kayıtları (audit-archive) | **7 yıl** | MinIO lifecycle 2557g (#158) |
| KVKK m.12 erişim/işleme logu | **2 yıl** | meeting-ai DB (ikinci faz, #156) |

Machine-readable go-live kontrolü:
`scripts/retention_gate.py --evidence docs/evidence/retention-readiness-2026-06-25.json --repo-root .`
mevcut durumda bilinçli olarak `blocked` döner; MinIO lifecycle tamamdır, fakat
DB cleanup job'ları + VERBIS recorded/exempt-confirmed kanıtı olmadan #156
go-live kabulü yapılamaz.

## Paralel hukuk paketi (a yolu — async)
`docs/legal/adr-0030-hukuk-review-paketi.md` — pilot launch'tan en geç 2 hafta
önce deliverable, **pilot go-live gate'i**. Eklenen explicit soru:

> "KVKK m.12 erişim/işleme logları (2 yıl önerisi) ile 7 yıllık audit olay
> kayıtları ayrı saklama rejimleri olarak değerlendirilebilir mi? TTK m.82 /
> VUK m.253 bunlardan herhangi birine uygulanır mı?"

## Sonuçlar / gate'ler
- ✅ Build + pre-production + kontrollü pilot **hazırlığı** açık
- 🔴 Pilot go-live → hukuk görüşü deliverable gate'i (≤2 hafta önce)
- 🔴 Geniş production / müşteri rollout → hukuk görüşü + yönetim sign-off (#59)
- Bu ADR ACCEPTED → **#52 + #60 kapanır**; #53 portal işlemi sonrası düşer.
