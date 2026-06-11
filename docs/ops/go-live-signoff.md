# #59 Production Go-Live Sign-off — Faz 24 acceptance

**Kural:** Aşağıdaki TÜM gate'ler yeşil + müdür imzası olmadan D30 cutover
(#56) tetiklenmez. Bu dosya tek doğruluk kaynağıdır; her gate kapanışında
tarih+kanıt linki işlenir.

| Gate | İçerik | Kanıt | Durum |
|---|---|---|---|
| G1 | Kod main'de (STT/GPU/streaming/skeleton'lar) | PR #107-#147; CI workflow aktif, 4/4 servis yeşil (run 27330362640) | ✅ 2026-06-10 |
| G2 | GPU host PROD deploy (kalıcı, boot-dayanıklı) | deploy/gpu-host/ (PR #134-#145, Scheduled Tasks); canlı mikrofon e2e PASS; Halil review ✓ (şart: ADR-0031 D5 amendment → G11) | ✅ 2026-06-11 |
| G3 | Model + donanım kararları | ADR-0031, #40 final | ✅ 2026-06-10 |
| G4 | Hukuk: ADR-0030 ACCEPTED | #52 paketi: docs/legal/ | ⏳ danışman/risk kabulü |
| G5 | VERBIS karar/güncelleme | #53: docs/legal/verbis-... | ⏳ operatör |
| G6 | LLM Option A/B onayı | #54 CLOSED: Option B operatör onaylı (PR #133), Ollama GPU host'ta canlı | ✅ 2026-06-10 |
| G7 | MinIO prod kurulumu | #55: deploy/minio/ apply | ⏳ host |
| G8 | Browser smoke 10/10 PASS | Aşama-1 6/6 PASS — Halil KABUL ✓ (2026-06-11); staging kurulumunu Halil tarafı üstlendi (#57 yorumu); Aşama-2'de screenshot+console+network artifact ZORUNLU; parçalı koşu kabul (G1+G2 önce, M1-M4 harness sonrası) | ⏳ staging (Halil kuruyor) |
| G9 | Rollback provası (snapshot restore test) | #58: docs/ops/warm-rollback... | ⏳ prova |
| G10 | Pilot WER kalibrasyonu (consent sonrası) | #34 protokol, #36 Ek-A | ⏳ consent |
| G11 | Açık mühendislik riskleri kabul/kapama | #62 CLOSED ✓ (producer CDC live + 3 istemci repoya strict-tracking devri); #65 ADR-0032 iter-2 (P1/P2 işlendi, ACCEPTED bekler); YENİ: ADR-0031 D5 amendment — Windows GPU host + Scheduled Tasks deploy modelini mühürle veya k3s geçiş planı (Halil G2 review şartı) | ⏳ karar |

**İmza bloğu**
- Teknik hazır beyanı: ____________ (Zeynep) tarih: ______
- Cross-AI review kaydı: ____________
- Go-live onayı: ____________ (müdür) tarih: ______

İmza sonrası: #56 D30 penceresi planlanır → cutover → 72h warm (#58) →
sorunsuz kapanışta #59 CLOSED.
