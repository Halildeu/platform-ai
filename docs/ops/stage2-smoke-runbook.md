# #57 Aşama-2 Smoke Runbook — Gateway + Mobile (staging hazır olduğunda ~1 saat)

**Ön şartlar — durum (2026-06-11):**
| Ön şart | Durum |
|---|---|
| #106 audio-gateway Redis dispatcher merge | ✅ platform-backend #534 MERGED (iter-2 APPROVE) |
| live-stt Redis consumer | ✅ platform-ai #138 MERGED (`STT_CHUNK_CONSUMER_ENABLED`) |
| Staging ortamı (gateway + Redis + WireGuard) | ⏳ operatör/Halil — TEK kalan blokaj |
| Mobil harness (#94 Detox+Maestro) | ⏳ staging ile birlikte |

## Kurulum (staging geldiği gün)

1. **Redis** (staging-sw): `live-stt-v1` consumer group, 32 partition
   (`audio:chunks:p00..p31`) — producer/consumer zaten bu kontrata göre merge'li.
2. **audio-gateway** (platform-backend): `audio.gateway.dispatcher.mode=redis`,
   `spring.redis.*` staging adresi.
3. **live-stt** (GPU host): mevcut prod kuruluma yalnız env ekle:
   `STT_CHUNK_CONSUMER_ENABLED=true`, `STT_REDIS_URL=redis://<staging>:6379/0`
   → task restart (`Stop-ScheduledTask` + `Start-ScheduledTask platform-ai-live-stt`).

## Koşu sırası (browser-smoke-acceptance.md Aşama-2 tablosu)

| # | Senaryo | Nasıl koşulur |
|---|---|---|
| G1 | Login → meeting başlat | Staging web istemcisi; X-Correlation-Id'yi gateway+live-stt loglarında eşle |
| G2 | Client → gateway → Redis → live-stt | Konuş; gateway 200/Accepted, Redis `XLEN audio:chunks:p*` artar, live-stt consumer logunda chunk işleme |
| M1 | expo-audio capture → WS gateway | #94 Maestro akışı `record-basic` |
| M2 | Draft→final state machine UI | #94 Detox `draft-final-transitions` |
| M3 | Arka plan → dönüş | Maestro `background-resume` |
| M4 | Düşük bant simülasyonu | Detox network-condition profili; gateway 429 + Retry-After gözle (backpressure) |

**KVKK koşu kuralı:** gerçek kişisel veri YOK — sabit test cümleleri;
loglar transcript-free kalır (S5 kuralı Aşama-2'de de geçerli).

## Kabul / kayıt

- 6/6 PASS → browser-smoke-acceptance.md'ye sonuç tablosu işlenir →
  **G8 tamamen yeşil** → #57 kapanır.
- Herhangi FAIL → cutover bloke, bulgu issue'su açılır (Faz 23 deseni).

## Hata ayıklama kopya kağıdı

- Gateway 503 + Retry-After 30s → Redis/WireGuard bağlantısı (D8 failure modes)
- Gateway 429 + Retry-After 10s → consumer lag; live-stt consumer ayakta mı?
- live-stt chunk almıyor → consumer group/partition adları birebir mi
  (`audio:chunks:p`, `live-stt-v1`), `STT_CHUNK_CONSUMER_ENABLED=true` mi?
- Dedup şüphesi → consumer `messageId` (sessionId:chunkSeq) okur, Redis entry
  ID DEĞİL (#534 Javadoc kontratı).
