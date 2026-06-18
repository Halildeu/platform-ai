# Voiceprint (Sesli Biyometrik) KVKK m.6 Hukuk Süreci — Go-Live Gate Paketi

> **#168 izleme dokümanı.** ADR-0035 Karar 2 (Halil, 2026-06-18): konuşmacı kimliği = voiceprint.
> Voiceprint **biyometrik / özel nitelikli kişisel veridir (KVKK m.6).** ADR-0033 amend (#169 MERGED)
> ile yasak **şarta bağlandı**: kod yazılabilir, **canlı biyometrik işleme bu paket tamamlanmadan AÇILAMAZ.**
>
> **Mimari posture (Halil, 2026-06-18):** voiceprint **OPSİYONEL + VARSAYILAN KAPALI** — düşürülmez, inşa edilir;
> **istendiğinde bağlam-başına GATED açılır** (serbest flip değil — §0.5). Varsayılan = non-biyometrik (sıfır m.6 işleme).
>
> **Veri sorumlusu = ŞİRKET** (tüzel kişi). **Halil = veri sorumlusu temsilcisi / karar sahibi** + hukuk danışmanı (teyit + imza).
> **Bu doküman hukuki görüş DEĞİLDİR** — yapısal süreç iskeleti + danışmana sorular. İnsan/hukuk onayı
> olmadan "hukuki dayanak" olarak kullanılamaz. Hazırlık: AI ajanı + cross-AI (Codex) istişare, Halil adına.
>
> **İlişki:** `kvkk-uygunluk-degerlendirmesi-taslak.md` (Soru 2 + Ek-A) **GENEL veriyi (m.5)** kapsar;
> bu doküman **m.6 özel-nitelikli (biyometrik) AYRI ve DAHA SIKI rejimi** kapsar (§1).

---

## §0 — Çekirdek tez (özet)

1. Voiceprint m.6 → işleme zemini **yalnız geçerli açık rıza** (meşru menfaat YOK). *(beklenen pozisyon — danışman m.6/3'ü teyit eder)*
2. Çalışan açık rızası, güç asimetrisi nedeniyle kırılgan → **maliyetsiz opt-out** rızayı özgür kılar (**zorunlu teminat, ama tek başına yeterli DEĞİL**).
3. Açık rıza olsa BİLE biyometrik işleme **gerekli + orantılı** olmak zorunda — manuel-etiketleme tam çalışıyorken voiceprint neden gerekli? Bu **ayrı değerlendirme** (§1.5) zorunlu.
4. m.6 ek yükümlülükler: güvenlik tedbirleri (§5.5), consent-enforcement teknik kanıt (§5.6), alt-işleyen/aktarım kontrolü (§5.7), etki değerlendirmesi (§1.5).
5. **Voiceprint = OPSİYONEL yetenek, VARSAYILAN KAPALI** (§0.5) — düşürülmez, inşa edilir; risk default-off + gated-açma ile yönetilir.

---

## §0.5 — Mimari posture: opsiyonel + default-OFF + gated-enablement (Halil direktifi, 2026-06-18)

**Karar:** Riskli biyometrik voiceprint **düşürülmez — opsiyonel yapılır.** *"Hepsi olacak":* voiceprint dahil
tüm yetenekler **kodlanır/inşa edilir** (ADR-0033 amend; kod serbest). Risk, **özellik kapısı** ile yönetilir,
özelliği silerek değil.

- **VARSAYILAN KAPALI:** Varsayılan ürün = non-biyometrik diarizasyon (anonim SPEAKER_xx) + manuel/profil
  etiketleme. **Varsayılan kurulumda HİÇ biyometrik işleme YOK** → varsayılan dağıtım m.6 açısından zaten
  uyumlu (özel-nitelikli veri işlenmiyor; bu hâlde gate gerekmez). Bu, *privacy-by-default + veri minimizasyonu*.
- **"İstendiğinde açılabilsin" = GATED açma, SERBEST FLİP DEĞİL:** Voiceprint'i bir bağlamda
  (tenant/org/dağıtım) AÇMAK, o bağlam için go-live gate'inin (§7 G2-G14) sağlanmasını gerektiren **açık,
  denetlenebilir, geri-alınabilir (kill-switch)** bir yönetici aksiyonudur. O bağlam için rıza/gerekçe/güvenlik/
  imza yoksa açılamaz. Açma kararı + kanıtı **per-bağlam loglanır**.
- **Sonuç (D7 yeniden çerçeveleme):** Gereklilik/orantılılık artık **global go/no-go DEĞİL, bağlam-başına
  gerekçe** (§1.5). Yetenek her yerde mevcut ama yalnız **gerekçeli + rızalı** bağlamlarda aktif. "Hepsini inşa
  et + her açmayı gerekçelendir" — en savunulabilir çerçeve (privacy-by-default + per-context justification).
- **Teknik:** capability flag `voiceprint.enabled` **default false**; per-bağlam override yalnız **gated
  enablement workflow** ile — **server-side enforced (sadece UI toggle DEĞİL) + RBAC + bypass-edilemez +
  immutable audit** (G13). Flag açık olsa bile **rıza yoksa işleme yok** (fail-closed, §5.6); kill-switch ile
  her an geri-kapatılır (geri-döndürülebilirlik).
- **TEST sınırı (Codex 019edc7b):** "kod serbest" yalnız **sentetik/anonim fixture** içindir. Geliştirme/test'te
  **gerçek çalışan sesi / gerçek voiceprint vektörü / pilot enrollment / prod-like kayıt** kullanmak artık "sadece
  kod" değil → **m.6 işleme başlar** → o da gate (G2-G14) kapsamındadır.

---

## §1 — Hukuki zemin: voiceprint m.6'dır, m.5 DEĞİL

| | Genel PII (m.5) — mevcut doc | **Voiceprint biyometrik (m.6) — bu doküman** |
|---|---|---|
| İşleme zemini | meşru menfaat (m.5/2-f) + destekleyici açık rıza | **açık rıza** (m.6/2) — meşru-menfaat m.6'da bağımsız zemin DEĞİL |
| Rıza geri çekilirse | m.5/2-f ile sürdürülebilir | **işleme DURUR** — başka zemin yok; voiceprint imha |
| Denge testi (Ek-A) | uygulanır | uygulanmaz (meşru menfaat m.6 zemini değil) — onun yerine **gereklilik/orantılılık** (§1.5) |

**Beklenen pozisyon:** Mevcut doc'un "meşru menfaat + rıza" çözümü voiceprint'e taşınmaz. KVKK m.6/2: özel
nitelikli veri kural olarak ilgilinin açık rızası ile işlenir. m.6/3 istisna seti (**7499 / 2024 değişikliği
sonrası güncel set**) özel sektör toplantı-voiceprint'ine büyük olasılıkla uygulanmaz → açık rıza tek
uygulanabilir zemin **gibi durur**. Kesin hüküm **danışman teyidine** (D1) bağlıdır.

> **Danışmana D1:** 7499-sonrası KVKK m.6/3 istisnalarını **madde madde** test et (kanunda açık öngörülme;
> bir hakkın tesisi/kullanılması/korunması; istihdam/İSG/sosyal güvenlik hukuki yükümlülüğü; alenileştirme; vb.).
> Şirket-içi toplantı-voiceprint'i bunların herhangi birine dayanabilir mi, yoksa açık rıza tek zemin mi?

---

## §1.5 — Gereklilik & Orantılılık (biyometrik için ZORUNLU — en kritik açık)

> **Per-enablement (§0.5):** Bu test artık **global go/no-go DEĞİL** — voiceprint varsayılan KAPALI; gereklilik
> **her açma (tenant/org/dağıtım) için ayrı** belgelenir. Aşağıdaki değerlendirme her enablement bağlamında yapılır.

**Sorun (Codex'in en güçlü itirazı):** "Manuel-etiketleme ile sistem tam çalışıyor" (§6) diyorsak, danışman/
denetçi haklı olarak sorar: **o halde biyometrik voiceprint neden gerekli ve orantılı?** Açık rıza biyometriği
*meşrulaştırsa* bile, KVKK genel ilkeleri (ölçülülük, veri minimizasyonu, amaçla sınırlılık) işlemeyi yine de
**gerekli + orantılı** olmaya zorlar. "Ürün kolaylığı" biyometrik için zayıf gerekçedir.

**Yapılacak — Biyometrik Gereklilik & Etki Değerlendirmesi (DPIA-benzeri ayrı belge, G-gate):**
- **Daha az müdahaleci alternatifler neden yetmiyor?** non-biyometrik diarizasyon (anonim SPEAKER_xx), kullanıcı
  profil seçimi ("ben Halil'im" manuel atama), tek-seferlik manuel etiketleme — hangi **ölçülebilir** ihtiyacı
  çözmüyor ki voiceprint gereksin? (Bu sorunun dürüst cevabı zayıfsa → voiceprint'ten vazgeçmek de bir sonuç.)
- **Kapsam daraltma:** voiceprint yalnız tekrarlayan iç toplantılarda mı; hangi katılımcı seti; ne kadar süre.
- **Risk / mitigasyon / residual risk** + karar sahibi imzası.

> **Danışmana D7:** Açık rıza olsa bile, manuel-etiketleme + non-biyometrik diarizasyon mevcutken voiceprint
> "gerekli/orantılı" sayılır mı? **Danışmana D9:** Bu risk sınıfı için DPIA-benzeri ayrı etki değerlendirmesi
> gerekli mi; minimum içeriği + imza sahibi kim?

---

## §2 — Açık rıza geçerliliği: işveren-çalışan + m.6

**Sorun:** Kurul içtihadı işçi-işveren güç asimetrisi → çalışan rızası "özgürce verilmiş" sayılmayabilir.
m.5'teki meşru-menfaat telafisi m.6'da YOK → rıza gerçekten özgür olmalı.

**Ana teminat (zorunlu ama tek başına YETERLİ DEĞİL): maliyetsiz opt-out (§6).** Çalışan HAYIR derse sistem
manuel-etiketleme ile tam çalışır, kişiye olumsuz sonuç yok → red maliyetsiz olduğundan rıza "özgür irade"
unsuruna yaklaşır. *Bu, geçerlilik riskini azaltan ana argümandır — geçerliliği tek başına garanti etmez;
§1.5 gereklilik + §5.5 güvenlik + genel ilkeler de sağlanmalı.*

**Geçerli açık rıza 5 unsur:** (1) ayrı/granüler (iş sözleşmesine/genel kayda gömülmez) · (2) bilgilendirilmiş
(§3 metni rıza anında) · (3) özgür (maliyetsiz opt-out §6) · (4) geri-çekilebilir (tek tık → imha §5) ·
(5) kayıtlı (kim/ne zaman/hangi versiyon — ispat yükü veri sorumlusunda).

> **Danışmana D2:** 5-unsur + maliyetsiz opt-out, m.6 çalışan-voiceprint rızasını "özgür irade" bakımından
> geçerli kılar mı? **D12:** Rıza yenileme periyodu, İK no-detriment politikası, yönetici-baskısını yasaklayan
> metin ve şikayet kanalı gerekli mi?

---

## §3 — Aydınlatma metni (voiceprint eklemesi)

Mevcut pilot şablonlarına voiceprint-özel bölüm: **veri** = ses imzası (biyometrik) · **hukuki sebep** = açık
rıza (m.6/2), *meşru menfaat değil* · **amaç** = otomatik konuşmacı tanıma · **saklama** (§5) · **opt-out + geri
çekme** kanalı + sonucu (manuel-etiketleme, dezavantaj yok) · **m.11 hakları + başvuru kanalı**.

> **Danışmana D3:** Voiceprint aydınlatması ayrı metin mi / mevcut aydınlatmaya bölüm mü; asgari unsurlar tam mı?

---

## §4 — VERBİS güncelleme

Şirket **kayıtlı**, **24-Biyometrik kategorisi mevcut (15 yıl)**. Voiceprint = **yeni kayıt değil, GÜNCELLEME**:
amaç ekle/teyit ("otomatik konuşmacı tanıma — biyometrik ses imzası") · saklama §5 ile hizala (15 yıl
**tavan** ≠ fiili) · alıcı = **hedef mimari: yok** (§5.7 teknik teyide bağlı) · zamanlama = **bir bağlamda ilk
biyometrik işleme/enrollment açılmadan ÖNCE** (per-enablement). Mevcut 24-Biyometrik kaydı voiceprint amacı/alıcı/
saklama/aktarımı zaten tam kapsıyorsa "güncelleme" yerine **kapsam-teyidi + evidence kaydı** yeterli olabilir — ama o da açmadan önce.

> **Danışmana D4:** 24-Biyometrik mevcut amaç beyanı voiceprint'i kapsıyor mu, amaç eklenmeli mi?

---

## §5 — Saklama / imha (veri minimizasyonu)

VERBİS 15 yıl bir **tavan**; fiili = **amaçla sınırlı + minimize**:
- Ses-imzası yalnız **aktif çalışan + geçerli rıza** süresince.
- **İmha tetikleyicileri:** rıza geri çekildi / çalışan ayrıldı / amaç sona erdi / [pilot] pilot bitti.
- İmha **geri-döndürülemez** (vektör + ham örnek), imha kaydı tutulur (ispat).
- Ham ses vs voiceprint vektörü ayrı süre (ham ses pilot sonrası imha; vektör tetikleyicilere bağlı).
- **Geri çekme sonrası geçmiş veri:** template silinince, önceki toplantılarda voiceprint ile üretilmiş
  "X konuştu" etiketleri ne olacak — silinecek mi / pseudonymize mi / hangi zeminde kalacak? (D11)

> **Danışmana D5:** Biyometrik ses-imzası fiili saklama süresi? "Aktif çalışan + rıza; ayrılış/geri-çekmede
> imha" yeterli mi, azami süre (ör. 6/12 ay yenileme) gerekli mi? **D11:** Geri çekmede yalnız template mi,
> geçmiş speaker etiketleri de mi silinir/pseudonymize edilir?

---

## §5.5 — Özel nitelikli veri güvenlik tedbirleri (m.6 ek yükümlülük)

Kurul'un özel-nitelikli-veri güvenlik tedbirleri çerçevesi: **vektör şifreleme** (rest+transit) · anahtar
yönetimi (**Vault/KMS**, key rotation) · **rol-bazlı erişim + least privilege** · erişim audit log + periyodik
gözden geçirme · log redaction (biyometrik loglara sızmaz) · **yedeklerden de imha** · incident response planı.

> **Danışmana D8:** Biyometrik voiceprint için asgari teknik/idari tedbirler neler; eksik kontrol var mı?

---

## §5.6 — Consent-enforcement teknik kanıt (fail-closed)

Rıza yoksa → **enrollment yok, matching yok, vektör üretimi yok, cache/log/backup'ta kalıntı yok.** Feature-flag
+ kill-switch + **negatif test kanıtı** (rızasız kullanıcıda hiçbir biyometrik artefakt oluşmadığı kanıtlanır).
Bu, ADR-0033 amend'in "kod serbest, canlı YASAK" sınırının teknik teminatıdır.

> **Hukuk-gate vs mühendislik-gate ayrımı (Codex):** Hukuk = "hangi kontrol gerekir, hangi eşik sağlanmalı,
> m.9/DPA tetikleniyor mu?" (gereksinim seti). **Mühendislik/Ops** = "rızasız kullanıcıda vektör oluşmuyor"
> negatif testi, kill-switch, log/cache/backup kontrolü, mimari aktarım kanıtı, vendor/telemetry envanteri
> (kanıt). **Final gate** = hukuk + şirket bu kanıt paketini kabul eder. §5.6 + §5.7 kanıt-üretimi agent/Zeynep
> işidir (hukuk değil); gereksinim setini hukuk teyit eder (D8/D10).

---

## §5.7 — Alt-işleyen & aktarım kontrolü (m.9)

"Alıcı yok / yurt-içi" iddiası **mimari kanıtla** desteklenmeli (assertion değil): diarizasyon/voiceprint
model kaynağı (pyannote/HuggingFace indirme), telemetry, cloud STT/LLM (mevcut: lokal Ollama — m.9 yok),
crash-reporting, monitoring/log export → herhangi biri yurt dışına veri taşıyor mu? Taşıyorsa m.9 + DPA/işleyen
sözleşmesi. **Hedef mimari: alıcı/yurtdışı yok; go-live öncesi teknik teyit (G-gate).**

> **Danışmana D10:** Model sağlayıcı/alt-işleyen/telemetry/cloud/monitoring-export/yurtdışı aktarım için hangi
> DPA / m.9 / standart-sözleşme kontrolleri gerekir?

---

## §6 — Rıza-vermeyene alternatif + kapsam

ADR-0035 Karar 2-b garantisi: **manuel-etiketleme fallback HER ZAMAN açık** → rızasız/geri-çeken çalışan için
sistem tam fonksiyonel; **performans/görev/terfi etkisi YOK** (yazılı İK garantisi). §2 özgür-rıza teminatının
teknik+organizasyonel ayağı.
- **Kapsam:** voiceprint yalnız **rıza vermiş çalışanlar**. **Dış katılımcı / müşteri / tedarikçi** voiceprint'i
  ayrı rıza akışı olmadan **kapalı** (varsayılan: dış sesler manuel/anonim).

> **Danışmana D6:** Opt-out + no-detriment yazılı garanti m.6 rıza geçerliliği için yeterli teminat mı?
> Dış katılımcı voiceprint'i için ayrı rejim gerekir mi?

---

## §7 — Per-Enablement Gate (bir bağlamda voiceprint AÇMAK için HEPSİ ✓ + imza; VARSAYILAN KAPALI gate gerektirmez)

> **§0.5 gereği:** Voiceprint varsayılan KAPALI → biyometrik işleme yok → gate gerekmez. Aşağıdaki gate, voiceprint'i
> **bir bağlamda (tenant/org/dağıtım) AÇMAK** istendiğinde o bağlam için işler; G1-G14 sağlanmadan o bağlamda açılamaz.


- [x] **G1 — ADR-0033 amend** (voiceprint şarta-bağlı) — #169 MERGED.
- [ ] **G2 — Hukuki zemin teyidi** (§1): m.6 açık-rıza-tek-zemin (D1, 7499-sonrası tam set).
- [ ] **G3 — Açık rıza geçerlilik** (§2): 5-unsur + opt-out (D2, D12) + İK no-detriment politikası.
- [ ] **G4 — Gereklilik & Etki Değerlendirmesi** (§1.5): DPIA-benzeri belge + daha-az-müdahaleci-alternatif analizi (D7, D9) — imzalı.
- [ ] **G5 — Aydınlatma metni** (§3) hazır + danışman onayı (D3).
- [ ] **G6 — VERBİS güncelleme** (§4) yapıldı (go-live öncesi) + danışman (D4).
- [ ] **G7 — Saklama/imha** (§5) yazıldı + imha mekanizması **test edildi** + geçmiş-veri politikası (D5, D11).
- [ ] **G8 — Güvenlik tedbirleri** (§5.5) uygulandı (D8).
- [ ] **G9 — Consent-enforcement teknik kanıt** (§5.6): fail-closed + negatif test kanıtı.
- [ ] **G10 — Alt-işleyen/aktarım** (§5.7): mimari teyit (alıcı/m.9 yok) + gerekirse DPA (D10).
- [ ] **G11 — Opt-out fallback canlı** (§6) + kapsam sınırı (dış katılımcı kapalı).
- [ ] **G12 — Şirket (veri sorumlusu) + hukuk imzası** — tüm pozisyonlar onaylı.
- [ ] **G13 — Enablement governance & audit evidence** (Codex 019edc7b): bir bağlamda voiceprint açma yalnız
  **yetkili rol** tarafından, **hukuk/veri-sorumlusu-temsilcisi + security/ops ÇİFT-ONAY** ile yapılır;
  **server-side enforced** (sadece UI toggle değil — RBAC + bypass-edilemez). Enablement kararına: context ID,
  amaç, kapsam, kişi grubu, rıza-metni versiyonu, etki-değerlendirmesi (G4) ID, VERBİS teyidi, retention-policy ID,
  security-evidence (G8) ID, approver'lar, timestamp, expiry/review tarihi → **immutable audit log**. Break-glass
  açma **yasak** (veya ayrı incident kaydı + otomatik kill-switch review).
- [ ] **G14 — Per-context revalidation**: bağlam/amaç/katılımcı tipi/alt-işleyen/model-telemetry/saklama süresi/
  veri-sorumlusu rolü değişirse enablement **otomatik "needs-review"**; G4/G6/G8/G10/G12 yeniden geçilmeden açık kalamaz.

**Kapanış disiplini:** Danışman cevabı (D1-D12) yalnız **hukuki gereksinim/tasarım pozisyonunu** kapatır;
**G6-G14'ün operasyonel/teknik/yönetişim kapanışı ayrıca kanıt ister** (VERBİS güncelleme, imha testi, enforcement
negatif-test, fallback canlı, mimari teyit, enablement çift-onay+audit) — danışman teyidiyle otomatik kapanmaz.
Voiceprint **KODU** bu gate'ten bağımsız yazılabilir (ADR-0033 amend); yalnız **canlı biyometrik işleme** (bir
bağlamda) G1-G14 tamamlanınca açılır.

---

## §8 — Danışmana sorular (özet, ~1 saat teyit hedefi)

D1 (§1) 7499-sonrası m.6/3 istisnaları madde madde — voiceprint dayanabilir mi / açık rıza tek zemin mi?
D2 (§2) 5-unsur + maliyetsiz opt-out çalışan-voiceprint rızasını geçerli kılar mı?
D3 (§3) aydınlatma ayrı metin mi / asgari unsurlar tam mı?
D4 (§4) VERBİS 24-Biyometrik amaç beyanı voiceprint'i kapsıyor mu?
D5 (§5) biyometrik ses-imzası fiili saklama süresi?
D6 (§6) opt-out + no-detriment yeterli mi + dış katılımcı rejimi?
D7 (§1.5) açık rıza olsa bile voiceprint gerekli/orantılı mı (fallback tam çalışırken)?
D8 (§5.5) biyometrik asgari teknik/idari güvenlik tedbirleri?
D9 (§1.5) DPIA-benzeri etki değerlendirmesi gerekli mi / min içerik + imza sahibi?
D10 (§5.7) alt-işleyen/aktarım/m.9 için DPA/standart-sözleşme kontrolleri?
D11 (§5) geri çekmede template + geçmiş speaker etiketleri silinir/pseudonymize mi?
D12 (§2) rıza yenileme + İK no-detriment + baskı-yasağı + şikayet kanalı gerekli mi?

**Yürütme — 3 faz (Codex, "12 soru 1 saatte kapanır" gerçekçi değil):**
- **Faz L1 — Hukuki Tasarım Triage** (~60-90 dk danışman, ön-okuma sonrası): D1-D12'ye yazılı kısa
  **RAG/verdict** (AGREE / REVISE / RED / needs-evidence) + revizyon notları. G2-G5 + G6-G14.in *hukuki gereksinim* tarafı.
- **Faz E1 — Operasyonel/Teknik Kanıt Paketi**: G6-G14 kanıtları (VERBİS güncelleme, imha testi, enforcement
  negatif-test, fallback canlı, mimari/aktarım teyidi) — engineering/ops (agent/Zeynep) üretir.
- **Faz L2 — Go-Live Legal Sign-off**: hukuk + şirket temsilcisi kanıt paketini görerek **G12** imzalar +
  enablement **G13-G14** governance işler → o bağlamda voiceprint açılır (#168 gate o bağlam için kapanır).

**Önemli sonuç (§0.5 ile):** Voiceprint **VARSAYILAN KAPALI** — yetenek inşa edilir, varsayılan non-biyometrik
çalışır. D7 cevabı bir bağlamda "gereklilik zayıf" çıkarsa → **o bağlamda AÇMA** (varsayılan ile devam); gerekçe +
rıza güçlü başka bir bağlamda gated açılabilir. Yani "açmama" global red **değil** — default-off + per-context
gated açma. Hiçbir bağlamda gerekçe doğmazsa yetenek inşa edilmiş ama **teknik olarak inert** kalır → canlı m.6
işleme riski oluşmaz; ancak **bypass / yanlış-konfigürasyon / supply-chain residual** riski **G13-G14**
(server-side enforce + immutable audit + revalidation) ile yönetilir (mutlak "sıfır" değil).

---

*Hazırlık: AI ajanı + cross-AI (Codex thread 019edc4f) adversarial istişare, Halil adına — 2026-06-18.
Hukuki görüş değildir; danışman teyidi şarttır. Veri sorumlusu = şirket; Halil = temsilci/karar sahibi.*
