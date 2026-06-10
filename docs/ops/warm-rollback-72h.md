# #58 72h Warm Rollback Hazırlığı (meeting/transcript veri koruması + Faz 23 paritesi)

**İlke:** Cutover (#56) sonrası eski stack 72 saat **çalışır halde** bekler;
geri dönüş tek config değişikliğidir, veri kaybı sıfırdır.

## Warm tutulanlar
- Eski gateway route hedefi (pasif ama ayakta, health-checked)
- Cutover anındaki MinIO mirror + DB snapshot (immutable, 72h+7gün saklama)
- Eski model cache'leri (yeniden indirme gecikmesi olmasın)

## Veri koruması kuralları
1. **Append-only pencere:** 72h boyunca yeni stack'in yazdığı meeting/transcript
   kayıtları ayrı prefix'te (`v24/...`) — rollback eski veriyi ezmez, yeni veri
   de kaybolmaz (rollback sonrası reconcile listesi çıkar).
2. **Çift-yazım YOK** (karmaşıklık riski); bunun yerine snapshot + append-only
   prefix + reconcile. Faz 23'te aynı desen kullanıldı.
3. KVKK: snapshot'lar da ülke-içi host'ta, şifreli volume'de; 72h+7gün sonunda
   otomatik imha (retention job), audit kaydıyla (#32).

## Rollback prosedürü (< 5 dk hedef)
1. Gateway route'u eski hedefe çevir (cutover'ın tersi — tek config).
2. Yeni stack'i DURDURMA (delil + reconcile için 24h daha ayakta tut).
3. Reconcile: `v24/` prefix'indeki kayıtları raporla → operatör kararıyla
   eski şemaya taşı veya pilot verisi olarak arşivle.
4. Post-mortem şablonu doldur (tetikleyici, etki, kök neden, tekrar planı).

## Faz 23 paritesi kontrol listesi
- [ ] Route switch tek-config mi? (cutover provası ile doğrula)
- [ ] Snapshot restore provası yapıldı mı? (test ortamında 1 kez zorunlu)
- [ ] Grafana'da eski+yeni stack yan yana panel var mı?
- [ ] 72h sonunda warm stack'i söndürme runbook adımı takvimlendi mi?
