# Ayrı kopyada görev sahibi denemesi — 24 Eylül 2026

> **25 Eylül güncellemesi:** Üç farklı yaklaşımın yeni ölçümleri
> [RESULTS-20260925.md](./RESULTS-20260925.md) dosyasında. Hiçbiri kalite kabulünü
> geçmedi. Bu klasör yalnız ayrı deneydir; uygulamaya bağlanmadı.

**Güncel durum: yerel gerçek model karşılaştırması tamamlandı; aday kabul edilmedi.
Canlıya/TEST'e bağlanmadı; APK üretilmedi.**

Sonraki aşamada `local_compare.py` ile kurulu yerel `llama3.1:8b` üzerinde sekiz
örnek iki yoldan çalıştırıldı. Mevcut ve aday yolların ikisi de 4/8 tam sonuç verdi;
asıl `Mehmet.` vakası adayda da başarısız. Model TEST'tekinden farklı olduğundan
bu yalnız ön değerlendirmedir. İstem, ham yanıt, ret nedenleri ve sürelerin tamamı
[güncel raporda](<C:/Users/zeynep.akkilic/Documents/New project/outputs/isolated-owner-test-20260924/GERCEK-MODEL-SONUC.md>)
bağlantılıdır. Model kurulmadı/indirilmedi, ortak sunucuda çıkarım çalıştırılmadı.

**Aşağıdaki bölümler ilk çevrimdışı aşamanın kaydıdır:** “gerçek model çağrılmadı”,
“henüz ölçülmedi” ve 158 test ifadeleri o aşamaya aittir. Güncel çevrimdışı
kontrol sayısı, gerçek model koşucusunun altı birim testiyle birlikte 164'tür.

Bu klasör uygulamanın çalıştırdığı koddan bağımsızdır. Mevcut `app/`, API sözleşmesi,
Speechmatics ayarları, mobil uygulama ve Electron değiştirilmedi. Yeni bir sunucu
kurulmadı. Gerçek toplantı veya kullanıcı hesabı kullanılmadı.

## Doğrulanan neden

Taban kod: `13aa7b8303d7e0f85bef94a56b1e5d164d1884ed`.
Bu, yerel kaynak sürümüdür; o anki çalışan sunucunun yeniden doğrulanması değildir.

Önceden alınmış sentetik karşılaştırmanın normal konuşma metni şu sınırı içeriyor:

> Mehmet. Bütçe tablosunu 26 Eylül 2026 günü saat 12'ye kadar kontrol edecek.

`app/services/analyze.py` içindeki `OllamaAnalyzer._analyze`, model menüsünü
`selectable_sentences(...)` ile süzüyor. `Mehmet.` tek içerik sözcüğü olduğu için
bu menüden çıkarılıyor. Model çağrısının yerine kontrollü yanıt koyan test, gerçek
istem oluşturma yolunu çalıştırdı: **Zeynep istemde var; Mehmet yok.**

Dolayısıyla analiz girişinin tam metin özeti/hash'i Mehmet'i kapsasa bile, modelin
seçim yaptığı istem onu içermiyor. Bu deneyde kaybın somut bir nedeni kanıtlandı.
Bu kanıt tek başına geçmişte hangi dağıtımda bozulduğunu veya her boş görev sahibi
sonucunun aynı nedenden oluştuğunu göstermez.

Diğer ayrım da korundu: model zaten `owner=null` döndürürse ret sayacı boş kalıyor;
kontrollü yanıt `owner=Mehmet` verirse mevcut tek-cümle denetimi bunu reddediyor.
İki yol aynı eksik sorumlu görüntüsünü üretebilir. Gerçek modelin gizli çıktısının
ne olduğu bu kontrollü testlerden çıkartılmadı.

## Denenen yaklaşım

`candidate.py`, kısa parçaları **bağlam olarak koruyan** ayrı bir istem üretir.
Kısa parça bağlamda görünür, kendi başına görev cümlesine dönüştürülmez. Noktalar,
orijinal metin, kaynak cümle numaraları ve tarihler değiştirilmez.

Görev sahibi önerisinin doğrulayıcısı şunları kontrol eder:

- İsim ve görev için ayrı, orijinal kaynak referansları; metin hash'i, karakter
  aralığı, alıntı hash'i ve gateway olay sıra numaraları.
- Ayrılmış ismin hemen sonraki görevle ilişkilendirildiğinin açıkça önerilmesi;
  aradaki bir cümleyi atlayıp eski bir ismi ödünç almama.
- Ayrı kaynaklarda aynı konuşmacı etiketi ve aynı oturum kapsamı; eksiksiz metin
  aralığı/zaman eşleştirmesi. `S1` bir kişinin gerçek kimliği sayılmaz.
- Deneysel zaman aralığı sınırı, farklı konuşmacı/oturum, eksik veya çakışan
  etiketler ve eski metin hash'inde atamadan kaçınma.
- Görev cümlesinde zaten desteklenen başka bir sorumluyu değiştirmeme.

Normal sentetik kayıtta elle tanımlanmış doğru model önerileri verildiğinde
Zeynep ve Mehmet'in ikisi de korunuyor. Mehmet'in referansları **3 ve 4 numaralı
iki özgün kaynak** olarak kalıyor; yeni bir birleşik cümle uydurulmuyor. Duraklamalı
kayıtta Mehmet zaten görev cümlesindeyken tek kaynakla işlem sürüyor.

**Bu sonuç gerçek modelin iki ismi doğru çıkardığı anlamına gelmez.** Öneriler
testte kontrollü olarak verildi. Gerçek çıkarım servisi çağrılmadı.

## Yayına engel olan bilinen sınır

“Mehmet. Bütçe tablosunu ben kontrol edeceğim.” ifadesinde Mehmet'e hitap ediliyor
olabilir. Kaynak yakınlığı, aynı konuşmacı ve zaman kontrolü tek başına bu ayrımı
yapamaz. Yanlış `subject_continuation` önerisi bilerek verildiğinde yapısal
doğrulayıcı bunu kabul edebiliyor. Bu karşı örnek testte açıkça tutuldu;
`semantic_quality_verified` her zaman `false`.

Belirsiz/ilgisiz/iptal edilmiş ilişki önerilerinde atama yapılmaması da test edildi;
**modelin bu anlamsal etiketi doğru seçeceği henüz ölçülmedi**. Bu nedenle bu prototip
uygulamaya bağlanmaya hazır değildir. Yalnız bir istem talimatını daha güçlü
yazmak veya zaman sınırını değiştirmek başarı kanıtı sayılmaz.

Mevcut analiz API'sinin `TranscriptSegment` alanları metin ve başlangıç/bitiş zamanı;
konuşmacı/oturum kapsamı taşımıyor. Gelecek bir uygulama denemesinde hem bu güvenilir
veri aktarımı hem iki kaynaklı kanıt sözleşmesi açıkça tasarlanmalı. Canlı bağlam
penceresi ilerlerken görevle birlikte isim kaynağı da korunmalı. Bunlar mevcut
API'ye bu çalışmada eklenmedi.

## Test sonucu ve kapsamı

Son çalıştırma: **158 geçti, 1 kapsam dışında.** Bunun 44'ü bu deneyin başlangıç ve
aday kontrolleri; 114'ü mevcut seçici, alıntı/kanıt, canlı bağlam, model adaptörü ve
sahte analiz birim testleri. Aday dosyasında dal/satır kapsamı %100; bu oran
**anlamsal doğruluk veya canlı kullanım başarısı değildir**.

Windows'ta `test_live_endpoint_threads_cursor_and_keeps_original_citation_offsets`
testinin ASGI test istemcisi, asyncio için yerel socketpair oluşturuyor. İlk koşuda
bu işlem ağ korumasınca engellendi. Koruma kaldırılmadı; bu tek uç nokta testi son
koşuda açıkça kapsam dışında bırakıldı. Bu test için başarı iddiası yok.

Koşucu ortamdan işletim sistemi değişkenleri dışındaki ayarları temizler; Python
socket bağlantısı/DNS/bind/sendto ve alt süreç açma denemelerini engeller. Son
koşuda öz denetim dışında böyle bir girişim görülmedi. Bu süreç içi korumadır,
işletim sistemi seviyesinde güvenlik alanı değildir.

Ruff, Black ve mypy denetimleri ayrıca uygulanır. Son test makbuzu, JUnit ve kapsam
dosyaları çalışma alanında `outputs/isolated-owner-test-20260924/` altındadır.

## Tekrar çalıştırma

Çalışma dizini: `services/meeting-ai-service`. Mevcut Python bağımlılıkları yeterlidir;
paket indirmek, hesap açmak veya sunucuya bağlanmak gerekmez.

```powershell
python -I experiments/owner_context/run_offline.py --stage regression --output <yerel-dizin>/regression.json
python -I -m ruff check experiments/owner_context
python -I -m black --check experiments/owner_context
python -I -m mypy --follow-imports=silent --explicit-package-bases experiments/owner_context
```

## Veri kaynağı ve sonraki doğrulama

`fixtures/normal.json` ve `fixtures/paused.json`, daha önce tamamlanan sentetik
[karşılaştırma koşusunun](https://github.com/Halildeu/platform-k8s-gitops/actions/runs/35971201863)
yerel çıktılarından alındı. Bu çalışmada o koşu tekrarlanmadı. Fixture dosyaları
kaynak artifact hash'i ve doğrulanmış analiz metni hash'ini taşır. Bu yeni deney
ses dosyasını motora yeniden göndermedi; önceden kaydedilmiş olayları kullandı.

Canlı kaliteyi ölçmek için sonraki aşamada ayrı ve onaylı bir çıkarım ortamında
aynı veri üzerinde gerçek model yanıtları değerlendirilmelidir: doğru görev sahibi,
yanlış görev sahibi, belirsiz durumda boş bırakma, iptal/yeniden atama, kaynak
bağlantıları ve gecikme birlikte ölçülmelidir. Sadece iki olumlu cümle veya toplam
test sayısı yeterli değildir. Mevcut TEST/masaüstü bu deney için kullanılmadı.

Şu an telefonda değişiklik yok; yeni APK veya tekrar telefon testi gerekmiyor.
