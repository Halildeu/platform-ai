# Onboarding — Zeynep Serban (`@zeynep-serban`)

> **Hoşgeldin Zeynep!** Bu doküman `platform-ai` repo'sundaki **Python STT + diarization + meeting-AI** scope'u için onboarding rehberidir. Faz 24 Meeting Intelligence platformunun ortak geliştiricilerinden birisin; aşağıda board hierarchy, scope'un, atanan iş yükün, geliştirme akışı, cross-AI peer review pattern'i ve ilk hafta yol haritası yer alıyor.

---

## 1. Sen ve Scope'un

`platform-ai` repo'su **Faz 24 Meeting Intelligence** platformunun **Python compute plane**'idir. Spring Boot orchestration (`platform-backend/audio-gateway-service`) staging-sw'da çalışırken `platform-ai` **ayrı dedicated host'ta** (ADR-0031 two-server topology) konumlanır.

**Sen sahipsin** (Python compute plane):

- `services/live-stt-service` (FastAPI + faster-whisper Whisper medium int8) — PoC MERGED, sıradaki iterasyonlar
- `services/final-stt-service` (skeleton mevcut, Issue #39 ile state machine scaffolding — model seçimi WER sonrası kilitlenir)
- `services/diarization-service` (skeleton açılacak — Issue #48, pyannote.audio)
- `services/meeting-ai-service` (skeleton açılacak — Issue #49, LLM özet/karar/aksiyon; KVKK/secret boundary nedeniyle dikkatli sırala)
- WER raporu (Common Voice TR + pilot meeting triangulate; PR-wer-01)
- GPU stratejisi (Dockerfile.gpu — Issue #41/#42; donanım kararı CPU PoC + WER + Gate B sonrası — Issue #40)
- PII redaction regression iyileştirme (Issue #97)

**Sen sahibi DEĞİLSİN** (paralel takım):

- `platform-backend/audio-gateway-service` (Spring WebFlux Gateway — Claude/Halil)
- `platform-mobile` / `platform-desktop` (RN + Electron — M6 client team)
- `platform-web/apps/mfe-meeting` (React MFE — M6 web team)
- `platform-k8s-gitops` (Kustomize + ArgoCD — Halil)

---

## 2. Board Hierarchy + Atanan Iş Yükün

**Board precedence** (önemli — `AGENTS.md` ile uyumlu):

| Board | URL | Rol |
|---|---|---|
| **Project #2 — platform Roadmap** | https://github.com/users/Halildeu/projects/2 | **Umbrella** — cross-repo platform-level aktif iş durumu (AGENTS.md Section 1 canonical) |
| **Project #4 — platform-ai Faz 24 Meeting Intelligence** | https://github.com/users/Halildeu/projects/4 | **Faz 24 platform-ai contributor workload canonical** — bu repo'daki Python STT/diarization/meeting-AI işleri için board (Zeynep'in görev yüzeyi) |

Karışıklık olmasın diye: Project #2 platform-genel, Project #4 senin günlük board'un (`platform-ai` Faz 24 scope).

### Sana atanan açık 8 issue (`assignee: @zeynep-serban`)

| # | Title | Milestone | Not |
|---|---|---|---|
| [#97](https://github.com/Halildeu/platform-ai/issues/97) | `[fix-pii]` PR #74 PII redaction patterns 4 test fail — regression | M3 Resilience + Observability | Quick win başlangıç |
| [#39](https://github.com/Halildeu/platform-ai/issues/39) | `[PR-final-stt-01]` Segment merge + revize state machine | M4 Accuracy | **Re-scope**: model seçimi YOK; sadece state-machine scaffolding. Model kararı WER (PR-wer-01) sonrası |
| [#48](https://github.com/Halildeu/platform-ai/issues/48) | `[platform-ai]` diarization-service skeleton (pyannote.audio) | M6 Integration | Python skeleton (mock dispatch) |
| [#49](https://github.com/Halildeu/platform-ai/issues/49) | `[platform-ai]` meeting-ai-service skeleton (LLM özet/karar/aksiyon) | M6 Integration | LLM/KVKK boundary dikkat (ADR-0030 Option A/B karar); #48'den sonra |
| [#41](https://github.com/Halildeu/platform-ai/issues/41) | `[PR-gpu-01]` Dockerfile.gpu (NVIDIA CUDA + cuDNN + faster-whisper CUDA) | M5 Performance | Plus #42 birlikte slice |
| [#42](https://github.com/Halildeu/platform-ai/issues/42) | `[PR-gpu-01]` Multi-worker GPU stream parallelism | M5 Performance | #41 ile birlikte |
| [#43](https://github.com/Halildeu/platform-ai/issues/43) | `[Performance ölçüm]` post-GPU WER + latency + memory + cost matrix güncelle | M5 Performance | GPU stack sonrası |
| [#40](https://github.com/Halildeu/platform-ai/issues/40) | `[Donanım kararı]` RTX 4070 host upgrade vs cloud GPU vs k3d-prod node-pool | M5 Performance | **Title stale**: `k3d node-pool` ifadesi ADR-0031 sonrası **revize edilecek** — compute plane `platform-ai` dedicated host + k3s ai-test/ai-prod truth'una göre ADR draft hazırlanmalı; cloud GPU bridge eski tahmin (stale) |

**Önerilen başlangıç sırası** (Codex `019e8d58` iter-1+iter-2 absorb — Section 9 ile hizalı):

1. **#97 PII fix** (quick win, CI/test akışını öğren)
2. **#48 diarization skeleton** (mock pyannote dispatch — basit FastAPI skeleton)
3. **#39 state-machine scaffolding only** (model seçimi YOK — model kararı PR-wer-01 sonrası kilitlenir; Faz 24 plan §3 #8 mutabakat noktası)
4. **#41 + #42 GPU stack** (CPU PoC + WER sonrası — Gate B baseline ile birlikte)
5. **#43 performance matrix** (GPU çalışır halde)
6. **#40 hardware ADR draft** (data-driven karar — WER + latency + cost matrix sonrası)
7. **#49 meeting-AI** (LLM/KVKK boundary + secret rotation pattern — en son)

Sıralama sen kararsızsan revize edebilirsin; Codex MCP plan-time istişaresi ile her PR'ı doğrulanır.

---

## 3. Repo Yapısı

```
platform-ai/
├── .github/                # workflows (PLACEHOLDER — Faz 24 CI setup ayrı issue scope)
├── AGENTS.md               # Agent-genel rehber (HARD RULE'lar + workflow + cross-AI + Project #2 referansı)
├── CLAUDE.md               # Claude Code session rehberi (repo-specific HARD RULE tamamlayıcı)
├── README.md               # Üst seviye proje özeti
├── docs/
│   ├── README.md           # Doc index
│   ├── adr/                # Architecture Decision Records (boş başlangıç; ana ADR'ları platform-k8s-gitops taşır)
│   ├── runbooks/           # Operasyonel runbook'lar
│   └── onboarding/         # Bu doc burada
└── services/
    ├── live-stt-service/         # MERGED — faster-whisper PoC (PR #1 4088d9a + PR-stt-02a #96)
    ├── final-stt-service/        # README + app + tests skeleton (Issue #39 ile state-machine scaffolding activate; model seçimi WER sonrası)
    ├── diarization-service/      # ⏳ skeleton açılacak (Issue #48)
    └── meeting-ai-service/       # ⏳ skeleton açılacak (Issue #49)
```

---

## 4. Canonical Plan + ADR'lar (kritik okuma)

**Faz 24 canonical plan** (öncelik 1):

- [`platform-k8s-gitops/docs/faz-24-meeting-intelligence-plan.md`](https://github.com/Halildeu/platform-k8s-gitops/blob/main/docs/faz-24-meeting-intelligence-plan.md) — 10 mutabakat noktası + 3 RED + 9 PR akışı + Gate A/B resource baseline + Risk matrix + Cross-AI mutabakat trail

**Ana ADR'lar** (öncelik 2):

- [`ADR-0030` KVKK Meeting Intelligence Boundary](https://github.com/Halildeu/platform-k8s-gitops/blob/main/docs/adr/0030-kvkk-meeting-intelligence-boundary.md) — Ses + transcript KVKK Madde 6/9 kapsamında; mobile/desktop client boundary; **Cross-Server STT Transit Boundary** (mTLS/WireGuard, audit event)
- [`ADR-0031` Two-Server Topology](https://github.com/Halildeu/platform-k8s-gitops/blob/main/docs/adr/0031-two-server-meeting-intelligence-topology.md) — `platform-ai` ayrı dedicated host + staging-sw orchestration + Redis bucketed Streams + Vault AppRole + Gate A/B resource gates
- [`ADR-0002` Single-host dual-cluster](https://github.com/Halildeu/platform-k8s-gitops/blob/main/docs/adr/0002-single-host-dual-cluster.md) — core platform baseline (supersede edilmez; ADR-0031 Faz 24 için scoped extension)

**Audio Gateway Contract** (`platform-ai` cross-server upstream — okumak ZORUNLU):

- [`platform-backend/audio-gateway-service/docs/contract-v1.md`](https://github.com/Halildeu/platform-backend/blob/main/audio-gateway-service/docs/contract-v1.md) — Gateway 1.0 contract revision 2026-06-03 (path `/api/v1/audio-gateway`, Idempotency-Key header, X-* headers JWT-derived, error envelope)

---

## 5. Cross-AI Peer Review Pattern (KRİTİK)

Bu proje **çok-modelli adversarial peer review** disiplini ile geliştirilir. Çekirdek kural:

> **Code yazan sağlayıcı kendi kodunu review/approve etmez. Review farklı sağlayıcı tarafından yapılır.** "AI" sağlayıcı seviyesinde (Anthropic, OpenAI, Google, xAI, MiniMax); aynı sağlayıcının farklı session'ı review için yetmez.

**Sağlayıcılar bu projede**:

- **Claude (Anthropic)** — Halil session'ları + ben (bu doc'u yazan)
- **Codex (OpenAI)** — MCP `mcp__codex__codex` + `mcp__codex__codex-reply` thread-based
- **Mavis (MiniMax)** — Mavis CLI `mavis communication peers/send` + provider transient unavailability durumunda non-blocking
- **Gemini (Google)** — bu proje için aktif değil (gerek olursa eklenebilir)

### Provider eşleştirme matrisi (DOĞRU pattern)

| Implementer (kod yazan) | Reviewer SEÇENEK (farklı sağlayıcı) |
|---|---|
| Anthropic (Claude / Claude Desktop / Cursor+Claude) | Codex (OpenAI) ✅, Gemini (Google), Mavis (MiniMax) |
| OpenAI (Codex / Cursor+GPT-4 / Copilot / ChatGPT) | Claude (Anthropic) ✅, Gemini, Mavis — **Codex YASAK (same provider)** |
| Google (Gemini Code Assist / Bard) | Claude, Codex, Mavis |
| MiniMax (Mavis) | Claude, Codex, Gemini |

**Same-provider exception**: kullanıcı explicit "Codex implementer Codex reviewer kabul" derse `Same-provider exception: user-explicit-approval` + `Exception reason: <kullanıcı beyanı + audit referansı>` field ile audit edilir; aksi halde merge YASAK.

### PR body field value canonical enum (cross-ai-audit script uyumu)

Audit script (`scripts/ci/pr-cross-ai-audit.mjs`) sadece `claude`, `codex`, `gemini`, `other` kabul eder. Semantic → field value mapping:

| Semantic provider | PR field value |
|---|---|
| Anthropic / Claude / Claude Desktop / Claude Code / Cursor+Claude | `claude` |
| OpenAI / Codex / GPT-4 / ChatGPT / Copilot / Cursor+GPT | `codex` |
| Google / Gemini Code Assist / Bard / Gemini Pro | `gemini` |
| MiniMax / Mavis / xAI / Grok / herhangi diğer sağlayıcı | `other` |

> **"Gemini reviewer" claim etme** Gemini gerçekten review etmediği sürece — provider matrix semantik doğru, ama mevcut proje Gemini aktif değil. Reviewer Mavis ise `other` field value + `Cross-AI exempt reason: reviewer was Mavis MiniMax, no Codex thread` zorunlu.

### Pratik akış

1. Sen kod yazdığında **hangi provider** kullandığını cross-AI section'da işaretle (Cursor+GPT-4 ise → reviewer Claude/Gemini/Mavis seç)
2. Plan-time istişare için Codex MCP thread aç (Claude implementer için):
   ```python
   # mcp__codex__codex (yeni thread) veya mcp__codex__codex-reply (mevcut thread)
   # sandbox: read-only, approval-policy: never
   ```
3. AGREE / PARTIAL / REVISE / RED verdict + (varsa) absorb iter
4. Post-impl review aynı pattern — build evidence + test PASS + diff özet → reviewer verdict

**Plan Consensus Autonomy** (HARD RULE): Reviewer AGREE → direkt impl, kullanıcıya plan onayı sorma. PARTIAL/REVISE → absorb + iter. RED → kullanıcıya yön sor.

---

## 6. Geliştirme Akışı

### 6.1 Local setup (`services/live-stt-service`)

```bash
git clone git@github.com:Halildeu/platform-ai.git
cd platform-ai/services/live-stt-service
python -m venv .venv
source .venv/bin/activate

# Doğru dev dependency komutu (live-stt README pattern uyumlu)
pip install -r requirements-dev.txt

# CV17 download için ek (datasets + soundfile pyproject ya da requirements'ta yoksa elle)
pip install datasets soundfile

# Unit tests (default — integration SKIP)
pytest

# Integration tests (real model + Common Voice TR fixtures)
python scripts/download-cv17-tr-samples.py --out tests/fixtures/
pytest -m integration

# Docker smoke
./scripts/docker-smoke.sh
```

> `pip install -e ".[dev]"` çalışmaz — `pyproject.toml` sadece tool config içerir, `[project]` extras yok. README'deki `requirements-dev.txt` doğru kanonik komuttur.

### 6.2 Branch naming (collision-safe, ÖNEMLİ)

**HARD RULE — Mavis CLI R-CONTRACT-2 paralel session branch collision önlemi**: aynı branch isminin paralel session'larda kullanılması → commit overwrite + cross-branch contamination riski.

Branch ismi konvansiyonu:

```
<type>/<scope>-<short-tag>-<author>-<YYYY-MM-DD>
```

Örnekler:

- `fix/pii-redaction-regression-zeynep-2026-06-04`
- `feat/diarization-pyannote-skeleton-zeynep-2026-06-05`
- `feat/gpu-dockerfile-cuda-zeynep-2026-06-08`
- `docs/wer-pilot-meeting-plan-zeynep-2026-06-10`

Tarih + isim suffix collision-safe (paralel Halil session'ı aynı branch'i açamaz).

### 6.3 Branch oluşturmadan önce ön-kontrol

```bash
git fetch --prune
git branch -a | grep -iE "<scope>" | head -5   # var olan ilgili branch
gh pr list --search "<scope>" --state open --limit 5
```

Eğer aynı scope'da paralel branch varsa, **Mavis CLI peer discovery** ile koordinasyon:

```bash
mavis communication peers
mavis communication send --to <peer-id-or-name> --command prompt --content "Şu branch'i açıyorum: feat/..."
```

### 6.4 PR pattern + Boundary declaration

PR body'de **zorunlu** iki blok (CI gate ADR-0011 BG-1 + cross-ai-audit):

#### A) Source-only Python PR örneği (en yaygın senin için)

```markdown
## Boundary declaration (ADR-0011 §2.3)

- [ ] credential-read
- [ ] credential-write
- [ ] state-mutation (test cluster)
- [ ] state-mutation (production)
- [ ] boundary-cross
- [ ] user-communication
- [x] none of the above

## Cross-AI Peer Review

Implementer AI: <claude | codex | gemini | other>
Reviewer AI: <claude | codex | gemini | other>
Codex thread: <019eXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX | N/A>
Cross-AI exempt reason: <ZORUNLU eğer Codex thread N/A — örn. "reviewer was Mavis (MiniMax), no Codex thread">
Verdict: <agree | partial | revise | red>
Verdict reason: <iter özet — örn. "iter-1 plan-time + iter-2 post-impl AGREE">
Same-provider exception: <N/A | user-explicit-approval>
Exception reason: <ZORUNLU eğer Same-provider exception=user-explicit-approval; aksi halde N/A>
```

> **Field semantics** (cross-ai-audit script `pr-cross-ai-audit.mjs` regex pattern):
> - Plain `Key: Value` format (bullet `- ` YASAK; regex `^\s*(Key)\s*:\s*` match)
> - `Cross-AI exempt reason` SADECE `Codex thread: N/A` durumunda dolu
> - `Exception reason` SADECE `Same-provider exception: user-explicit-approval` durumunda dolu
> - Aksi `N/A` yazılır (boş bırakılmaz)

#### B) GitOps / test cluster apply PR örneği

```markdown
## Boundary declaration (ADR-0011 §2.3)

- [ ] credential-read
- [ ] credential-write
- [x] state-mutation (test cluster)
- [ ] state-mutation (production)
- [ ] boundary-cross
- [ ] user-communication
- [ ] none of the above

User-approval evidence: N/A-not-allowed
```

#### C) Cross-repo contract change PR (ör. `platform-backend/audio-gateway-service` contract dokunması)

```markdown
- [x] boundary-cross
```

Plus PR body'de etkilenen repo'lar açıkça listelensin.

**Format katı**: bullet (`- `) yerine düz `Key: Value` (cross-ai-audit script regex match — `^\s*(Implementer AI|Reviewer AI|Codex thread|Verdict|...):` pattern).

### 6.5 Beklenen CI gate'ler

> **Şu an `.github/workflows/` placeholder** — Faz 24 platform-ai CI workflow setup ayrı issue scope (sıradaki sprint'lerde aktive edilecek). Aşağıdaki gate'ler **beklenen evidence + PR pre-merge check** olarak tanımlanır; aktif workflow olmadığında lokal verify + PR yorumunda kanıt sun.

Beklenen evidence (her PR'a uygulanır):

- **Unit tests** (`pytest -m 'not integration'`) — output paste'lenir veya CI run linki
- **Integration tests** (`pytest -m integration` — model + CV17 fixture)
- **Ruff** (`ruff check app/ tests/`)
- **Mypy** (`mypy app/`)
- **Black** (`black --check`)
- **Docker build smoke** (`./scripts/docker-smoke.sh`)
- **ADR-0011 BG-1 PR boundary declaration** — PR body'de exact format
- **cross-ai-audit** — PR body Cross-AI section format (Implementer/Reviewer/Codex thread/Verdict)
- **gitleaks** — test fixture'larda `key`/`secret`/`token`/`api` değişken adlarından kaçın (generic-api-key regex false-positive; gerçek pattern: low-entropy fixture string + variable rename)

**HARD RULE — CI Kırmızıyken Merge YASAK**: yeşil olmadan squash merge YASAK.
**HARD RULE — Admin Merge YASAK**: `--admin` flag YASAK (kritik prod outage istisnası dışında).

### 6.6 Normal merge

```bash
gh pr merge <num> --squash --delete-branch
```

Cross-AI AGREE-merged sonra audit trail otomatik.

---

## 7. Mavis CLI ile İletişim (önemli)

**HARD RULE — Lokal Agent İletişimi: Mavis CLI**: lokal agent'lar (Claude session'lar, biz iki kişi) arası ve kullanıcı ile iletişim için **Mavis CLI** standart kanal.

```bash
# Peer discovery
mavis communication peers

# Mesaj gönder (Session ID veya Agent name)
mavis communication send --to <peer-id-or-name> --command prompt --content "X scope'unu başlattım"

# Yardım
mavis --help
```

### Faz 24 + Meeting Intelligence için ek YASAK payload (genel guard üstüne)

`--content` içine YASAK (shell history + process list + Mavis log/queue + karşı peer transcript'ine düşebilir):

**Genel (her zaman YASAK)**:
- secret / JWT / refresh token / raw bearer / webhook URL / cookie / OAuth client secret / private key / signing key / HMAC secret / admin credential / PII (email, telefon, UPN)

**Faz 24 / Meeting Intelligence özel (KVKK + ADR-0030 ek)**:
- raw audio payload veya base64 audio
- transcript text (partial veya final — KVKK Madde 6/9 hassas)
- sample file path containing user/customer identity (örn. `recordings/<user-email>/<meeting-id>.wav`)
- tenant ID / company ID / user ID / device ID / meeting ID (numeric veya UUID — audit metadata KISITLI)
- internal VPN/mTLS material (WireGuard config, Vault PKI cert/key payload)
- signed URLs (S3/MinIO presigned, içerik path'i sızdırır)
- raw log/output snippet containing transcript text or object storage keys (PII transitif sızıntı)

Gerekirse sadece **redacted özet + evidence path/issue/PR linki** gönderilir.

---

## 8. HARD RULE'lar — Kısa Liste (tam liste için Global CLAUDE.md)

### Cross-cutting (her proje, her repo)

| HARD RULE | Özet |
|---|---|
| **Cross-AI Peer Review** | Code yazan provider ≠ review eden provider (sağlayıcı seviyesinde) |
| **Plan Consensus Autonomy** | Reviewer AGREE → direkt impl; plan onayı sorma |
| **No Fake Work** | Test koşmadan PASS rapor YASAK; build evidence + test PASS kanıt zorunlu |
| **No Closure Language** | "Bitti / kapandı / gün sonu" YASAK; sıradaki adım zorunlu |
| **Yarın YASAK / Şimdi yap** | "Yarın bakarım / sonra fix ederim" YASAK; şimdi yap |
| **CI Kırmızıyken Merge YASAK** | Yeşil olmadan squash YASAK |
| **Admin Merge YASAK** | `--admin` flag YASAK (kritik prod outage istisnası dışında) |
| **Mavis CLI** | Multi-session koordinasyon için standart kanal |
| **Her İş Project Board'a Eklenir** | Yapılacak her iş önce GitHub Project board'a issue olarak girer + tam field set + Status In Progress → Done |
| **Cevap Dili Türkçe** | Kullanıcıya yönelen serbest metin Türkçe; kod-paylaşılan teknik artifact İngilizce |
| **Uzun Vadeli Kalıcı Çözüm Tercih Edilir** | Geçici patch yerine 6-ay-sonra-doğru kalıcı tasarım |

### platform-ai repo-specific (AGENTS.md + ADR-0030/0031 absorb)

| HARD RULE | Özet |
|---|---|
| **No direct client-to-STT (Gateway only)** | Mobile/desktop/web hiçbir zaman `platform-ai`'a doğrudan bağlanmaz; tüm istek `audio-gateway-service` üzerinden geçer (ADR-0031 §D1) |
| **PII/KVKK no raw audio/transcript logging** | Structured log redaction filter zorunlu; `kvkk_pii_redaction_total` metric < threshold; audit event metadata-only (text/path payload'a YASAK) |
| **STT model/version/hash pin** | Whisper model adı + version + content hash environment variable (`STT_MODEL_NAME`, `STT_MODEL_VERSION`, `STT_MODEL_SHA256`) ile pin'lenir; runtime drift YASAK |
| **GPU CPU-first discipline** | PoC CPU-only önce → WER + latency + Gate B ölçüm → Codex consensus → GPU karar; donanım yatırımı data-driven |
| **Workcube ekosistem reuse** | Standalone publish YOK; auth/notification/audit/permission reuse zorunlu; cross-repo contract drift yasak |

**Tam liste**: `~/.claude/CLAUDE.md` (kullanıcının lokal global rule'ları — Claude Code session açtığında otomatik yüklenir).

---

## 9. İlk Hafta Yol Haritası (Önerilen — Codex `019e8d58` iter-1 absorb)

### Gün 1: Setup + Repo Okuma

- [ ] `git clone git@github.com:Halildeu/platform-ai.git`
- [ ] Python 3.11 + venv + `services/live-stt-service` setup (`requirements-dev.txt` + `datasets soundfile`)
- [ ] [Faz 24 canonical plan](https://github.com/Halildeu/platform-k8s-gitops/blob/main/docs/faz-24-meeting-intelligence-plan.md) tam oku
- [ ] [ADR-0030](https://github.com/Halildeu/platform-k8s-gitops/blob/main/docs/adr/0030-kvkk-meeting-intelligence-boundary.md) + [ADR-0031](https://github.com/Halildeu/platform-k8s-gitops/blob/main/docs/adr/0031-two-server-meeting-intelligence-topology.md) oku
- [ ] [Audio Gateway Contract v1.0](https://github.com/Halildeu/platform-backend/blob/main/audio-gateway-service/docs/contract-v1.md) oku
- [ ] `AGENTS.md` + `CLAUDE.md` (platform-ai repo) oku — repo-specific kurallar
- [ ] `services/live-stt-service` README + main.py + transcribe.py + metrics.py oku
- [ ] `pytest` (unit) çalıştır — 22+ test PASS olmalı
- [ ] `pytest -m integration` (fixture download sonrası) çalıştır

### Gün 2-3: Issue #97 (PII Redaction Fix — Hızlı Win)

- [ ] [Issue #97](https://github.com/Halildeu/platform-ai/issues/97) ve PR #74 oku
- [ ] 4 fail test'i incele (TC kimlik / IBAN / phone regex edge case)
- [ ] Regex pattern fix (TC kimlik 11 digit checksum + IBAN TR format + phone +90 prefix)
- [ ] Yeni branch: `fix/pii-redaction-regression-zeynep-2026-06-04`
- [ ] Cross-AI plan-time istişare (sen Cursor+GPT-4 kullanıyorsan reviewer Claude/Gemini; Claude kullanıyorsan reviewer Codex) → AGREE → impl
- [ ] PR + Boundary declaration (Section 6.4 A — `none of the above`) + Cross-AI section
- [ ] Local pytest + ruff + mypy → tüm green
- [ ] CI varsa yeşil; yoksa lokal evidence (test output + ruff/mypy/black + docker-smoke) + PR comment + reviewer AGREE → squash merge (HARD RULE: CI varsa kırmızıyken merge YASAK)

### Gün 4-5: Issue #48 Diarization Skeleton Only

- [ ] `services/diarization-service` skeleton (pyannote.audio + FastAPI; mock dispatch)
- [ ] Cross-AI iter chain (plan-time + post-impl)
- [ ] Codex `019e879c` mutabakat noktalarına uyum (compute worker boundary, no direct client)
- [ ] PR + Boundary declaration + Cross-AI section
- [ ] LIVE STT pattern reuse (FastAPI lifespan + CorrelationIdMiddleware + Prometheus metrics + KVKK redaction)

> #49 meeting-AI bu hafta YOK — LLM/KVKK/secret rotation boundary nedeniyle dikkatli sırala; ADR-0030 Option A/B karar pilot öncesi gerek. Hafta 2-3'te ele al.

### Hafta 2: #39 State Machine Scaffolding (Model Seçimi YOK)

- [ ] [Issue #39](https://github.com/Halildeu/platform-ai/issues/39) re-scope: "Segment merge + revize state machine **scaffolding only** — model seçimi PR-wer-01 sonrası kilitlenir"
- [ ] `services/final-stt-service` mevcut skeleton üzerine state machine (STARTED → STREAMING → FINISHING → FINISHED)
- [ ] WER ölçüm öncesi model seçimi YASAK (`large-v3-turbo` veya başka varsayım YOK)
- [ ] Cross-AI iter chain → AGREE → impl

### Hafta 3+: GPU Stack + Performance + Hardware ADR

- [ ] #41 Dockerfile.gpu (CUDA + cuDNN + faster-whisper CUDA build) — CPU PoC parite testi
- [ ] #42 Multi-worker GPU stream parallelism
- [ ] #43 Performance ölçüm matrix (WER + latency + memory + cost)
- [ ] #40 Hardware ADR draft — ADR-0031 dedicated host + k3s ai-test/ai-prod truth'una göre revize ("k3d node-pool" stale terim, "RTX 4070 platform-ai host vs cloud GPU vs sectoral A10 self-host" yeni karar matrisi)
- [ ] #49 meeting-AI skeleton — LLM provider seçimi + KVKK Option A/B + secret rotation

---

## 10. İletişim + Soru

**Bana ulaşmak için** (Claude session açan agent — Halil'in ortağı):

- GitHub issue/PR yorumu (`@Halildeu` mention)
- Mavis CLI (yukarıda Section 7)
- Doğrudan Halil ile mesajla → Halil → bana iletim

**Soru / clarification gerek** (HARD RULE Plan Consensus Autonomy gereği):

- Stratejik karar (mimari, FAZ pivot, scope reset) → Halil'e sor
- Implementasyon detay (algoritma seçimi, library pattern) → Codex MCP istişare
- HARD RULE belirsizliği → bana sor veya Global CLAUDE.md oku

---

## 11. Referans Linkler

| Resource | URL |
|---|---|
| **Project #2 platform Roadmap** (umbrella) | https://github.com/users/Halildeu/projects/2 |
| **Project #4 Faz 24 platform-ai** (Zeynep board) | https://github.com/users/Halildeu/projects/4 |
| `platform-ai` repo | https://github.com/Halildeu/platform-ai |
| `platform-backend` repo | https://github.com/Halildeu/platform-backend |
| `platform-k8s-gitops` repo | https://github.com/Halildeu/platform-k8s-gitops |
| Faz 24 canonical plan | https://github.com/Halildeu/platform-k8s-gitops/blob/main/docs/faz-24-meeting-intelligence-plan.md |
| ADR-0030 KVKK | https://github.com/Halildeu/platform-k8s-gitops/blob/main/docs/adr/0030-kvkk-meeting-intelligence-boundary.md |
| ADR-0031 Two-Server | https://github.com/Halildeu/platform-k8s-gitops/blob/main/docs/adr/0031-two-server-meeting-intelligence-topology.md |
| Audio Gateway Contract v1.0 | https://github.com/Halildeu/platform-backend/blob/main/audio-gateway-service/docs/contract-v1.md |
| live-stt-service MERGED PR | https://github.com/Halildeu/platform-ai/pull/1 |
| PR-stt-02a M2 anchor | https://github.com/Halildeu/platform-ai/pull/96 |
| **Lokal global HARD RULE'lar** | `~/.claude/CLAUDE.md` (kullanıcının lokal kopyası; Claude session başlangıcında otomatik yüklenir) |
| **platform-ai repo-specific** | https://github.com/Halildeu/platform-ai/blob/main/CLAUDE.md (repo'ya özel tamamlayıcı kurallar) |
| **platform-ai AGENTS.md** | https://github.com/Halildeu/platform-ai/blob/main/AGENTS.md (agent giriş yüzeyi) |

---

## 12. Sonuç

Hoşgeldin Zeynep! Bu repo + Faz 24 platformu ortak çabamızdır. **Cross-AI peer review** + **kalıcı çözüm tercihi** + **canlı evidence** (test PASS + CI green) disiplinimiz; PR-gw-01A audio-gateway-service normalize'ı MERGED — evidence: 28/28 unit/contract test + 13/13 CI green check; Codex `019e8c26` 4-iter consensus (iter-1 REVISE → iter-2 AGREE for A → iter-3 REVISE → iter-4 AGREE final).

Python STT/AI için ilk acceptance: **`curl /transcribe` + `docker-smoke.sh` + `pytest -m integration` smoke**. Browser smoke M6 client fazında (mobile/desktop UI ile birlikte) gelir; Python compute plane için browser kanıtı şu an scope dışı.

Sıradaki Python STT/AI işlerini sen sürdüreceksin. Sorun olursa Halil'e ya da bana (Claude session) yaz. Soru-tartışma için Mavis CLI veya GitHub issue/PR yorum. HARD RULE'lar bağlayıcıdır + sürpriz değildir; net + dokümante + adversarial review ile test edilir.

Başarılar! 🚀

— Claude session `ac816415` (Halil'in geliştirici ortağı), 2026-06-03
