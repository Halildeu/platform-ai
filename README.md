# platform-ai

Meeting Intelligence / Speech-to-Text mikroservisleri — Workcube ERP ekosistemine entegre Python servisleri.

## Amaç

Telefon, masaüstü ve ileride Teams/Zoom kaynaklarından gelen ses akışını işleyerek:

- **Canlı geçici transkript** (2-8 sn gecikme)
- **Kesinleşmiş transkript** (10-20 sn bağlamlı)
- **Toplantı sonu nihai işleme** (özet, kararlar, aksiyonlar)
- **Konuşmacı ayrımı** (diarization)
- **Meeting AI** (özet/karar/aksiyon LLM çıkarımı)

üretir. Faz 24 Meeting Intelligence kapsamında konumlanır.

## Repo Konumu (Workcube ekosistem haritası)

| Repo | Rol |
|---|---|
| **platform-ai** (bu) | Python servisleri — STT, diarization, meeting-ai, FastAPI |
| [platform-backend](https://github.com/Halildeu/platform-backend) | Spring Boot — `meeting-service` + `transcript-service` + `audio-gateway-service` (planlı) |
| [platform-web](https://github.com/Halildeu/platform-web) | React + Single-SPA — `mfe-meeting` MFE (planlı) |
| [platform-mobile](https://github.com/Halildeu/platform-mobile) | React Native + Expo — mobile audio capture client (planlı) |
| [platform-k8s-gitops](https://github.com/Halildeu/platform-k8s-gitops) | GitOps desired-state — kustomize overlays + ArgoCD manifestleri |

## Servisler (planlı)

| Servis | Görev | Stack |
|---|---|---|
| `live-stt-service` | 2-3 sn chunk → geçici transkript | FastAPI + faster-whisper (medium int8) |
| `final-stt-service` | 10-15 sn bağlamlı kesin transkript | FastAPI + faster-whisper (large-v3-turbo) |
| `diarization-service` | Konuşmacı ayrımı (Speaker 1/2/3...) | FastAPI + pyannote.audio |
| `meeting-ai-service` | Özet + karar + aksiyon LLM çıkarımı | FastAPI + Anthropic/OpenAI API (ilk faz), Ollama (ileri faz) |

## Reuse — Workcube Ekosisteminden

- **Keycloak SSO** → JWT validation (`auth-service` realm reuse)
- **api-gateway** → routing + JWT propagation
- **Vault + ESO** → API key + model storage + S3 credential
- **PostgreSQL** (`host-compose`) → meeting/transcript şema (V26+ migration platform-backend tarafında)
- **Faz 23 notification-service** → meeting özet/aksiyon Teams + email + in-app push
- **permission-service + OpenFGA** → meeting Zanzibar authz (owner/participant/viewer)
- **kube-prom-stack** → STT latency + chunk throughput + queue depth metrik
- **ArgoCD + Kustomize** → GitOps deploy (overlay test/prod)
- **GitHub Actions + GHCR + Cross-AI Codex review** → CI/CD + adversarial review disiplini

## Yeni Eklemeler

- **MinIO** (S3-compatible) → ses dosyası storage (host-compose)
- **Redis** → chunk sıralama + Whisper queue + cache
- **GPU node** (planlı) → faster-whisper large-v3-turbo + pyannote (NVIDIA RTX 4060/4070 12 GB VRAM)
- **WebSocket / WebRTC edge** → mobile audio streaming

## Mimari Akış

```
[Mobile / Web] → WebSocket → [audio-gateway-service (Spring WebFlux)]
                                      ↓ (Redis queue)
                              [live-stt-service]    → draft transcript
                              [final-stt-service]   → final transcript
                              [diarization-service] → speaker tags
                              [meeting-ai-service]  → summary + actions
                                      ↓
                              [transcript-service (Spring Boot)]
                                      ↓
                              [PostgreSQL] + [notification-service]
```

## Geliştirme Disiplini

Tam liste: [CLAUDE.md](./CLAUDE.md) + global `~/.claude/CLAUDE.md` HARD RULE seti.

Özet:
- **Cross-AI Peer Review** zorunlu (provider-level — code yazan ≠ review eden)
- **Plan Consensus Autonomy** — Codex MCP AGREE → direkt impl
- **No Fake Work** — test koşmadan "tests added" yasak
- **CI kırmızıyken merge YASAK** + **admin bypass YASAK**
- **No Closure Language** — sıradaki aksiyon her raporda
- **Türkçe cevap default**

## Faz Yol Haritası — 3-AI Mutabakat (2026-06-02)

Cross-AI consensus: Claude + Codex `019e879c` AGREE + Mavis `mvs_c922...` msg `78` AGREE. Canonical plan: [platform-k8s-gitops/docs/faz-24-meeting-intelligence-plan.md](https://github.com/Halildeu/platform-k8s-gitops/blob/main/docs/faz-24-meeting-intelligence-plan.md).

**Anahtar pivot**: `live-stt-service` ürün API'si **DEĞİL** — iç compute worker. Mobile/Web hiçbir zaman doğrudan platform-ai'a bağlanmaz. Audio Gateway Contract 1.0 önce kilitlenir, sonra STT entegre olur.

| Sıra | PR | Konu | Durum |
|---|---|---|---|
| **0** | charter | ADR-0030 KVKK + Observability skeleton + PLAN.md Faz 24 + canonical plan | 🟡 [PR #1207](https://github.com/Halildeu/platform-k8s-gitops/pull/1207) OPEN |
| **1** | PR-gw-01 | Audio Gateway Contract 1.0 freeze (platform-backend Spring WebFlux) | ⏳ Adım 0 sonrası |
| **2** | PR-stt-02 | live-stt real audio fixture + Docker e2e + resource pressure baseline | ⏳ PR-gw-01 sonrası |
| **3** | PR-stt-03 | supervised subprocess worker + hard timeout kill | ⏳ PR-stt-02 sonrası |
| **4** | PR-queue-01 | bounded Redis admission + Gateway → STT producer/consumer | ⏳ PR-stt-03 sonrası |
| **5** | PR-obs-01 | Grafana/Prometheus dashboard genişletme (skeleton 0'da) | ⏳ PR-queue-01 sonrası |
| **6** | PR-wer-01 | Common Voice TR + gerçek pilot meeting WER raporu (ADR girdisi) | ⏳ PR-stt-03 + pilot kayıt |
| **7** | PR-final-stt-01 | `final-stt-service` — WER sonucuna göre model kararı | ⏳ PR-wer-01 sonrası |
| **8** | PR-gpu-01 | GPU Dockerfile variant — donanım + ölçüm sonrası | ⏳ en son |

### 3-AI 3 RED (yapılmayacak)

1. ❌ Gateway contract kilitlenmeden mobile/Web veya STT WebSocket contract yazılması
2. ❌ KVKK ADR olmadan gerçek Workcube meeting kaydı kullanılması
3. ❌ Synthetic WER ile model kararı kapatılması

### Tahmini MVP

14-18 hafta (mevcut Faz 22-23 paralel devam ederken). Donanım stratejisi: CPU PoC → cloud GPU bridge (Lambda/Vast.ai) → production GPU kararı (WER + maliyet sonrası).

### Live durum (2026-06-02)

- ✅ `live-stt-service` PoC iskelet **LIVE** (PR #1 MERGED `4088d9a`) — FastAPI + faster-whisper medium int8 + 22/22 test PASS + Codex `019e877b` AGREE
- 🟡 Adım 0 charter (PR #1207) — Codex `019e879c` AGREE, CI gates bekleniyor
- ⏳ Sıradaki: PR-gw-01 (platform-backend Spring WebFlux Audio Gateway Contract 1.0)

## Hızlı Başlangıç (placeholder)

```bash
# Python 3.11+ venv
python -m venv .venv && source .venv/bin/activate
pip install -r services/live-stt-service/requirements.txt

# Local dev (PoC — CPU-only Whisper medium)
uvicorn services.live-stt-service.app:app --port 8200
```

## Lisans

Internal — Workcube ERP platform.
