# WER Pilot Consent Modal Copy

Title: Pilot toplantı kaydı izni

Body:

Bu toplantı yalnızca Workcube içi STT doğruluk ölçümü için kaydedilecektir.
Kayıt, canlı ürün kullanımı veya müşteri verisi amacıyla kullanılmayacaktır.

Kayıt ile:

- sesiniz pilot WER/model değerlendirmesi için işlenecektir,
- manuel ground-truth transkript hazırlanacaktır,
- kayıt ve transkript şifreli alanda saklanacaktır,
- ham ses veya transkript git reposuna, loglara veya herkese açık ortama
  yazılmayacaktır,
- istediğiniz zaman silme/geri çekme talebi iletebilirsiniz.

Bu toplantıda müşteri adı, kişisel veri, parola, finansal veri veya gizli bilgi
paylaşılmamalıdır.

Buttons:

- Kabul ediyorum ve kayda izin veriyorum
- Kabul etmiyorum

Required metadata:

```json
{
  "consentVersion": "wer-pilot-consent-v1",
  "purpose": "STT WER/model evaluation",
  "recordingDurationTargetMinutes": "5-10",
  "withdrawalContact": "operator-defined"
}
```
