# ADR-0035: Capture Stratejisi (Meeting Intelligence — Adoption Kapısı)

- Status: **ACCEPTED** (Karar 1 + Karar 2 — Halil, 2026-06-18)
- Tarih: 2026-06-18
- Issue: #160 [Faz24 T-A] Capture — adoption kapısı [P0]
- Kapsam: Toplantı sesini hangi yolla yakalayacağız + konuşmacı kimliği nasıl kurulacak

## Bağlam

Capture = ürünün #1 adoption kapısı. audio-gateway (ingest, Redis Streams)
canlı ve hazır; ama onu besleyecek capture client SIFIR. Issue net emir veriyor:
"bot vs native vs recorder — TEK güçlü yol seç, 3 client'ı parlatma = over-engineering."

## Değerlendirilen yollar

| Kriter | Teams Bot | Zoom/Meet Bot | Recorder (Electron) |
|---|---|---|---|
| Kapsadığı toplantı | Sadece Teams | Sadece Zoom/Meet | Hepsi (+ yüz yüze) |
| Yüz yüze (temas) | ❌ | ❌ | ✅ mikrofon |
| Hibrit (oda+uzak) | ❌ | ❌ | ✅ tek cihaz |
| Maliyet | 🔴 compliance lisansı | 🔴 API tier | ✅ 0 ₺ |
| Ölçek | 🔴 merkez GPU | 🔴 merkez GPU | ✅ kullanıcı cihazı |
| Kurulum | 🔴 tenant admin onayı | 🔴 workspace onayı | ✅ tek seferlik |
| 3.parti bağımlılık | 🔴 MS Graph | 🔴 2 API | ✅ yok |

## Karar 1 — Capture yolu: **RECORDER (Electron) primary** ✅

Halil kararı (2026-06-18): ana kanal = **Masaüstü/Mobil Recorder.** Tek güçlü yol
(tüm platform + yüz yüze + hibrit + 0₺ + cihazda ölçek + KVKK-temiz). Bot'lar
**şimdilik defer** — ileride "tam otomatik katılım" talebi olursa gündeme alınır.

→ Client geliştirme açılabilir: desktop (#75-84) / mobile (#85-94).

## Karar 2 — Konuşmacı Kimliği: **(a) Voiceprint** ✅ (go-live HUKUK-GATE'li)

Halil kararı (2026-06-18): otomatik tanıma için **voiceprint** (kişiden ~20sn ses
imzası → otomatik tanı). Voiceprint **biyometrik / özel-nitelikli veridir**
(KVKK m.6); bu yüzden **kod yazılabilir ama canlı işleme aşağıdaki gate'ler
tamamlanmadan AÇILAMAZ.**

| Yol | Karar |
|---|---|
| (a) Voiceprint | ✅ **SEÇİLDİ** — go-live hukuk-gate'li |
| (b) Manuel etiketleme | rıza-vermeyene **fallback** olarak kalır |
| (c) Herkes kendi cihazından | desteklenir (S2); voiceprint tek-ortak-cihazı da destekler |

### Voiceprint GO-LIVE gate'leri (KVKK m.6)
1. **ADR-0033 amend** — voiceprint yasağı kaldırılır (ayrı PR).
2. **Hukuk süreci** (canlı kullanımdan ÖNCE tamamlanmalı):
   - açık rıza framework (çalışan–işveren rıza geçerliliği — hukuk teyidi şart)
   - VERBİS güncelleme + aydınlatma metni
   - saklama / imha politikası
   - rıza-vermeyene alternatif yol (manuel-etiketleme fallback, Karar 2-b)

> Voiceprint **kodu** yazılabilir; **canlı işleme** hukuk süreci + rıza altyapısı
> tamamlanana kadar açılamaz (KVKK m.6 gate).

## Açık Sorular — yanıtlandı

S1. **Tek ekran:** ✅ Evet — tüm toplantılar (recorder/online/yüz yüze, kaynak
    fark etmez) tek dashboard'da birleşir (audio-gateway her kaynağı tek formata
    çevirir, meetingId ile birleştirir).

S2. **Cihaz modeli:** Voiceprint **tek-ortak-cihaz** senaryosunu da destekler;
    UX/cihaz tasarımı buna göre kilitlenebilir. (Herkes-kendi-cihazı varyantı da
    açık kalır — her ses ayrı kanal + KVKK-temiz.)

## Sonuçlar

- **Karar 1** → seçilen client geliştirme (desktop #75-84 / mobile #85-94) başlar.
- **Karar 2** → tetiklenen işler:
  - ADR-0033 voiceprint-ban **amend PR** (ayrı)
  - **Hukuk süreci kickoff issue** (yukarıdaki 4 madde; Halil + hukuk koordinasyonu)
  - voiceprint go-live KVKK m.6 gate'i (hukuk + rıza altyapısı tamamlanmadan canlı yok)
- **G-CAP gate:** kayıt tamamlanma oranı + konuşmacı doğruluk oranı ≥ hedef.
