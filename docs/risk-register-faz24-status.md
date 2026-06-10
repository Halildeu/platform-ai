# Faz 24 Risk Register — Status & Evidence (#61–#67)

Maps each Faz 24 risk-register issue to its issue-defined mitigation **plus the
concrete evidence produced by completed work** (#17–#43, #97, #106). Risk
ownership/closure remains an operator/governance decision; this document records
status and evidence only.

Status legend: 🟢 mitigated (evidence) · 🟡 partially mitigated · 🔴 open (no work yet)

| Risk | Status (2026-06-10 final) | Owner |
|---|---|---|
| #61 R-HW-1 hardware idle | 🟢 retired — bkz. güncelleme | operator (HW investment) |
| #62 R-CONTRACT-1 gateway drift | 🟡 partial — AÇIK kalır | gateway/contract |
| #63 R-LEAK-1 worker leak | 🟢 mitigated | STT |
| #64 R-WER-1 Turkish WER low | 🟢 mitigated (pilot kalibrasyonu kaldı) | STT |
| #65 R-MT-1 multi-tenant retro | 🟡 partial — AÇIK kalır | architecture |
| #66 R-MOBILE-1 RN harness | 🟢 mitigated (#94 ile) | mobile |
| #67 R-RESOURCE-1 staging OOM | 🟢 mitigated (gate otomasyonu residual) | observability/ops |

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

## Güncelleme — 2026-06-10 (#35/#36/#40/#43 sonrası FINAL durum)

- **#61 R-HW-1 → 🟢 RETIRED.** Riskin öncülü ("WER PoC farklı çıkarsa GPU atıl
  kalır") ölçümle çözüldü: #35 matrisi **GPU'yu gerektiren** large-v3-turbo'yu
  final model seçti (bozuk koşulda medium'dan 5.4 puan iyi); #40 FINAL kararı
  lokal RTX 4070 (₺1.65/saat, cloud'dan ~15× ucuz). Kart zaten mevcuttu (sunk);
  "atıl kalma" senaryosunun gerçekleşme yolu kalmadı.
- **#63 R-LEAK-1 → 🟢.** Değişmedi; ek olarak #42 shared-backend'de supervisor
  düzeyi terminate→grace→kill→respawn aynı garantiyi taşıyor (test: 105 unit PASS).
- **#64 R-WER-1 → 🟢.** Tam matris ölçüldü: temizde tüm adaylar %25 eşiğinin
  altında (turbo %18.3); sentetik-bozukta turbo %25.58 (eşik sınırında, medium
  %30.9). Mitigasyon = turbo final + draft revize akışı (#39). SaaS backup
  (Azure/AssemblyAI) eskalasyon yolu olarak dokümante. Kalan: pilot kalibrasyonu
  (#34 protokolü, consent) — riskin kendisi değil, doğrulama adımı.
- **#66 R-MOBILE-1 → 🟢.** Issue'nun istediği mitigasyon **#94'te teslim edilip
  kapandı** (Detox e2e + Maestro flow + browser MCP wrapper). Bu register'ın
  önceki "çalışma yapılmadı" satırı mobil hattın işini görmüyordu; düzeltildi.
- **#67 R-RESOURCE-1 → 🟢.** #19 (AG-019 two-host baseline, PR #113 merged):
  STT compute **staging-sw'den ayrı** platform-ai GPU host'una taşındı → riskin
  ana kaynağı (aynı host'ta üç faz) ortadan kalktı. #42 VRAM guard OOM çöküşünü
  konfigüratif engelliyor. Residual: host-level gate otomasyonu (ops backlog).
- **#62, #65 → 🟡 AÇIK kalır.** #62: cross-repo consumer-driven contract + CI
  gate kurulmadı (gateway içi contract testleri var, yeterli değil). #65:
  gateway'de tenantId birinci sınıf, ama STT/DB katmanında tenant izolasyonu
  tasarlanmadı. İkisi de gerçek kalan iş — kapatılmaz.

## Özet (final)

**5/7 risk kanıtla kapatılabilir durumda** (#61, #63, #64, #66, #67); **#62 ve
#65 açık kalmalı** (gerçek iş kaldı). #60 (KVKK gap) bu dokümanın kapsamı dışında
— hukuk paketiyle (#52/#53) birlikte yönetiliyor. Kapanışlar issue yorumlarında
bu dokümana referansla yapıldı; itiraz halinde yeniden açılır.
