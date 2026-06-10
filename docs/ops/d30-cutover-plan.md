# #56 D30 Atomic Cutover Planı (Faz 22-23 mantığı reuse)

**Amaç:** Faz 24 Meeting Intelligence'ı tek, geri-alınabilir bir pencerede
production'a almak. Faz 22-23'ün kanıtlanmış deseni: önkoşul gate'leri →
atomic switch → doğrulama → 72h warm rollback (#58).

## Önkoşul gate'leri (hepsi yeşil olmadan cutover YOK)
| Gate | Kanıt | Durum (2026-06-10) |
|---|---|---|
| G1 Kod: tüm STT/GPU zinciri main'de | PR #107-#129 merged | ✅ |
| G2 Test deploy GPU'da doğrulandı | deploy-test-2026-06-10, /transcribe 200 OK | ✅ |
| G3 Model/donanım kararları FINAL | ADR-0031 + #40 | ✅ (pilot kalibrasyonu hariç) |
| G4 KVKK: hukuk review + ADR-0030 ACCEPTED | #52 | ⏳ operatör |
| G5 VERBIS kararı | #53 | ⏳ operatör |
| G6 LLM Option kararı onayı | #54 paketi hazır | ⏳ onay |
| G7 MinIO prod ayakta + bucket policy | #55 compose hazır | ⏳ host apply |
| G8 Browser smoke acceptance PASS | #57 checklist | ⏳ koşulacak |
| G9 Rollback prep tamamlandı | #58 planı | ⏳ prova |

## Cutover adımları (D30 penceresi, tahmini 60 dk)
1. **T-24h:** Yeni stack'i prod host'ta *pasif* başlat (live-stt shared K=2,
   final-stt turbo, diarization, meeting-ai mock→onaylı backend, MinIO).
   Sağlık: `/health` hepsi `ok`; `nvidia-smi` baz VRAM kaydet.
2. **T-1h:** Veri koruması: mevcut meeting/transcript verisinin snapshot'ı
   (MinIO mirror + DB dump) — #58'in rollback girdisi.
3. **T-0 (atomic switch):** Gateway route'u eski endpoint'ten yenisine çevir
   (tek config değişikliği = atomiklik). Eski stack DURDURULMAZ (warm, #58).
4. **T+10dk doğrulama:** smoke istekleri (gerçek olmayan test sesi!),
   correlation-id uçtan uca, Grafana STT panelleri (#28), hata oranı < %1.
5. **T+60dk karar:** yeşilse pencere kapanır; değilse **rollback tetik**
   (#58: route'u geri çevir — tek config, < 5 dk).

## Rollback tetikleyicileri
Hata oranı > %5 (5 dk pencere) · p95 > 2× baz · OOM/VRAM tavanı · KVKK
şüphesi (transkript log sızıntısı) → anında geri dön, post-mortem.

## Sorumlular
Switch: operatör (Halil/Zeynep) · Gözlem: Grafana nöbeti · Karar: müdür.
