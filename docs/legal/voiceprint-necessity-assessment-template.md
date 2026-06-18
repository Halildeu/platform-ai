# Voiceprint Gereklilik & Etki Değerlendirmesi — PER-ENABLEMENT Şablon (DPIA-benzeri)

> **Ne için:** `voiceprint-m6-hukuk-sureci.md` **G4 + G9** gate maddesinin doldurulabilir şablonu. Voiceprint'i
> **bir bağlamda (tenant/org/dağıtım) AÇMADAN ÖNCE** bu form o bağlam için doldurulur + imzalanır. **Her
> enablement = ayrı doldurulmuş kopya** (§0.5 per-enablement; D7/D9).
>
> **Bu hukuki görüş DEĞİLDİR + bu form TEK BAŞINA AÇMA YETKİSİ VERMEZ.** Açma ancak G2-G14 evidence paketi +
> G12 imza + G13 server-side governance tamamlanınca yapılır. Karar sahibi (veri sorumlusu temsilcisi) + hukuk
> danışmanı doldurur/onaylar. Çerçeve hukuk triage'ında (L1, D7/D9) teyit edilir; **her instance** ayrıca imzalanır
> (§6 re-review tetikleyicileri varsa ayrı hukuk görüşü).
>
> **Altın kural:** §2.0 amaç elemesi veya §2 alternatifler amacı kabul edilebilir eşikte karşılıyorsa → voiceprint
> **gerekli değil → AÇILMAZ** (varsayılan non-biyometrik ile devam). Voiceprint daha iyi/kolay/hızlı olsa **bile**
> daha az müdahaleci yol "yeterince iyi" ise AÇILMAZ. "Ürün kolaylığı" biyometrik için yeterli gerekçe değildir.

---

## 0. Bağlam kimliği (context)

| Alan | Değer |
|---|---|
| Context ID (tenant/org/dağıtım) | `__________` |
| Tarih | `__________` |
| Karar sahibi (veri sorumlusu temsilcisi) | `__________` |
| Hukuk danışmanı | `__________` |
| Security/Ops onaylayan (G13 çift-onay) | `__________` |
| Geçerlilik / review tarihi (G14) | `__________` (örn. +12 ay veya bağlam değişiminde) |
| Bu bağlamda **özel grup / dış taraf** var mı? (çocuk/stajyer/dış müşteri/tedarikçi/sendika temsilcisi/hassas toplantı) | ☐ Yok ☐ Var → **default HAYIR veya ayrı hukuk review (§6)** |

---

## 1. İşleme tanımı (bu bağlamda)

- **Amaç (somut ve sınırlı):** Voiceprint bu bağlamda hangi dokümantasyon/konuşmacı-atfı ihtiyacını çözüyor? Hangi kırmızı alanlara (performans/disiplin/...) **kullanılmayacak**? (`__________`)
- **Kapsam:** hangi toplantı türü, hangi katılımcı seti (yalnız rıza vermiş çalışanlar; dış katılımcı KAPALI — §6 ana doküman).
- **Veri akışı:** ses → vektör → eşleştirme; nerede üretilir/saklanır (ülke-içi teyidi §5.7/G10 ana doküman).
- **Hacim/süre:** kaç kişi, ne sıklıkta, ne kadar süre.

---

## 2.0 AMAÇ ön-elemesi (fail-gate — önce bu)

- **Amaç açık, belirli, meşru ve bağlamla sınırlı mı?** ☐ Evet ☐ Hayır → *Hayır ise dur: AÇILMAZ.*
- **Amaç kırmızı alana kayıyor mu?** performans izleme / disiplin / verimlilik puanlama / davranış analitiği /
  gizli denetim → ☐ Hayır ☐ Evet → *Evet ise: AÇILMAZ (yasak/aşırı-müdahaleci amaç).*
- **Consequential-use yasağı kabul ediliyor mu?** Voiceprint çıktısı (konuşmacı atfı) **İK/performans/disiplin/
  hukuki/müşteri-uyuşmazlığı kararı için KULLANILAMAZ** — yalnız dokümantasyon kolaylığı. ☐ Kabul ☐ Hayır → *Hayır ise: AÇILMAZ.*

*Üç eleme de geçilmeden §2'ye geçilmez.*

---

## 2. GEREKLİLİK testi (en kritik — D7)

**Soru:** Daha az müdahaleci alternatifler bu amacı **kabul edilebilir eşikte** karşılamıyor mu? Voiceprint olmadan
**maddi** olarak ne kaybedilir (amaca somut engel)?

| Alternatif | Amacı karşılıyor mu? | **Kabul edilebilir eşik** (hedef metrik) | **Alternatif baseline + kanıt kaynağı** | **Maddi boşluk** — amaca neden engel? |
|---|---|---|---|---|
| Non-biyometrik diarizasyon (anonim `SPEAKER_xx`) | ☐E ☐H | `____` | `____` | `____` |
| Manuel profil-seçimi ("ben X'im" — kullanıcı kendini atar) | ☐E ☐H | `____` | `____` | `____` |
| Tek-seferlik manuel etiketleme (toplantı sonu) | ☐E ☐H | `____` | `____` | `____` |
| Hibrit (manuel atama + non-biyometrik tutarlılık) | ☐E ☐H | `____` | `____` | `____` |

**Kural:** Bir alternatif eşiği karşılıyorsa → voiceprint **AÇILMAZ.** "Maddi boşluk" nicelenmemiş/yumuşak (örn.
"biraz daha hızlı", "daha kullanışlı") ise → gereklilik **ZAYIF → AÇILMAZ.** Sayısal bir fark (örn. "%X daha az
manuel iş") bile tek başına yetmez; boşluğun amaca **neden maddi engel** olduğu gösterilmelidir.

**Gereklilik sonucu:** ☐ Voiceprint **gerekli** (somut maddi boşluk belgelendi) ☐ **gerekli DEĞİL → AÇILMAZ**

---

## 3. ORANTILILIK testi

| Test | Değerlendirme |
|---|---|
| **Minimum kapsam mı?** Yalnız gerekli katılımcı/toplantı/süre | `__________` |
| **Veri minimizasyonu** — en az veri, en kısa süre (§5 saklama/imha ana doküman) | `__________` |
| **Denge** — kişinin temel hak/özgürlüğüne etki vs amaç faydası; biyometrik = yüksek hassasiyet | `__________` |
| **Function creep guard** — voiceprint yalnız bu amaç için; başka amaca kullanılmayacağı yazılı (§2.0 consequential-use) | `__________` |

---

## 4. Risk değerlendirmesi (olasılık × etki + mitigasyon → residual)

| Risk | Olas. | Etki | Mitigasyon (gate map) | Residual |
|---|---|---|---|---|
| Yanlış eşleşme (false accept/reject) | `__` | `__` | confidence threshold + düşük-güvende **anonim `SPEAKER_xx` fallback** + "unknown speaker" + insan-onay (bağlayıcı) | `__` |
| **Alt-grup performans eşitsizliği / bias** (aksan/cinsiyet/yaş/konuşma bozukluğu/gürültü) | `__` | `__` | per-context validasyon + düşük-güvende anonim fallback + consequential-use yasağı + periyodik metrik review; **örneklem küçükse "ölçülmedi = sorun yok" DEĞİL → konservatif (öneri-etiketi/anonim/kritik toplantıda kapalı)** | `__` |
| Yanlış atfın zararı (consequential use) | `__` | `__` | **voiceprint çıktısı İK/performans/disiplin/hukuki karar için kullanılamaz** (§2.0) | `__` |
| Vektör/veri sızıntısı | `__` | `__` | şifreleme + RBAC + audit (G8) | `__` |
| **Geri çekme sonrası geçmiş etiketler** | `__` | `__` | template imha + geçmiş "X konuştu" etiketleri **pseudonymize/sil** (G7 geçmiş-veri politikası; D11) | `__` |
| **Enrollment kalitesi / model drift** | `__` | `__` | kaliteli enrollment + re-enrollment + periyodik validasyon + **model değişiminde G14 revalidation** | `__` |
| **Özel grup / dış katılımcı / çocuk** | `__` | `__` | §0 işaretliyse default HAYIR / ayrı legal review (§6) | `__` |
| **İtiraz & düzeltme hakkı** | `__` | `__` | kullanıcı yanlış atfı görür + itiraz + düzelttirir (risk mitigasyonu, sadece UX değil) | `__` |
| Açma kötüye kullanımı (un-gated) | `__` | `__` | server-side gated + çift-onay + immutable audit (G13) | `__` |
| Bağlam değişimi (sessiz drift) | `__` | `__` | per-context revalidation (G14) | `__` |
| Yurt dışı aktarım / alt-işleyen | `__` | `__` | mimari teyit + DPA (G10/§5.7 ana doküman) | `__` |

*(Olas./Etki: Düşük/Orta/Yüksek. Residual = mitigasyon sonrası kalan.)*

---

## 5. ÖN DEĞERLENDİRME SONUCU / ÖNERİLEN enablement kararı

> **Bu bölüm karar MERCİİ değildir — öneridir.** Bu form tek başına açma yetkisi VERMEZ; açma ancak G2-G14
> evidence + G12 imza + G13 server-side governance tamamlanınca yapılır.

- ☐ **ÖNERİ: EVET** — §2.0 elemesi geçildi + §2 gereklilik somut maddi boşlukla belgelendi + §3 orantılılık sağlandı + §4 residual kabul edilebilir.
- ☐ **ÖNERİ: HAYIR / AÇILMAMALI** — eleme veya alternatif yeterli / gereklilik zayıf / residual yüksek → **varsayılan non-biyometrik ile devam** (manuel/profil etiketleme). Voiceprint KODU mevcut ama bu bağlamda **inert** kalır. *(Geçerli + sık beklenen sonuç.)*

**Gerekçe (1-2 cümle):** `__________`

**İmzalar (G12 + G13 çift-onay):**

| Rol | Ad | İmza / onay | Tarih |
|---|---|---|---|
| Karar sahibi (veri sorumlusu temsilcisi) | | | |
| Hukuk danışmanı | | | |
| Security/Ops | | | |

---

## 6. Ayrı hukuk re-review tetikleyicileri (varsa instance imzası YETMEZ → yeni hukuk görüşü)

L1'de hukuk **metodolojiyi + tetikleyici listesini** onaylar. Standart düşük-riskli iç tenant enablement'ı için
ayrı uzun memo gerekmeyebilir (yine instance imzası + evidence review şart). Ama şunlardan **biri** varsa **ayrı
hukuk re-review ZORUNLU:**

☐ yeni/değişen amaç · ☐ dış katılımcı / çocuk / özel grup · ☐ yurt dışı aktarım ihtimali · ☐ yeni model / vendor /
telemetry · ☐ saklama süresi değişimi · ☐ yüksek residual risk · ☐ consequential-use ihtimali.

---

## Notlar

- Bu form **G4 + G9** kanıtıdır; doldurulmuş kopya enablement immutable audit log'una (G13) **evidence ID** ile bağlanır.
- Bağlam/amaç/katılımcı/alt-işleyen/model/saklama değişirse (G14) → form **yeniden doldurulur**, eski karar "needs-review".

*İmzalı enablement kopyasında provenance: "prepared internally; reviewed/approved by the roles below." (Bu iç
çalışma şablonu cross-AI istişareyle hazırlandı; hukuki görüş değildir — karar sahibi + hukuk doldurur/onaylar.)*
