# ADR-0035: Capture Stratejisi (Meeting Intelligence — Adoption Kapısı)

- Status: PROPOSED — KARAR BEKLİYOR (Halil + hukuk)
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

## Karar 1 — Capture yolu (AÇIK KARAR — Halil)

Üç yol yukarıdaki tabloda değerlendirildi. Analiz özeti (karar değil, girdi):
- Recorder tek client ile tüm platformlar + yüz yüze + hibrit + sıfır maliyet +
  ölçek + KVKK-temiz sağlıyor; bot'ların tek üstünlüğü otomatik katılım.
- Bot'lar yüz yüze/hibrit yapamıyor, lisans/merkez-GPU maliyeti getiriyor, tek
  platforma kilitli.

Nihai capture yolu seçimi Halil'e aittir — biz yalnızca analizi sunuyoruz.

Halil'in karara bağlaması:
- Ana kanal hangisi olacak? (recorder / Teams bot / Zoom-Meet bot)
- Bot'lar ikincil/opsiyonel olarak gündeme alınsın mı, hiç mi?

## Karar 2 — Konuşmacı Kimliği (AÇIK KARAR — Halil + hukuk)

Ürün şartı: her konuşmacının sesi ayrılmalı VE tanımlanmalı (isimli).

| Yol | Nasıl | Otomatik? | KVKK |
|---|---|---|---|
| (a) Voiceprint (kişiden 20sn ses imzası → otomatik tanı) | bir kez kur, hep otomatik | ✅ | 🔴 BİYOMETRİK / özel-nitelikli (m.6) — açık rıza + hukuk + ADR-0033 değişikliği |
| (b) Manuel etiketleme | sistem ayırır, insan "1=Ahmet" der | ❌ HER toplantıda elle | ✅ temiz |
| (c) Herkes kendi cihazından | kimlik login'den gelir | ✅ | ✅ temiz |

Analiz girdisi (karar değil): otomatik ayırma şartı (a) voiceprint veya (c) ile
karşılanır; (b) manuel her toplantıda elle uğraştırır.

Halil + hukuğun karara bağlaması:
1. Biyometrik voiceprint yolu açılıyor mu? (ADR-0033'ün voiceprint yasağı kalkar)
2. Açık rıza süreci geçerli mi? (çalışan–işveren rızası KVKK'da tartışmalı)
3. Ek yükümlülükler kimde? (VERBİS, aydınlatma, saklama/imha, rıza-vermeyene alternatif)
4. Hukuk netleşene kadar geçici olarak (b) insan-etiketleme dursun mu?

## Açık Sorular

S1. Tek ekran: Tüm toplantılar (recorder/online/yüz yüze — kaynak fark etmez)
    kullanıcıya TEK dashboard'da mı sunulacak? (Mimari buna uygun: audio-gateway
    her kaynağı tek formata çevirir, meetingId ile birleşir.)

S2. Cihaz modeli: Konuşmacı kimliğini kesinleştirmek için herkes KENDİ
    cihazından mı (telefon yeterli) bağlanacak, yoksa odada TEK ortak cihaz mı
    kullanılacak?
    - Herkes kendi cihazı → her ses ayrı kanal + isim kesin (KVKK-temiz, (c))
    - Tek ortak cihaz → tek mikrofon → ayrım için (a) voiceprint veya (b) gerekir
    Bu seçim Karar 2'yi doğrudan belirliyor.

## Sonuçlar

- Karar 1 onaylanınca → seçilen client geliştirme (desktop #75-84 / mobile #85-94) başlar
- Karar 2 (a) çıkarsa → ADR-0033 güncellenecek + hukuk süreci tetiklenecek
- S2'nin yanıtı cihaz/UX tasarımını ve konuşmacı kimliği yöntemini kilitler
- G-CAP gate: kayıt tamamlanma + (seçilen yolda) konuşmacı doğruluk oranı
