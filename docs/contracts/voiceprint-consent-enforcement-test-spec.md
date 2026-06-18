# Voiceprint Consent-Enforcement Fail-Closed — Test Spec (§5.6 + G9 + G13/G14)

> **Ne için:** Voiceprint **inşa edildiğinde** geçmesi ZORUNLU fail-closed enforcement sözleşmesi.
> `voiceprint-m6-hukuk-sureci.md` **§5.6** (consent-enforcement) + **G9** (negatif-test) + **G13/G14** (enablement
> governance + revalidation) için **machine-checkable** kanıt. Kod henüz yok (capability default-off, Pydantic
> `DIA_` env-flag); bu spec o kodun kontratıdır — voiceprint servisi yazılınca CI-gate olur.
>
> **Eşlik eden şema:** [`voiceprint-enablement-audit.schema.json`](./voiceprint-enablement-audit.schema.json) (v2;
> append-only **hash-chain ledger**; her enable/disable/revalidate aksiyonu şemaya valide kayıt yazmadan yapılamaz).
>
> **Çekirdek ilke — fail-closed:** Şüphe/eksik/kapalı/hata durumunda **biyometrik işleme OLMAZ** (artefakt
> üretilmez). "Aktif" olmak için **üçü birden**: (a) capability flag açık + (b) geçerli rıza + (c) bağlam ledger'ında
> **son hash-chain-doğrulanmış kayıt** = enable/revalidate(confirmed), resulting_state=enabled, süresi dolmamış,
> açık G14 needs-review yok.

---

## 1. Enforcement invariant'ları

| # | Invariant | Gate | Schema mı / Runtime mı |
|---|---|---|---|
| I1 | **Default-off:** flag yok/false → 0 enrollment/matching/vektör/kalıntı | §0.5/§5.6 | runtime |
| I2 | **Rıza yoksa işleme yok** (flag açık olsa bile) → 403 + 0 vektör | §2/§5.6 | runtime |
| I3 | **Gated enablement zorunlu:** son geçerli ledger kaydı (enable) + dual-approval olmadan flag yetmez (server-side) | G13 | schema + runtime |
| I4 | **Geri çekme = imha:** vektör geri-döndürülemez sil + matching dur + geçmiş etiket pseudonymize | G7/D11 | runtime |
| I5 | **Kill-switch:** disable → anında dur, yeni artefakt yok | §0.5 | runtime |
| I6 | **Gerçek-ses test sınırı:** enablement'sız gerçek ses bloklanır (yalnız sentetik fixture) | §0.5 | runtime |
| I7 | **Audit bütünlüğü:** her aksiyon → şemaya valide **hash-chain** kayıt; invalid kayıtla aksiyon reddedilir | G13 | schema + runtime |
| I8 | **Consequential-use guard:** voiceprint çıktısı İK/performans/disiplin/hukuki sink'e bağlanamaz | §2.0 | static+runtime |
| I9 | **Atomicity:** audit kaydı yazılmadan enable aktif olamaz; state aktif olup audit fail ederse rollback | G13 | runtime |
| I10 | **Chain continuity:** record_hash = H(canonical_payload_hash ‖ previous_record_hash); zincir kopuk/tampered → reddedilir | G13 | runtime |
| I11 | **Expiry runtime:** expiry_review_date geçmişse enablement **otomatik inactive/needs-review** (yalnız kayıt alanı değil) | G14 | runtime |
| I12 | **Distinct approver:** dual-approval **iki FARKLI principal** (aynı kişi iki rol ≠ çift-onay) | G13 | runtime (schema-limit) |
| I13 | **Leak yok:** vektör/embedding/raw-fingerprint/identity/confidence log/trace/metric/temp/model-cache'e sızmaz | §5.5 | runtime |

---

## 2. Negatif test seti (given / when / then) — "reddedildi/işleme olmadı" = PASS

**Çekirdek (T1-T9):**
- **T1 (I1) default-off:** flag unset → enroll/match `403/disabled` + 0 artefakt.
- **T2 (I2) no-consent:** flag açık + enablement geçerli + rıza YOK → `403` + 0 vektör + audit "consent_missing".
- **T3 (I3) un-gated bypass:** flag açık + rıza var ama **geçerli ledger enable kaydı yok** (veya şemaya invalid) → reddedilir (UI toggle atlanamaz).
- **T3b (I12) same-principal dual-approval:** approvers iki satır farklı rol ama **aynı approver_id** → reddedilir (runtime; şema yakalamaz).
- **T4 (I4) withdrawal:** enrolled → rıza çekildi → vektör irreversible sil + sonraki match `unknown` + geçmiş etiket pseudonymize.
- **T5 (I5) kill-switch:** disable → anında match dur + yeni vektör yok + ledger'a disable kaydı.
- **T6 (I6) real-voice guard:** enablement yok + gerçek ses → bloklanır; yalnız `synthetic=true` (server-trusted) geçer.
- **T7 (I7) audit-required:** audit kaydı şemaya invalid (consequential=true / tek-approver / external=true) → enable **reddedilir** (kayıt yazılamaz → aksiyon yok).
- **T8 (I8) consequential-use:** İK/performans endpoint voiceprint çıktısını tüketmeye çalışır → static-analiz/CI + runtime guard reddeder.
- **T9 (low-confidence):** düşük confidence → otomatik isim YOK → anonim `SPEAKER_xx` fallback (bias/yanlış-atıf mitigasyonu).

**Fail-OPEN testleri (T10-T21 — Codex 019edcc8; partial-failure/race/persistence kaçaklarını kapar):**
- **T10 partial-failure:** consent-service / audit-ledger / policy-registry / DB / cache / queue **down/timeout** → enroll/match fail-closed + 0 artefakt (fail-open DEĞİL).
- **T11 (I9) atomicity:** audit kaydı yazılmadan enable state aktif olamaz; state aktif olup audit write fail ederse **rollback** (yarım enable yok).
- **T12 race/TOCTOU:** withdrawal/disable ile **eşzamanlı** enroll/match → sonuç her zaman deny/purge.
- **T13 stale-cache/replica:** eski consent veya eski enablement cache'iyle matching yapılamaz; disable/withdrawal sonrası **güçlü tutarlılık veya revocation-epoch**.
- **T14 queued/background:** disable/withdrawal **öncesi** kuyruğa girmiş enroll/match job'ları iptal; DLQ/temp/vektör kalıntısı yok.
- **T15 backup/restore resurrection:** restore eski vektörü geri getiremez; **erasure tombstone/deny-list** restore sonrası da uygulanır.
- **T16 (I13) leak:** vektör/embedding/raw-audio-fingerprint/identity/confidence **log/trace/metric/temp/model-cache'e sızmaz**.
- **T17 (I11) retention/expiry:** `expiry_review_date` geçmiş → enablement otomatik inactive/needs-review.
- **T18 (G14) context-mutation:** participant/meeting-type/vendor-telemetry/transfer/retention/controller değişimi → G14 needs-review **fail-closed**.
- **T19 multi-instance kill-switch:** bir instance disable görür → diğeri eski cache ile match yapamaz.
- **T20 synthetic-flag spoofing:** `synthetic=true` **client-provided** ise gerçek-ses guard'ı bypass etmez (fixture trust-boundary server-side).
- **T21 (I8) consequential-use lineage:** voiceprint-türevli çıktı **veri-etiketi** taşır; downstream sink guard etiketi **runtime'da** da reddeder.

---

## 3. Şema invariant testi (machine-checked — bu PR'da koştu ✓)

`voiceprint-enablement-audit.schema.json` (v2) **valid Draft 2020-12** + valid **enable/disable/revalidate** örnekleri
geçer + invalid'ler reddedilir (`jsonschema` + `FormatChecker`). Bu PR'da doğrulandı:

- VALID: enable (genesis seq=0, null previous) · disable (light: reason+erasure+actor+rbac, gate-evidence GEREKMEZ) · revalidate (supersedes+changed_fields+result) · gate_ref `not_applicable_reason`.
- INVALID red: consequential_use=true · tek-approver · aynı-rol · external=true · verbis before=false · **gate_evidence eksik/yok** (enable) · re-review required=true triggers yok / **triggers=[]** · default_state=enabled · server_side=false · **genesis-with-previous / non-genesis-null** (chain) · **disable reason/erasure yok** · revalidate `needs_review` ama `needs_review_evidence_id` yok · gate_ref boş/iki-alan/sneaky · additional-property · **bad date-time** · **revalidate result↔state mismatch** (confirmed↔enabled / needs_review↔needs_review / disabled↔disabled dışı 6 kombinasyon red — schema-enforced, Codex 019edcc8 doğruladı).

> **action-branch disiplini:** `enable`/`revalidate` = full gate evidence (G2-G14) + dual-approval + VERBİS-before; `disable` = HAFİF (reason+erasure+actor+rbac) — *disable'ı over-constrain etmek fail-OPEN üretirdi* (Codex). 

---

## 4. Runtime invariant'ları (şema YAKALAMAZ — kod + test sağlar)

JSON Schema yapı doğrular; şu cross-record/cross-field ilişkiler **runtime + test** ile sağlanır (dürüst sınır):

- **R1 distinct principal** (T3b): approver_id'ler farklı kişi/principal.
- **R2 expiry > decided_at** (T11/T17): süre mantıklı + geçmişse inactive.
- **R3 chain continuity** (T10/I10): `record_hash` = H(payload ‖ previous_hash); `previous_record_id/hash` gerçek önceki kayıtla eşleşir; append-only.
- **R4 latest-active-record check** (I3): capability aktif ⇔ bağlamın **son** chain-doğrulanmış kaydı enable/revalidate(confirmed) + enabled + süresi dolmamış + açık needs-review yok + **consent hâlâ geçerli**. Runtime "herhangi valid enable kaydı var mı?" DEĞİL, "son yürürlükteki kayıt ne?" diye bakar.
- **R5 tamper-evidence anchoring** (Codex 019edcc8 secondary): `signature_ref` şemada optional ama prod kontratında **zorunlu** olmalı — kayıtlar KMS/HMAC ile imzalanır + **WORM / external-ledger anchoring** (append-only, silinemez). Schema tek-kayıt yapısını doğrular; immutability'nin kendisi storage-layer + runtime invariant'ıdır.

---

## 5. CI bağı

- **Şimdi (kod yok):** §3 şema-invariant testi koşar (schema valid + örnekler) — bu PR'da geçti.
- **Voiceprint servisi yazılınca:** §2 (T1-T21) o servisin test-suite'ine girer (CI-gate); enablement endpoint'i her aksiyonda audit kaydını şemaya valide eder + R1-R5 runtime invariant'larını uygular (yazılamaz/doğrulanamazsa aksiyon yok = fail-closed).
- **Enforcement noktası:** `DIA_VOICEPRINT_ENABLED` (default false; diarization-service `DIA_` env) + server-side ledger/consent/expiry check → fail-closed.

*Hazırlık: AI ajanı + cross-AI (Codex 019edcc8) istişare, Halil adına — 2026-06-19. Hukuki görüş değil; G9/G13 mühendislik-kanıt sözleşmesi.*
