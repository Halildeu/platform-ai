# AGENTS.md — platform-ai

Bu dosya repo içindeki en yüksek öncelikli giriş yüzeyidir. Yeni bir agent veya oturum bu repoda bağlam toplarken önce bu dosyayı, hemen ardından [CLAUDE.md](./CLAUDE.md) ve [README.md](./README.md) dosyalarını okur.

## 1. Okuma Sırası

Kural ve öncelik çözümü için:
1. `AGENTS.md`
2. Global `~/.claude/CLAUDE.md` HARD RULE seti
3. `CLAUDE.md` (repo-specific tamamlayıcı)
4. `README.md` (proje + ekosistem haritası)

Soru tipine göre otoriter kaynak:
- **Mimari karar**: `docs/adr/*.md` (planlı — ADR-0001 charter + ADR-0002 STT model seçimi vb.)
- **Roadmap / faz / done kriteri**: ana repo `PLAN.md` Faz 24 + bu repo `docs/faz-24-*`
- **Aktif iş durumu**: [platform Roadmap board](https://github.com/users/Halildeu/projects/2) Project #2
- **Servis API**: `services/<svc>/openapi.json` (FastAPI auto-generate)
- **Cluster manifest**: `platform-k8s-gitops` repo `kustomize/base/apps/<svc>/`

Navigator ama karar kaynağı olmayan yüzeyler:
- `README.md`
- `CLAUDE.md` (agent-özel tamamlayıcı)
- `docs/session-handoff-*.md` (planlı, henüz yok)

## 2. Repo Kimliği

- Bu repo `platform-ai` **Python mikroservisleri** (STT, diarization, meeting AI) için **kaynak kod ve image build** repo'sudur.
- Manifest, GitOps desired-state, ArgoCD apply `platform-k8s-gitops` repo'sundadır.
- Backend Spring Boot servisleri (`meeting-service`, `transcript-service`, `audio-gateway-service`) `platform-backend` repo'sundadır.
- Frontend MFE (`mfe-meeting`) `platform-web` repo'sundadır.
- Mobile client `platform-mobile` repo'sundadır.

## 3. HARD RULE (özet)

Tam liste: global `~/.claude/CLAUDE.md` + repo [CLAUDE.md](./CLAUDE.md).

Repo-spesifik öne çıkanlar:

- **PII/KVKK boundary**: Ses dosyası + transcript = hassas veri. Encryption + retention ADR + audit + consent zorunlu.
- **STT model pin**: Whisper model adı + version + hash environment variable ile pin'lenir.
- **GPU disiplini**: PoC CPU-only önce → metrik ölç → Codex consensus → GPU karar.
- **Cross-AI Peer Review**: Code yazan sağlayıcı review yapmaz. Python repo'da Codex review thread her PR için zorunlu.
- **Test koşmadan "tests added" YASAK**: pytest output + coverage rapor olmadan PR yeşil sayılmaz.
- **Workcube ekosistem reuse**: Standalone publish yok; auth/notification/audit/permission reuse zorunlu.
- **Türkçe cevap default**: Kullanıcıya yönelen tüm cevaplar Türkçe.

## 4. Çalışma Disiplini

- Yeni servis öncesi mevcut FastAPI pattern (`services/<svc>/`) referans alınır.
- Whisper model değişimi ayrı ADR + PoC ölçüm gerektirir.
- Ses dosyası işleyen kod = PII handler; logging redaction zorunlu.
- Cross-repo değişim (örn. transcript-service API kontratı) eş-zamanlı PR ile yapılır.
- Codex iter sırasında plan-time AGREE → direkt impl (Plan Consensus Autonomy).
