# #59 Production Go-Live Sign-off — Faz 24 acceptance

**Kural:** Aşağıdaki TÜM gate'ler yeşil + müdür imzası olmadan D30 cutover
(#56) tetiklenmez. Bu dosya tek doğruluk kaynağıdır; her gate kapanışında
tarih+kanıt linki işlenir.

| Gate | İçerik | Kanıt | Durum |
|---|---|---|---|
| G1 | Kod main'de (STT/GPU/streaming/skeleton'lar) | PR #107-#147; CI workflow aktif, 4/4 servis yeşil (run 27330362640) | ✅ 2026-06-10 |
| G2 | GPU host PROD deploy (kalıcı, boot-dayanıklı) | deploy/gpu-host/ (PR #134-#145, Scheduled Tasks); canlı mikrofon e2e PASS | ✅ 2026-06-11 |
| G3 | Model + donanım kararları | ADR-0031, #40 final | ✅ 2026-06-10 |
| G4 | Hukuk: ADR-0030 ACCEPTED | #52 paketi: docs/legal/ | ⏳ danışman/risk kabulü |
| G5 | VERBIS karar/güncelleme | #53: docs/legal/verbis-... | ⏳ operatör |
| G6 | LLM Option A/B onayı | #54 CLOSED: Option B operatör onaylı (PR #133), Ollama GPU host'ta canlı | ✅ 2026-06-10 |
| G7 | MinIO prod kurulumu | #55: deploy/minio/ apply | ⏳ host |
| G8 | Browser smoke 10/10 PASS | Aşama-1 6/6 PASS; Aşama-2 #106 merge ile bloksuz — runbook: stage2-smoke-runbook.md | ⏳ staging koşusu |
| G9 | Rollback provası (snapshot restore test) | #58: docs/ops/warm-rollback... | ⏳ prova |
| G10 | Pilot WER kalibrasyonu (consent sonrası) | #34 protokol, #36 Ek-A | ⏳ consent |
| G11 | Açık mühendislik riskleri kabul/kapama | #62: cross-server CDC kanıtlı (#534+#138), mobile/web CDC kaldı; #65: ADR-0032 taslak | ⏳ karar |

**İmza bloğu**
- Teknik hazır beyanı: ____________ (Zeynep) tarih: ______
- Cross-AI review kaydı: ____________
- Go-live onayı: ____________ (müdür) tarih: ______

İmza sonrası: #56 D30 penceresi planlanır → cutover → 72h warm (#58) →
sorunsuz kapanışta #59 CLOSED.
