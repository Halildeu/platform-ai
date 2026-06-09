# Faz 24 Risk Register — Status & Evidence (#61–#67)

Maps each Faz 24 risk-register issue to its issue-defined mitigation **plus the
concrete evidence produced by completed work** (#17–#43, #97, #106). Risk
ownership/closure remains an operator/governance decision; this document records
status and evidence only.

Status legend: 🟢 mitigated (evidence) · 🟡 partially mitigated · 🔴 open (no work yet)

| Risk | Status | Owner |
|---|---|---|
| #61 R-HW-1 hardware idle | 🟡 lowered | operator (HW investment) |
| #62 R-CONTRACT-1 gateway drift | 🟡 partial | gateway/contract |
| #63 R-LEAK-1 worker leak | 🟢 mitigated | STT |
| #64 R-WER-1 Turkish WER low | 🟡 lowered (CV only) | STT |
| #65 R-MT-1 multi-tenant retro | 🟡 partial | architecture |
| #66 R-MOBILE-1 RN harness | 🔴 open | mobile |
| #67 R-RESOURCE-1 staging OOM | 🟡 partial | observability/ops |

---

## #61 — R-HW-1: Donanım atıl kalma (GPU yatırım vs SaaS) 🟡

- **Risk:** RTX 4070 erken yatırım → WER PoC farklı çıkarsa (ör. medium yeterli) → GPU atıl. Impact ~25k TL.
- **Issue mitigation:** WER PoC önce → karar; cloud GPU bridge ara çözüm.
- **Evidence / current status:**
  - #40 provisional decision: **mevcut** RTX 4070 dev/PoC hedefi olarak seçildi — **yeni donanım satın alınmadı** (yatırım riski ertelendi).
  - #43 WER ölçüldü: medium 20.8%, large-v3 18.3% (Common Voice TR). medium tek başına **kabul edilebilir aralıkta** → "medium yeterli olabilir" senaryosu veriyle destekleniyor.
  - #42 VRAM: medium ~2 GB/worker, 8 GB'a 2–3 paralel akış.
- **Residual:** Nihai yatırım kararı pilot WER (#35/#36) + operatör onayı bekliyor. Cloud bridge hâlâ ara seçenek.

## #62 — R-CONTRACT-1: Gateway contract drift 🟡

- **Risk:** PR-gw-01 kontratı donduktan sonra mobile/web/STT iki yere bağlı → sessiz drift.
- **Issue mitigation:** Consumer-driven contract test + OpenAPI schema validation + CI gate.
- **Evidence / current status:**
  - #106 audio-gateway-service'te **contract testleri mevcut** (`ChunkAdmissionContractTest`, `ChunkDispatchContractTest`, `SessionLifecycleContractTest`, `StartSessionContractTest`).
  - Sealed `DispatchOutcome` arayüzü (#421) tip-güvenli kontrat sağlıyor.
  - OpenAPI (`/openapi.json`) HTTP route'ları expose ediyor.
- **Residual:** Cross-repo (mobile/web/STT) consumer-driven contract + CI gate kurulumu hâlâ açık (ayrı PR'lar).

## #63 — R-LEAK-1: Worker thread/process leak 🟢

- **Risk:** PR-stt-03 subprocess isolation yetersiz → worker arka planda devam eder → memory leak / CPU saturation / queue lock.
- **Issue mitigation:** Supervised pattern + monitoring alert + recovery worker.
- **Evidence / current status:**
  - #20/#21/#22: subprocess worker + **hard timeout kill** + worker restart (testli).
  - #42 `ProcessWorkerPool`: çökme→respawn, timeout→terminate/kill+respawn, `worker_kill_grace_sec`; supervised single-flight slot.
  - Metrik: `stt_worker_killed_total`, `stt_timeout_total`.
- **Residual:** Üretim altında uzun-süre sızıntı gözlemi (staging monitoring) hâlâ doğrulanmalı.

## #64 — R-WER-1: Türkçe doğruluk düşük kalır 🟡

- **Risk:** medium int8 Türkçe WER yetersiz (>%25); large-v3-turbo da düşük.
- **Issue mitigation:** WER PoC erken + 3rd-party SaaS (Azure Speech, AssemblyAI) backup.
- **Evidence / current status:**
  - #43 ölçüldü (Common Voice TR, 989 kelime): **medium %20.8, large-v3 %18.3** → **ikisi de %25 eşiğinin ALTINDA.** Bu senaryoda risk büyük ölçüde gerçekleşmedi.
  - İki-katmanlı tasarım (medium live + large-v3 final) #43'te doğrulandı.
- **Residual:** **Common Voice ≠ gerçek toplantı.** Pilot meeting WER (#35/#36) hâlâ eksik; gürültülü/jargonlu toplantıda WER yükselebilir. SaaS backup planı hâlâ geçerli yedek.

## #65 — R-MT-1: Multi-tenant retroactive eklemek pahalı 🟡

- **Risk:** MVP tek tenant; sonradan multi-tenant ayrımı tüm Gateway/STT/DB refactor gerektirir.
- **Issue mitigation:** Başlangıçtan `tenantId` reserved + contract'ta required.
- **Evidence / current status:**
  - #106 audio-gateway: `tenantId` **birinci sınıf alan** — per-tenant Redis Stream bucketing `meeting:chunks:{tenantId}`; `ChunkDispatchCommand.tenantId` zorunlu.
- **Residual:** STT/DB/transcript katmanlarında tenant izolasyonu (row-level/schema) hâlâ tasarlanmalı.

## #66 — R-MOBILE-1: RN test harness yetersiz 🔴

- **Risk:** Browser MCP mobile için yetmez; computer-use disconnect olur.
- **Issue mitigation:** Expo dev preview + Detox e2e + EAS Build pipeline; mobile test harness ADR.
- **Evidence / current status:** **Çalışma yapılmadı** — mobil, bu STT/AI hattının kapsamı dışında.
- **Residual:** Tümü açık. Mobil ekip / ayrı harness ADR gerekiyor.

## #67 — R-RESOURCE-1: Staging resource exhaustion 🟡

- **Risk:** staging-sw 23 GiB RAM / 6.2 GiB available; Faz 22.5 + 23 + STT PoC aynı host → sessiz OOM.
- **Issue mitigation:** Her PR-stt-* öncesi resource pressure acceptance gate (`free -m` + `kubectl top`).
- **Evidence / current status:**
  - #19 two-host resource baseline (AG-019) raporlandı.
  - #42 VRAM ölçümü: per-worker ~2 GB, **K=4'te 8 GB OOM** bulundu → kapasite sınırı somut.
  - #42 opsiyonel VRAM guard (`STT_WORKER_VRAM_BUDGET_MB`) OOM çöküşünü önler.
- **Residual:** Host-seviye RAM acceptance gate otomasyonu + staging gerçek ölçümü (AG-019) hâlâ operatör tarafında açık.

---

## Özet

Risk register'ın **6/7'sine** tamamlanmış işten somut kanıt/azaltma bağlandı; yalnızca **#66 (mobil)** kapsam dışı ve açık. **#64 (WER)** ve **#63 (worker leak)** ölçüm/koda dayalı en güçlü azaltmalar. Tüm risklerin **nihai kapanışı operatör/governance kararı** — bu doküman kanıt+durum kaydıdır, kapanış değil.
