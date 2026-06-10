# KVKK Uygunluk Değerlendirmesi — Taslak Pozisyonlar (#52 / #53 / #60 girdisi)

> **NİTELİK UYARISI:** Bu doküman AI yardımıyla hazırlanmış bir **karar destek
> taslağıdır; hukuki mütalaa DEĞİLDİR.** Amaç: `adr-0030-hukuk-review-paketi.md`
> içindeki 5 danışman sorusuna gerekçeli taslak cevaplar sunarak, veri
> sorumlusunun (şirket yönetimi) avukatsız karar alabilmesini veya avukat
> süresini dakikalara indirmesini sağlamak. Nihai onay insan imzası gerektirir.
> 2024 tarihli 7499 sayılı Kanun değişiklikleri (m.6 ve m.9 revizyonu) dikkate
> alınmıştır; güncel Kurul kararları go-live öncesi teyit edilmelidir.

## Soru 1 — Aydınlatma + açık rıza metinleri yeterli mi?

**Taslak cevap: Büyük ölçüde evet; 3 küçük ekleme önerilir.**

Mevcut şablonlar (`wer-pilot-consent-email.md`, `wer-pilot-consent-modal.md`)
Aydınlatma Yükümlülüğü Tebliği'nin asgari unsurlarını (veri sorumlusu kimliği,
amaç, hukuki sebep, aktarım, haklar) karşılıyor. Eklenmesi önerilen:

1. **Saklama sürelerinin metinde açıkça yazılması** (ses: pilot sonrası imha;
   transkript: 2 yıl önerisi; audit: 7 yıl) — Tebliğ m.5/1-ç "süre" unsuru.
2. **m.11 haklarının kullanım kanalı** (başvuru e-posta adresi / form) —
   Veri Sorumlusuna Başvuru Usul ve Esasları Tebliği uyumu.
3. **Kaydı reddetme yolunun** açık yazılması: toplantıya kayıtsız katılım
   alternatifi veya kayda girmeme hakkı (rızanın "özgür irade" unsuru için
   kritik — aşağıda Soru 2).

## Soru 2 — İşveren-çalışan bağlamında açık rıza yeterli zemin mi?

**Taslak cevap: Tek başına rızaya YASLANMA; "meşru menfaat (m.5/2-f) +
destekleyici açık rıza" çift zemin kullan.**

Gerekçe: Kurul içtihadında işçi-işveren ilişkisindeki güç asimetrisi nedeniyle
çalışan rızasının "özgürce verilmiş" sayılmama riski bilinir. Bu yüzden:

- **Birincil zemin:** m.5/2-f meşru menfaat — toplantı dokümantasyonu,
  aksiyon takibi ve kurumsal hafıza, temel hak ve özgürlükleri zedelemeyen
  ölçülü bir menfaattir. **Denge testi bu dokümanın ekinde kayıt altına
  alınmalı** (aşağıda Ek-A şablonu).
- **Destekleyici:** açık rıza yine alınır (şablonlar hazır) — ama rıza geri
  çekilirse işleme m.5/2-f'ye dayalı sürdürülebilir; kişiye kayıtsız katılım
  alternatifi sunulur.
- **İK teyidi:** Bu pozisyonun İK politikasına işlenmesi (çalışan el kitabı /
  bilgilendirme) önerilir.

## Soru 3 — LLM Option A (yurt dışı API) m.9 aktarım rejimi?

**Taslak cevap: Şu an SORU DOĞMUYOR; Option B seçildi (lokal Ollama, #54/#133
merged). Yurt dışına veri çıkmıyor.**

- Mevcut mimari: özet/karar/aksiyon çıkarımı **ülke-içi, şirket donanımında**
  (Ollama backend). m.9 tetiklenmez. Redaction yine de zorunlu tutuluyor
  (derinlemesine savunma).
- İleride Option A'ya dönülürse: 2024 sonrası m.9'da "standart sözleşme"
  (Kurul'a 5 iş günü içinde bildirim) en pratik yol olur; yalnız
  ***REDACTED*** metin gönderimi + işleyici sözleşmesi (DPA) şart. Bu karar
  noktası `issue-54-llm-option-decision-support.md`'de zaten dokümante.

## Soru 4 — 7 yıl audit retention dayanağı uygun mu?

**Taslak cevap: Evet, savunulabilir; kapsamı dar tut.**

- Dayanak çerçevesi: genel zamanaşımı (TBK m.146: 10 yıl) ve iç
  denetim/uyuşmazlık ispat ihtiyacı karşısında 7 yıl orantılı bir tercihtir;
  e-defter/vergi pratiğindeki saklama alışkanlığıyla uyumlu.
- **Kapsam sınırı (kritik):** 7 yıl YALNIZ audit olay kayıtları için
  (kim/ne zaman/hangi işlem — transkript içeriği DEĞİL). Ses ve transkript
  kendi (daha kısa) sürelerine tabi. Bu ayrım `#31/#32` tasarımında zaten var;
  ADR-0030'a tek cümle olarak işlenmesi önerilir.

## Soru 5 — VERBIS kaydı gerekli mi?

**Taslak cevap: Şirketin mevcut yükümlülük durumuna bağlı — karar ağacı:**

```
Çalışan sayısı > 50 VEYA yıllık mali bilanço > 25M TL?
├─ HAYIR → VERBIS muafiyeti devam eder; bu faaliyet muafiyeti bozmaz
│          (özel nitelikli veri ana faaliyet olarak işlenmiyor).
│          → Yazılı iç not yeterli; #53 bu notla kapanır.
└─ EVET (veya zaten kayıtlı) → MEVCUT kaydı GÜNCELLE (yeni kayıt değil):
           + amaç: "toplantı dokümantasyonu ve iş takibi"
           + kategori: "işitsel kayıt, transkript"
           + alıcı: yok (ülke-içi; üçüncü taraf aktarım yok)
           + saklama: ses=pilot sonrası imha / transkript=2 yıl / audit=7 yıl
           → Güncelleme go-live (#59) ÖNCESİ yapılmalı (G5 gate).
```

`verbis-bildirim-karari.md` madde 1'deki ☐ kutucuğunu yönetim doldurunca
yukarıdaki dal otomatik belli olur — **tek eksik bilgi bu.**

## Ek-A — Meşru Menfaat Denge Testi (şablon, yönetim onaylar)

| Test adımı | Değerlendirme |
|---|---|
| Menfaat meşru mu? | Toplantı kararlarının doğru kaydı, aksiyon takibi, kurumsal hafıza — meşru ve somut |
| İşleme zorunlu mu? | Manuel not almak hataya açık; otomasyon ölçülü alternatif. Veri minimizasyonu uygulanıyor (PII redaction, anonim SPEAKER_XX, transcript-free loglar) |
| Denge: hak ihlali riski? | Düşük: ülke-içi işleme, şifreli depolama, kısa saklama, redaction zorunlu, kayıtsız katılım alternatifi sunuluyor |
| Sonuç | ☐ Menfaat ağır basar — m.5/2-f uygulanabilir (imza/tarih: ________) |

## Kapanış prosedürü önerisi

1. Yönetim (veri sorumlusu temsilcisi) bu dokümandaki 5 pozisyonu okur;
   katıldıklarını işaretler, Ek-A'yı imzalar, VERBIS kutucuğunu doldurur.
2. Mümkünse bir avukata "yalnız bu dokümanın teyidi" için danışılır
   (tahmini süre: < 1 saat). Mümkün değilse yönetim kararı yazılı alınır.
3. ADR-0030 → ACCEPTED yükseltilir → **#52 kapanır.**
4. VERBIS dalına göre işlem yapılır/iç not yazılır → **#53 kapanır.**
5. İkisi kapanınca gap kalmaz → **#60 kapanır.**

*Hazırlayan: Cursor (Fable 5) AI ajanı, zeynep-serban adına — 2026-06-10.*
*Bu taslak insan onayı olmadan "hukuki görüş" olarak kullanılamaz.*
