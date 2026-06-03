# Onboarding — Zeynep Serban (`@zeynep-serban`)

> **Hoşgeldin Zeynep!** Bu doküman `platform-ai` repo'sundaki Python STT + diarization + meeting-AI scope'u için onboarding rehberidir. Faz 24 Meeting Intelligence platformunun ortak geliştiricilerinden birisin; aşağıda repo yapısı, atanan iş yükün, geliştirme akışı, cross-AI peer review pattern'i ve ilk hafta yol haritası yer alıyor.

---

## 1. Sen ve Scope'un

`platform-ai` repo'su **Faz 24 Meeting Intelligence** platformunun **Python compute plane**'idir. Spring Boot orchestration (`platform-backend/audio-gateway-service`), staging-sw'da çalışırken `platform-ai` **ayrı dedicated host'ta** (ADR-0031 two-server topology) konumlanır.

**Sen sahipsin**:

- `services/live-stt-service` (FastAPI + faster-whisper Whisper medium int8)
- `services/diarization-service` (ileri faz — pyannote.audio)
- `services/meeting-ai-service` (ileri faz — LLM özet/karar/aksiyon çıkarımı)
- WER raporu (Common Voice TR + pilot meeting triangulate)
- GPU stratejisi (Dockerfile.gpu + RTX 4070 vs cloud GPU vs k3d node-pool kararı)
- PII redaction regression iyileştirme (Issue #97)

**Sen sahibi DEĞİLSİN** (paralel takım):

- `platform-backend/audio-gateway-service` (Spring WebFlux Gateway — Claude/Halil)
- `platform-mobile` / `platform-desktop` (RN + Electron — M6 client team)
- `platform-web/apps/mfe-meeting` (React MFE — M6 web team)
- `platform-k8s-gitops` (Kustomize + ArgoCD — Halil)

---

## 2. Atanan Iş Yükün (GitHub Project #4 board)

[**Project #4 — platform-ai Faz 24 Meeting Intelligence**](https://github.com/users/Halildeu/projects/4) (public, board canonical)

Sana atanan açık 8 issue (`assignee: @zeynep-serban`):

| # | Title | Milestone |
|---|---|---|
| [#39](https://github.com/Halildeu/platform-ai/issues/39) | `[PR-final-stt-01]` Segment merge + revize state machine | M4 Accuracy |
| [#40](https://github.com/Halildeu/platform-ai/issues/40) | `[Donanım kararı]` RTX 4070 host upgrade vs cloud GPU vs k3d-prod node-pool | M5 Performance |
| [#41](https://github.com/Halildeu/platform-ai/issues/41) | `[PR-gpu-01]` Dockerfile.gpu (NVIDIA CUDA + cuDNN + faster-whisper CUDA) | M5 Performance |
| [#42](https://github.com/Halildeu/platform-ai/issues/42) | `[PR-gpu-01]` Multi-worker GPU stream parallelism | M5 Performance |
| [#43](https://github.com/Halildeu/platform-ai/issues/43) | `[Performance ölçüm]` post-GPU WER + latency + memory + cost matrix güncelle | M5 Performance |
| [#48](https://github.com/Halildeu/platform-ai/issues/48) | `[platform-ai]` diarization-service skeleton (pyannote.audio) | M6 Integration |
| [#49](https://github.com/Halildeu/platform-ai/issues/49) | `[platform-ai]` meeting-ai-service skeleton (LLM özet/karar/aksiyon) | M6 Integration |
| [#97](https://github.com/Halildeu/platform-ai/issues/97) | `[fix-pii]` PR #74 PII redaction patterns 4 test fail — regression | M3 Resilience + Observability |

**Önerilen başlangıç sırası**: #97 (hızlı fix + güvenle CI/test akışını öğren) → #39 → #48 → #41 + #42 (GPU stack) → #40 + #43 (data-driven hardware karar) → #49.

**Sıraladığım iş sıralaması**, üstelik sen kendin sıralayabilirsin. Mavis/Codex/Claude ile cross-AI peer review pattern ile her PR'ı geliştiririz.

---

## 3. Repo Yapısı

```
platform-ai/
├── .github/                # workflows + issue templates + dependabot
├── AGENTS.md               # Agent-genel rehber (HARD RULE'lar + workflow + cross-AI)
├── CLAUDE.md               # Claude Code session rehberi (HARD RULE'lar repo-specific)
├── README.md               # Üst seviye proje özeti
├── docs/
│   ├── README.md           # Doc index
│   ├── adr/                # Architecture Decision Records (boş başlangıç; platform-k8s-gitops ana ADR'ları taşır)
│   ├── runbooks/           # Operasyonel runbook'lar
│   └── onboarding/         # Bu doc burada
└── services/
    ├── live-stt-service/   # MERGED — faster-whisper PoC (PR #1 4088d9a + PR-stt-02a #96)
    ├── diarization-service/   # ⏳ skeleton açılacak (Issue #48)
    └── meeting-ai-service/    # ⏳ skeleton açılacak (Issue #49)
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

**Pratik akış**:

1. Sen kod yazarsan (örn. Cursor + GPT-4 / Claude desktop / Copilot kullansaydın), **review için farklı sağlayıcı kullan** (örn. Codex MCP veya Claude session)
2. Plan-time istişare için Codex MCP thread aç:
   ```python
   # Claude session veya Codex thread plan-time consensus
   # mcp__codex__codex (yeni thread) veya mcp__codex__codex-reply (mevcut thread)
   # sandbox: read-only, approval-policy: never
   ```
3. AGREE / PARTIAL / REVISE / RED verdict + (varsa) absorb iter
4. Post-impl review aynı pattern — build evidence + test PASS + diff özet → Codex verdict

**Plan Consensus Autonomy** (HARD RULE): Codex AGREE → direkt impl, kullanıcıya plan onayı sorma. PARTIAL/REVISE → absorb + iter. RED → kullanıcıya yön sor.

**HARD RULE — Cross-AI Peer Review provider seviyesinde**:
[Global CLAUDE.md](https://github.com/Halildeu/platform-k8s-gitops/blob/main/CLAUDE.md) ve `~/.claude/CLAUDE.md` referans — aynı sağlayıcının farklı session/subagent'i de YASAK.

---

## 6. Geliştirme Akışı

### 6.1 Local setup (`services/live-stt-service`)

```bash
git clone git@github.com:Halildeu/platform-ai.git
cd platform-ai/services/live-stt-service
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pip install prometheus_client soundfile

# Unit tests (default — integration SKIP)
pytest

# Integration tests (real model + Common Voice TR fixtures)
python scripts/download-cv17-tr-samples.py --out tests/fixtures/
pytest -m integration

# Docker smoke
./scripts/docker-smoke.sh
```

### 6.2 Branch naming (collision-safe, ÖNEMLİ)

**HARD RULE — Mavis CLI R-CONTRACT-2 paralel session branch collision önlemi**: aynı branch isminin paralel session'larda kullanılması → commit overwrite + cross-branch contamination riski (bizim `audio-gateway-service` PR #378/#379/#380 vakası gibi).

Branch ismi konvansiyonu:

```
<type>/<scope>-<short-tag>-<author>-<YYYY-MM-DD>
```

Örnekler:

- `feat/live-stt-pii-redaction-fix-zeynep-2026-06-04`
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

```markdown
## Boundary declaration (ADR-0011 §2.3)

- [ ] credential-read
- [ ] credential-write
- [x] state-mutation (test cluster)
- [ ] state-mutation (production)
- [ ] boundary-cross
- [ ] user-communication
- [ ] none of the above

## Cross-AI Peer Review

Implementer AI: claude
Reviewer AI: codex
Codex thread: 019eXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX
Verdict: agree
Verdict reason: iter-X cevabı...
```

**Format katı**: bullet (`- `) yerine düz `Key: Value` (cross-ai-audit script regex match).

### 6.5 CI gate'ler (yeşil olmadan merge YASAK)

`platform-ai` CI workflow'ları:

- Unit tests (`pytest`) — default `-m 'not integration'`
- Integration tests (`pytest -m integration` — manual / nightly)
- Ruff + mypy + black
- Docker build smoke
- ADR-0011 BG-1 PR boundary declaration check
- cross-ai-audit (V2.1-GOV-1)
- gitleaks (test fixture'larda "key/secret/token/api" değişken adlarından kaçın — generic-api-key regex false-positive)

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

**Yasak**: `--content` içine secret/JWT/refresh token/raw bearer/webhook URL/cookie/OAuth client secret/private key/signing key/HMAC secret/admin credential/PII koymak (shell history + process list + Mavis log/queue düşer). Gerekirse sadece redacted özet + evidence path/issue/PR linki.

---

## 8. HARD RULE'lar — Kısa Liste (tam liste için Global CLAUDE.md)

| HARD RULE | Özet |
|---|---|
| **Cross-AI Peer Review** | Code yazan provider ≠ review eden provider (sağlayıcı seviyesinde) |
| **Plan Consensus Autonomy** | Codex AGREE → direkt impl; plan onayı sorma |
| **No Fake Work** | Test koşmadan PASS rapor YASAK; build evidence + test PASS kanıt zorunlu |
| **No Closure Language** | "Bitti / kapandı / gün sonu" YASAK; sıradaki adım zorunlu |
| **Yarın YASAK / Şimdi yap** | "Yarın bakarım / sonra fix ederim" YASAK; şimdi yap |
| **CI Kırmızıyken Merge YASAK** | Yeşil olmadan squash YASAK |
| **Admin Merge YASAK** | `--admin` flag YASAK (kritik prod outage istisnası dışında) |
| **Mavis CLI** | Multi-session koordinasyon için standart kanal |
| **Her İş Project Board'a Eklenir** | Yapılacak her iş önce GitHub Project board'a issue olarak girer + tam field set + Status In Progress → Done |
| **Cevap Dili Türkçe** | Kullanıcıya yönelen serbest metin Türkçe; kod-paylaşılan teknik artifact İngilizce |
| **Uzun Vadeli Kalıcı Çözüm Tercih Edilir** | Geçici patch yerine 6-ay-sonra-doğru kalıcı tasarım |

**Tam liste**: `~/.claude/CLAUDE.md` (kullanıcının global rule'ları; Claude Code session açtığında otomatik yüklenir).

---

## 9. İlk Hafta Yol Haritası (Önerilen)

### Gün 1: Setup + Repo Okuma

- [ ] `git clone git@github.com:Halildeu/platform-ai.git`
- [ ] Python 3.11 + venv + `services/live-stt-service` setup
- [ ] [Faz 24 canonical plan](https://github.com/Halildeu/platform-k8s-gitops/blob/main/docs/faz-24-meeting-intelligence-plan.md) tam oku
- [ ] [ADR-0030](https://github.com/Halildeu/platform-k8s-gitops/blob/main/docs/adr/0030-kvkk-meeting-intelligence-boundary.md) + [ADR-0031](https://github.com/Halildeu/platform-k8s-gitops/blob/main/docs/adr/0031-two-server-meeting-intelligence-topology.md) oku
- [ ] [Audio Gateway Contract v1.0](https://github.com/Halildeu/platform-backend/blob/main/audio-gateway-service/docs/contract-v1.md) oku
- [ ] `services/live-stt-service` README + main.py + transcribe.py + metrics.py oku
- [ ] `pytest` (unit) çalıştır — 22+ test PASS olmalı
- [ ] `pytest -m integration` (fixture download sonrası) çalıştır

### Gün 2-3: Issue #97 (PII Redaction Fix — Hızlı Win)

- [ ] [Issue #97](https://github.com/Halildeu/platform-ai/issues/97) ve PR #74 oku
- [ ] 4 fail test'i incele (TC kimlik / IBAN / phone regex edge case)
- [ ] Regex pattern fix (TC kimlik 11 digit checksum + IBAN TR format + phone +90 prefix)
- [ ] Yeni branch: `fix/pii-redaction-regression-zeynep-2026-06-04`
- [ ] Codex MCP plan-time istişare → AGREE → impl
- [ ] PR + Boundary declaration + Cross-AI section
- [ ] CI yeşil + Codex AGREE → squash merge

### Gün 4-5: Issue #48 + #49 (Diarization + Meeting AI Skeleton)

- [ ] `services/diarization-service` skeleton (pyannote.audio + FastAPI)
- [ ] `services/meeting-ai-service` skeleton (LLM client + summarization endpoint)
- [ ] Codex iter chain (plan-time + post-impl)
- [ ] PR'lar ayrı (slice boundary: diarization + meeting-ai farklı PR)

### Hafta 2: WER + GPU (Issue #39 / #41 / #42 / #43)

- [ ] Segment merge + revize state machine (final-stt) — Issue #39
- [ ] Dockerfile.gpu (NVIDIA CUDA + cuDNN + faster-whisper CUDA) — Issue #41
- [ ] Multi-worker GPU stream parallelism — Issue #42
- [ ] Performance matrix (WER + latency + memory + cost) — Issue #43
- [ ] Donanım kararı ADR draft (RTX 4070 vs cloud GPU vs k3d-prod node-pool) — Issue #40

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
| **Project #4 board** | https://github.com/users/Halildeu/projects/4 |
| `platform-ai` repo | https://github.com/Halildeu/platform-ai |
| `platform-backend` repo | https://github.com/Halildeu/platform-backend |
| `platform-k8s-gitops` repo | https://github.com/Halildeu/platform-k8s-gitops |
| Faz 24 canonical plan | https://github.com/Halildeu/platform-k8s-gitops/blob/main/docs/faz-24-meeting-intelligence-plan.md |
| ADR-0030 KVKK | https://github.com/Halildeu/platform-k8s-gitops/blob/main/docs/adr/0030-kvkk-meeting-intelligence-boundary.md |
| ADR-0031 Two-Server | https://github.com/Halildeu/platform-k8s-gitops/blob/main/docs/adr/0031-two-server-meeting-intelligence-topology.md |
| Audio Gateway Contract v1.0 | https://github.com/Halildeu/platform-backend/blob/main/audio-gateway-service/docs/contract-v1.md |
| live-stt-service MERGED PR | https://github.com/Halildeu/platform-ai/pull/1 |
| PR-stt-02a M2 anchor | https://github.com/Halildeu/platform-ai/pull/96 |
| Global CLAUDE.md (HARD RULE'lar) | `~/.claude/CLAUDE.md` (lokal — kopya https://github.com/Halildeu/platform-ai/blob/main/CLAUDE.md) |

---

## 12. Sonuç

Hoşgeldin Zeynep! Bu repo + Faz 24 platformu ortak çabamızdır. **Cross-AI peer review** + **kalıcı çözüm tercihi** + **canlı evidence (test PASS + CI green + browser smoke)** disiplinimiz; PR-gw-01A audio-gateway normalize'ı dün tamamladık (Codex 4-iter AGREE final, 28/28 test PASS, 13/13 CI green). Sıradaki Python STT/AI işlerini sen sürdüreceksin.

Sorun olursa Halil'e ya da bana (Claude session) yaz. Soru-tartışma için Mavis CLI veya GitHub issue/PR yorum. HARD RULE'lar bağlayıcıdır + sürpriz değildir; net + dokümante + adversarial review ile test edilir.

Başarılar! 🚀

— Claude session `ac816415` (Halil'in geliştirici ortağı), 2026-06-03
