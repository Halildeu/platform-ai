# #57 Browser Smoke Acceptance — e2e (mobile + web)

**Amaç:** Cutover gate G8. Gerçek istemciden uçtan uca kabul. İki aşama:
**Aşama-1** bugünkü kodla koşulabilir (direct-WS yolu, #128/#129);
**Aşama-2** gateway+mobil zinciri ister (#106 merge + staging şart).

KVKK sınırının kodda olduğu yer (kriterler buna göre yazıldı — UI'da ham
transkript görünmesi üründür, redaction **loglarda** ve **LLM girdisinde**dir):
- Loglar transcript-free (#30, stream.py)
- meeting-ai: analyzer/LLM yalnız redacted metin görür (#49 guard)

## Aşama-1 — Direct WS + servisler (BUGÜN koşulabilir, GPU host)
| # | Senaryo | PASS kriteri |
|---|---|---|
| S1 | Mikrofon → `/ws/stream` → canlı draft | `ready` event; ilk `partial` < 2 sn; konuşma boyunca draft güncellenir |
| S2 | Konuşma sonrası final | `final` event draft'ı revize eder; metin doğru Türkçe |
| S3 | Sessizlik/müzik → halüsinasyon yok | "izlediğiniz için teşekkürler" tipi artefakt UI'a düşmez |
| S4 | Transcript → meeting-ai `/analyze` | Yanıtta `redacted: true`; TC/telefon içeren test cümlesi özette `***REDACTED_*` olarak görünür |
| S5 | Sunucu logları transcript-free | Servis konsolunda konuşulan metinden TEK kelime yok (seq/ms/uzunluk serbest) |
| S6 | WS kopması → yeniden bağlanma | Yeni bağlantı temiz `ready` alır; servis ayakta kalır |

## Aşama-2 — Gateway + Mobile (ÖN ŞART: #106 merge + staging)
| # | Senaryo | PASS kriteri |
|---|---|---|
| G1 | Login → meeting başlat (gateway auth) | Oturum + X-Correlation-Id uçtan uca |
| G2 | Client → audio-gateway → Redis → live-stt | STREAMING state (#102), chunk admission |
| M1 | expo-audio capture → WS gateway | Chunk akışı, kayıpsız |
| M2 | Draft→final state machine UI (#87) | draft/stabilizing/final/revised geçişleri |
| M3 | Arka plana alma → dönüş | Devam veya temiz duraklatma |
| M4 | Düşük bant simülasyonu | Backpressure kuyruklar, oturum düşmez |

Mobil koşular #94 harness'i ile (Detox + Maestro), rapor CI artefaktı.
**Gerçek kişisel veri kullanılmaz** — sabit test cümleleri.

## Kabul
- Aşama-1: 6/6 PASS → G8'in direct-WS yarısı yeşil (sonuçlar aşağı işlenir).
- Aşama-2: 6/6 PASS → G8 tamamen yeşil → #57 kapanır.
- Herhangi bir FAIL → cutover bloke, issue açılır.

## Koşu kayıtları
### Aşama-1 — (tarih/commit işlenecek)
| Senaryo | Sonuç | Not |
|---|---|---|
| S1 | | |
| S2 | | |
| S3 | | |
| S4 | | |
| S5 | | |
| S6 | | |

### Aşama-1 koşusu — 2026-06-10, GPU host (RTX 4070), main @ PR #131 sonrası
Kurulum: live-stt :8200 (cuda/fp16) + meeting-ai :8300 (mock) + cloudflared
quick tunnel (IPv4 origin: http://127.0.0.1:8200 — localhost IPv6/::1 tuzağına
dikkat) + stt-frontend (vite, laptop) `?ws=` parametresiyle.

| Senaryo | Sonuç | Not |
|---|---|---|
| S1 | **PASS** | İlk draft <3 sn (bağlantı dahil); draft inference 215-401 ms; live=medium, final=large-v3-turbo (ADR-0031 defaults) |
| S2 | **PASS** | Final'ler draft'ı revize etti (515-788 ms); ör. draft "güzel çalışıyorsun" → final "Güzel çalışıyorsun, beğendim." |
| S3 | **PASS** | Segment aralarındaki 9-19 sn boşluklarda hiçbir halüsinasyon artefaktı UI'a düşmedi |
| S4 | **PASS** | /analyze: `redacted:true`, `redaction_count:2`; TC→***REDACTED_TC***, telefon→***REDACTED_PHONE***; karar+aksiyon doğru ayıklandı |
| S5 | **PASS** | Sunucu logları transcript-free: yalnız `Stream connected` / `Final segment sent` / süre+VAD satırları; konuşulan metinden tek kelime yok |
| S6 | **PASS** | F5 sonrası yeniden bağlantı temiz `ready`; servis kesintisiz (logda disconnect→reconnect→final akışı görülür) |

**Aşama-1 = 6/6 PASS → G8'in direct-WS yarısı YEŞİL.** Kalan: Aşama-2
(gateway+mobil; ön şart #106 merge + staging).
