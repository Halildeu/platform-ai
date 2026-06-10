# #57 Browser Smoke Acceptance — e2e (mobile + web) çalıştırma kılavuzu

**Amaç:** Cutover gate G8. Gerçek tarayıcı/mobil istemciden uçtan uca akışın
kabulü. Harness #94'te teslim edildi (Detox e2e + Maestro flow + browser MCP
wrapper); bu doküman **kabul senaryolarını ve PASS kriterlerini** sabitler.

## Web (browser MCP / manuel)
| # | Senaryo | PASS kriteri |
|---|---|---|
| W1 | Login → meeting başlat → mikrofon izni | İzin akışı hatasız, oturum açılır |
| W2 | 30 sn konuşma → canlı draft görünür | İlk partial < 2 sn, draft akışı kesintisiz |
| W3 | Sessizlik → final segment | Final, draft'ı revize eder; halüsinasyon yok |
| W4 | Meeting bitir → özet/karar/aksiyon | meeting-ai yanıtı redacted, alanlar dolu |
| W5 | Transcript'te PII denemesi (test verisi: "TC: 12345678901") | UI'da ***REDACTED_TC*** görünür |
| W6 | Ağ kopması 5 sn → yeniden bağlanma | Oturum düşmez veya temiz hata + devam |

## Mobile (Detox/Maestro — #94 harness)
| # | Senaryo | PASS kriteri |
|---|---|---|
| M1 | expo-audio capture → WS gateway | Chunk'lar akar, STREAMING state (#102) |
| M2 | Draft→final state machine UI (#87) | draft/stabilizing/final/revised geçişleri |
| M3 | Arka plana alma → geri dönme | Kayıt devam veya temiz duraklatma |
| M4 | Düşük bant genişliği simülasyonu | Backpressure: 429/503 değil, kuyruk |

## Çalıştırma
- Web: staging URL + test hesabı; her senaryo ekran kaydı ile arşivlenir.
- Mobile: `maestro test flows/` + `detox test` (#94 pipeline); rapor CI artefaktı.
- **Gerçek kişisel veri KULLANILMAZ** — sabit test cümleleri + sentetik ses.

## Kabul
6/6 web + 4/4 mobile PASS → G8 yeşil. Herhangi bir FAIL → cutover bloklanır,
issue açılır. Sonuçlar bu dosyanın altına tarih+commit ile eklenir.

## Koşu kayıtları
*(henüz koşulmadı — staging penceresi bekleniyor)*
