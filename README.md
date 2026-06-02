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

## Faz Yol Haritası

| Faz | Konu | Durum |
|---|---|---|
| 24.0 | Charter + ADR-0030 + repo iskeleti | ⏳ planning |
| 24.1 | Backend skeleton (meeting + transcript service Spring Boot) | ⏳ |
| 24.2 | Python service skeleton (live-stt + final-stt + Redis) | ⏳ |
| 24.3 | Audio Gateway (Spring WebFlux WebSocket) | ⏳ |
| 24.4 | MinIO setup + audio storage akışı | ⏳ |
| 24.5 | React Native + Expo mobile MVP | ⏳ |
| 24.6 | platform-web `mfe-meeting` MFE | ⏳ |
| 24.7 | Meeting AI (LLM özet/karar/aksiyon) | ⏳ |
| 24.8 | Diarization (pyannote.audio + GPU karar) | ⏳ |
| 24.9 | Notification + report-service `weekly-meeting-summary` entegre | ⏳ |
| 24.X | Teams/Zoom recording webhook entegrasyonu (ileri) | ⏳ |

Tahmini MVP: 14-18 hafta (mevcut Faz 22-23 paralel devam ederken).

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
