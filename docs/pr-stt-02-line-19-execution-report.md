# PR-stt-02 Line 19 Execution Report

Date: 2026-06-04

Repo: `C:\Users\zeynep.akkilic\Desktop\platform-ai-work`

Branch: `feature/pr-stt-02-ag-019-resource-baseline`

GitHub Project item: `#19 [PR-stt-02][AG-019] Faz 24 two-host resource baseline: staging-sw orchestration + platform-ai STT compute (ADR-0031 re-scope)`

## Kisa Karar

Bu adim baslatildi, fakat staging acceptance gate bu PC'de tamamlanamadi.

Sebep:

- `staging-sw` host adi bu PC'de cozulmuyor.
- `k3d-test` kubectl context bu PC'de yok.

Bu nedenle #19 icin asil istenen staging resource baseline verileri henuz alinamadi. Bu rapor, yapilan read-only denemeleri ve eksik kalan gate'leri acik sekilde kayda alir.

## Plan #19 Ne Istiyordu?

GitHub Project #19 body:

```text
PR-stt-02 e2e oncesi acceptance gate:
- `ssh halil@staging-sw 'free -m'` available > 2 GiB
- `kubectl --context k3d-test top node + top pod -A` Faz 22-23 paralel is profili
- CPU fallback no-GPU verify
- Hangi servisler aktif (Faz 22.5 PR-D2.5 + Faz 23 notify durumu)
- Baseline ciktisi PR body'sinde tablo

Mavis 'staging resource pressure acceptance gate' onerisi (msg 74).
```

## Amac

#19'un amaci kod yazmak degil, PR-stt-02 e2e oncesi iki-host kapasite kapisini kontrol etmektir.

ADR-0031 mimarisinde:

- `staging-sw`: orchestration / gateway / core platform tarafi
- `platform-ai`: STT compute tarafi

Bu adim, STT compute isleri baslamadan once staging tarafinda kaynak baskisi var mi yok mu gormeyi hedefler.

## Calistirilan Read-only Kontroller

### 1. Tool availability

Kontrol edilen araclar:

```text
ssh
kubectl
docker
gh
```

Bu PC'de bulunanlar:

```text
C:\Windows\System32\OpenSSH\ssh.exe
C:\Program Files\Docker\Docker\resources\bin\kubectl.exe
C:\Program Files\Docker\Docker\resources\bin\docker.exe
```

### 2. staging-sw memory baseline denemesi

Komut:

```bash
ssh -o BatchMode=yes -o ConnectTimeout=10 halil@staging-sw "free -m"
```

Sonuc:

```text
ssh: Could not resolve hostname staging-sw: Bilinen boyle bir ana bilgisayar yok.
```

Degerlendirme:

Bu PC, `staging-sw` host adini cozemiyor. Bu DNS/hosts/VPN/Tailscale/Cloudflare Access veya SSH config eksigi olabilir. Bu veri alinmadan `available > 2 GiB` gate'i PASS denemez.

### 3. kubectl context listesi

Komut:

```bash
kubectl config get-contexts
```

Sonuc:

```text
CURRENT   NAME   CLUSTER   AUTHINFO   NAMESPACE
```

Degerlendirme:

Kubectl config bos gorunuyor. Bu PC'de kullanilabilir Kubernetes context yok.

### 4. k3d-test node baseline denemesi

Komut:

```bash
kubectl --context k3d-test top node
```

Sonuc:

```text
error: context "k3d-test" does not exist
```

Degerlendirme:

`k3d-test` context yok. Node CPU/memory baseline alinamadi.

### 5. k3d-test pod baseline denemesi

Komut:

```bash
kubectl --context k3d-test top pod -A
```

Sonuc:

```text
Error in configuration: context was not found for specified context: k3d-test
```

Degerlendirme:

Pod bazli resource baseline ve aktif servis listesi bu PC'den alinamadi.

## Acceptance Gate Durumu

| Gate | Plan beklentisi | Bu PC'deki sonuc | Durum |
| --- | --- | --- | --- |
| staging memory | `ssh halil@staging-sw 'free -m'`, available > 2 GiB | Hostname cozulmedi | BLOCKED |
| k3d node metrics | `kubectl --context k3d-test top node` | `k3d-test` context yok | BLOCKED |
| k3d pod metrics | `kubectl --context k3d-test top pod -A` | `k3d-test` context yok | BLOCKED |
| CPU fallback no-GPU verify | CPU Docker STT baseline | #17'de PASS | DONE |
| Active services | Faz 22.5 PR-D2.5 + Faz 23 notify durumu | Cluster erisimi yok | BLOCKED |
| Baseline PR body table | PR body'ye tablo | Taslak tablo hazirlanabilir, staging verisi eksik | PARTIAL |

## #17'den Devralinan CPU Fallback Kaniti

#17 CPU fallback baseline zaten tamamlanmisti.

Kaynak rapor:

```text
docs/poc-stt-baseline.md
docs/pr-stt-02-line-17-execution-report.md
```

Ozet:

| Metric | Value |
| --- | ---: |
| Model | `medium` |
| Device | `cpu` |
| Compute type | `int8` |
| Cold start total | `40.097687s` |
| Cold API `elapsed_ms` | `10872ms` |
| Approx. model load + overhead | `29.226s` |
| Warm transcribe wall-clock | `7.718536s` |
| Warm API `elapsed_ms` | `7701ms` |
| Peak observed container memory | `1.503GiB` |
| Docker smoke | PASS |
| Integration test | `3 passed, 50 deselected` |

Bu, #19 icindeki "CPU fallback no-GPU verify" maddesini destekler. Ancak staging-sw/k3d resource gate yerine gecmez.

## PR Body Baseline Table Taslagi

Staging erisimi duzeldikten sonra PR body'ye su tablo konmali:

| Check | Command | Expected | Observed | Status |
| --- | --- | --- | --- | --- |
| staging-sw free memory | `ssh halil@staging-sw 'free -m'` | available > 2 GiB | TODO | TODO |
| k3d node resource | `kubectl --context k3d-test top node` | node metrics available | TODO | TODO |
| k3d pod resource | `kubectl --context k3d-test top pod -A` | active services visible | TODO | TODO |
| active Faz 22.5/Faz 23 services | `kubectl --context k3d-test get pods -A` | PR-D2.5 + notify state known | TODO | TODO |
| CPU fallback STT | #17 Docker smoke + integration | PASS | PASS | PASS |
| STT baseline | `docs/poc-stt-baseline.md` | model/load/memory/latency table | present | PASS |

## Eksik Bilgiler

#19'un tamamlanmasi icin su bilgiler/erisiler gerekli:

1. `staging-sw` host adi bu PC'de nasil cozulmeli?
   - DNS mi?
   - hosts file mi?
   - SSH config alias mi?
   - Tailscale mi?
   - Cloudflare Access mi?

2. `halil@staging-sw` SSH erisimi icin hangi key/config gerekli?

3. `k3d-test` kubectl context nasil alinacak?
   - kubeconfig dosyasi mi?
   - `kubectl config use-context` icin mevcut context adi farkli mi?
   - Docker Desktop kubectl yerine repo/cluster kubeconfig mi kullanilmali?

4. Faz 22.5 PR-D2.5 ve Faz 23 notify durumunu hangi namespace/pod adlariyla kontrol edecegiz?

## Halil Bey'e Sorulacak Net Mesaj

```text
AG-019 / PR-stt-02 #19 icin staging resource baseline almaya basladik.

Bu PC'de su iki blocker var:
1. `ssh halil@staging-sw 'free -m'` -> staging-sw hostname cozulmuyor.
2. `kubectl --context k3d-test top node/pod -A` -> k3d-test context yok.

#19 acceptance gate'i tamamlamak icin staging-sw SSH alias/DNS bilgisini ve k3d-test kubeconfig/context kurulumunu paylasabilir misiniz?

#17 CPU fallback baseline hazir:
- Docker smoke PASS
- integration 3 passed
- cold start 40.097687s
- warm elapsed_ms 7701ms
- peak memory 1.503GiB

Staging erisimi gelince #19 PR body baseline tablosunu dolduracagiz.
```

## Plandan Sapma Var Mi?

Kod veya mimari sapma yok.

Planin istedigi read-only acceptance komutlari denendi. Ortam erisimi olmadigi icin staging kaynak verisi alinamadi. Bu bir implementasyon sapmasi degil, environment/access blocker'dir.

## Kod Degisti Mi?

Hayir.

Bu adimda runtime kodu, test kodu veya Docker script degismedi.

Eklenen dosya:

```text
docs/pr-stt-02-line-19-execution-report.md
```

## Test Calistirildi Mi?

Hayir.

Gerekce:

#19 bir staging resource acceptance gate adimi. Bu adimda asil test, staging ve kubectl read-only komutlaridir. Kod degismedigi icin unit/integration test calistirilmasi gerekmiyor.

Calistirilan gate komutlari:

```bash
ssh -o BatchMode=yes -o ConnectTimeout=10 halil@staging-sw "free -m"
kubectl config get-contexts
kubectl --context k3d-test top node
kubectl --context k3d-test top pod -A
```

## Halil Bey Reposuna Etki

Yok.

Bu calismada:

- `upstream/Halildeu/platform-ai` icin push yapilmadi.
- PR acilmadi.
- Halil Bey'in GitHub reposuna dokunulmadi.

## Son Durum

#19 tamamlanmadi; access blocker var.

Tamamlanmasi icin:

1. `staging-sw` SSH hostname/alias cozumlenmeli.
2. `k3d-test` kubeconfig/context kurulumu yapilmali.
3. `free -m`, `kubectl top node`, `kubectl top pod -A`, aktif servis kontrolleri tekrar alinmali.
4. PR body baseline tablosu gercek observed degerlerle doldurulmali.

Bu rapor, #19'un bu PC'de neden tamamlanamadigini ve eksik kalan kabul kapilarini izlenebilir hale getirir.

## Provisional Devam Karari

Bu madde P0 Blocker oldugu icin normal sartta #20'ye gecmeden once tamamlanmalidir. Ancak bu PC'de eksik olan kisim kod degil, staging erisim bilgisidir.

Bu nedenle, proje sahibi/ekip pratik ilerleme isterse asagidaki kontrollu yol izlenebilir:

```text
AG-019 staging resource gate pending; implementation validated locally only.
```

Bu not, #19 tamamlanmadan yapilacak sonraki her is raporuna eklenmelidir.

## Halil Bey'den Beklenen Bilgiler

#19'u gercek anlamda tamamlamak icin Halil Bey'den veya staging ortamindan su bilgiler alinmalidir:

1. `staging-sw` SSH erisim bilgisi
   - Hostname gercekten `staging-sw` mi?
   - Degilse IP veya dogru DNS adi nedir?
   - SSH alias icin `~/.ssh/config` kaydi gerekiyor mu?
   - Hangi user kullanilmali: `halil` mi, baska bir user mi?
   - Hangi SSH key kullanilmali?

2. Kubernetes kubeconfig/context bilgisi
   - `k3d-test` context bu PC'ye nasil eklenecek?
   - Context adi gercekten `k3d-test` mi?
   - Kubeconfig dosyasi nereden alinacak?
   - Docker Desktop kubectl yeterli mi, yoksa farkli kubeconfig path mi kullanilmali?

3. Metrics kontrol kosullari
   - `kubectl top node` calismali mi?
   - `metrics-server` cluster'da kurulu mu?
   - Kurulu degilse `top` yerine hangi alternatif metrik kaynagi kullanilacak?

4. Aktif servis kontrolu
   - Faz 22.5 PR-D2.5 servisleri hangi namespace/pod/deployment adlariyla kontrol edilecek?
   - Faz 23 notify hangi namespace/pod/deployment adlariyla kontrol edilecek?
   - Capacity exhaustion alarmi hangi dashboard/alert uzerinden kontrol edilecek?

## Provisional Devam Edersek Kabul Ettigimiz Riskler

#19 tamamlanmadan #20 ve sonraki islere sadece local/fork seviyesinde devam edilirse, asagidaki riskler kabul edilmis olur:

| Risk | Etki | Sonradan geri donunce beklenen is |
| --- | --- | --- |
| staging RAM < 2 GiB cikabilir | Worker count/model secimi fazla agresif kalabilir | `max_workers`, model, timeout ve queue ayarlari dusurulur |
| k3d-test pod/node CPU baskisi yuksek cikabilir | STT compute staging servislerini etkileyebilir | Backpressure/worker concurrency daha konservatif ayarlanir |
| Faz 22.5/Faz 23 servisleri zaten yuk altinda olabilir | PR-stt-02 e2e gecse bile staging'de kararsiz davranabilir | PR body'de resource risk notu ve capacity gate sonucu yazilir |
| metrics-server/top yok olabilir | Planin metrik kaniti eksik kalir | Alternatif metrik kaynagi veya Halil Bey ortam duzeltmesi gerekir |
| #20/#21 localde dogru, stagingde agir cikabilir | Ayar tuning gerekir | Kod bastan yazilmaz; config/limit tuning yapilir |

Bu riskler projeyi lokal/fork ortaminda bozmaz. Ancak resmi PR, merge veya deploy oncesinde #19'a geri donulmeden ilerlemek dogru degildir.

## Geri Donunce Hedeflenen Sonuc

#19'a geri donuldugunde hedeflenen sonuc sudur:

```text
staging-sw memory gate: PASS veya acik risk notu
k3d-test node metrics: PASS veya alternatif metrik kaniti
k3d-test pod metrics: PASS veya alternatif metrik kaniti
active services state: documented
CPU fallback baseline: PASS (#17)
PR body resource table: filled with observed values
```

Eger tum gate'ler PASS olursa:

- #19 tamamlanmis sayilir.
- #20/#21 gibi sonraki islerin local/fork sonuclari staging kapasite acisindan desteklenmis olur.
- PR body'ye gercek resource baseline tablosu konur.

Eger gate'lerden biri FAIL olursa:

- Proje bastan yazilmaz.
- Once config ve kapasite ayarlari gozden gecirilir.
- Muhtemel aksiyonlar:

```text
max_workers azalt
modeli kucult veya GPU/dedicated host sartini netlestir
request timeout'u ve queue/backpressure ayarlarini siki yap
PR body'de staging capacity riskini acik yaz
```

## Provisional Devam Icin Kurallar

#19 tamamlanmadan sonraki islere gecilecekse su kurallar uygulanmalidir:

1. Calisma sadece local branch ve `zeynep-serban/platform-ai` fork'unda kalir.
2. Halil Bey upstream repo'ya PR/merge/deploy yapilmaz.
3. Her sonraki is raporuna su not eklenir:

```text
AG-019 staging resource gate pending; implementation validated locally only.
```

4. #19 erisim bilgileri gelince bu dosya guncellenir.
5. Resmi PR veya merge oncesinde #19 mutlaka tekrar acilir ve gercek staging verileriyle kapatilir.
