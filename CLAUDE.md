# CLAUDE.md — platform-ai Agent Kılavuzu

> Bu dosya Claude Code / agent session'larında otomatik yüklenir. Repo-specific kurallar, pattern'ler ve bağlam.

> Öncelik notu: Repo-geneli giriş yüzeyi [AGENTS.md](./AGENTS.md). Global HARD RULE seti `~/.claude/CLAUDE.md` (her oturumda otomatik yüklenir). Çelişki halinde global HARD RULE > AGENTS.md > bu dosya.

---

## Proje Bağlamı

`platform-ai` Workcube ekosisteminde **Faz 24 Meeting Intelligence** kapsamında Python tabanlı STT (Speech-to-Text), diarization (konuşmacı ayrımı) ve meeting AI (özet/karar/aksiyon çıkarımı) mikroservislerini barındırır.

Repo eşleştirmesi: [README.md](./README.md) "Repo Konumu" tablosu.

## Ekosistem Reuse

Bu repo **standalone değil** — Workcube altyapısının doğal uzantısı:

- **Auth**: Keycloak SSO (`auth-service` realm) — JWT validation FastAPI middleware
- **Routing**: api-gateway (Spring Cloud Gateway) — `/api/v1/ai/*` route
- **Secret**: Vault AppRole → ESO ExternalSecret → K8s Secret → FastAPI env
- **DB**: PostgreSQL host-compose (`platform-pg-{prod,test}`) — meeting/transcript schema platform-backend tarafında
- **Storage**: MinIO (planlı) — ses dosyası S3-compatible
- **Queue**: Redis — chunk sıralama + Whisper queue
- **Monitoring**: kube-prom-stack — Prometheus metrics + Grafana dashboard
- **Notification**: Faz 23 notification-service — meeting özet/aksiyon push
- **Authz**: permission-service + OpenFGA — meeting Zanzibar tuple
- **CI/CD**: GitHub Actions + GHCR + Cross-AI Codex review

## Ana Kurallar (HARD RULE — global ⊕ repo)

### Global HARD RULE (otomatik yüklenir)

`~/.claude/CLAUDE.md` içindeki tüm HARD RULE seti aynen geçerli:

- Mavis CLI (lokal agent iletişimi)
- Tam Otonom Önerme + Yürütme
- Workspace Tooling: Microsoft Teams
- Uzun Vadeli Kalıcı Çözüm
- CI Kırmızıyken Merge YASAK
- Tarayıcıdan Sonuç Doğrulanmadan İş Bitmedi
- "Yarın" / İş Erteleme YASAK
- Deploy Sonrası Tarayıcı Console Verifikasyonu
- Admin Merge YASAK
- Cross-AI Peer Review (provider seviyesinde)
- Continuous Autonomous Mode + Codex Decision Authority
- No Fake Work / No Cosmetic Operations
- Pre-Production Full Authority
- Cevap Dili Türkçe
- Plan Consensus Autonomy

### Repo-specific (platform-ai)

1. **Python servis pattern**: FastAPI + Pydantic v2 + structlog + Prometheus metrics. Her servis ayrı `services/<svc>/` dizini, ayrı Dockerfile, ayrı GHCR image.

2. **STT model versioning**: Whisper model adı + version (örn. `large-v3-turbo-int8`) + hash environment variable ile pin'lenir. Drift sıfır tolerans.

3. **PII/KVKK boundary**: Ses dosyası + transcript hassas veri:
   - Storage encryption (MinIO SSE-S3 + Vault KMS)
   - Retention policy ADR ile sabit
   - Audit log her erişim için
   - User consent flow zorunlu (meeting başlatırken)
   - Voiceprint olmaz ilk fazda

4. **GPU compute disiplini**: PoC CPU-only başla → metrik ölç → GPU karar. Erken GPU yatırımı YASAK. Faz 24.2 PoC sonucu Codex consensus ile karar verilir.

5. **WebSocket discipline**: Mobile audio chunk WebSocket üzerinden gelir. Heartbeat + retry + local buffer client tarafında zorunlu. Ses kaybı = ürün kalitesi düşer.

6. **Cross-AI Codex review Python için**: Test runner = pytest, lint = ruff + mypy, format = black. Codex review thread her PR için zorunlu (provider OpenAI veya Gemini — Anthropic yazıyorsa Anthropic değil).

7. **No standalone publish**: Bu repo Workcube ekosistemine entegre — bağımsız ürün/lansman pattern'i yok. Yayın disiplini ana platform release pipeline'a bağlı.

## Pattern'ler

### FastAPI Servis İskeleti

```
services/<svc>/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI app
│   ├── api/                 # router'lar
│   ├── core/                # config (Pydantic Settings), security (JWT)
│   ├── services/            # iş mantığı (STT, diarization, ai)
│   ├── models/              # Pydantic schemas
│   └── observability/       # Prometheus + structlog
├── tests/
│   ├── unit/
│   └── integration/
├── Dockerfile               # multi-stage (build + runtime)
├── requirements.txt
├── requirements-dev.txt
└── pyproject.toml           # ruff/mypy/black config
```

### Commit Message

```
<type>(<scope>): <kısa başlık>

<body — neden, ne, kanıt>

<Codex iter referansı varsa>
<Co-Authored-By: Claude ...>
```

Types: `feat` / `fix` / `refactor` / `docs` / `chore` / `test` / `perf`

### CI Gates (planlı)

- `pytest --cov` (>80% coverage)
- `ruff check` (lint)
- `mypy --strict` (type)
- `black --check` (format)
- `bandit` (security)
- Docker build sanity
- Cross-AI Codex review thread referansı

## Codex Adversarial Protokol

Her büyük delta sonrası Codex MCP adversarial review (plan-time istişare):
- VERDICT: AGREE / PARTIAL / REVISE / RED
- AGREE → direkt impl, plan onayı sorma (Plan Consensus Autonomy)
- PARTIAL → absorb et, yeni iter submit et
- REVISE → absorb + karşı-tez + iter devam
- RED → kullanıcıya rapor + yön sor

Codex thread referansı PR squash mesajında zorunlu.

## Agent Session Akış

1. Oku: [AGENTS.md](./AGENTS.md) → [README.md](./README.md)
2. Bağlantılı repo state:
   - platform-backend → meeting/transcript-service durumu
   - platform-k8s-gitops → Faz 24 manifest durumu
   - platform-mobile → mobile MVP durumu
3. Kontrol: `git log --oneline main..HEAD | head -10` + `git status`
4. Memory: `~/.claude/projects/<slug>/memory/MEMORY.md`
5. Codex thread referansları (PLAN.md veya handoff doc'lardan)
6. İş seçimi + board: platform Roadmap board Project #2 (Faz 24 issue'ları)

## Kaynaklar

- [README.md](./README.md) — proje genel + repo eşleştirmesi
- [AGENTS.md](./AGENTS.md) — repo giriş yüzeyi + HARD RULE özet
- Global `~/.claude/CLAUDE.md` — tüm projeler için HARD RULE seti
- platform-backend Faz 24 → meeting-service Spring Boot iskeleti
- platform-k8s-gitops `docs/adr/0030-*` (planlı) — meeting platform ADR
