# #52 Hukuk Danışman Review Paketi — ADR-0030 yükseltme girdisi

**Hedef:** ADR-0030'un PLACEHOLDER → ACCEPTED yükseltmesi için danışmana tek
dosyada eksiksiz bağlam. (#60 R-KVKK-1 bu review ile birlikte kapanır.)

## 1. İşleme faaliyeti özeti
Şirket-içi toplantıların ses kaydı → canlı transkript (draft+final) →
diarizasyon (anonim SPEAKER_XX, **voiceprint/biyometrik YOK**) → LLM ile
özet/karar/aksiyon çıkarımı. Tüm işleme **ülke-içi, şirket donanımında**
(#40 FINAL: lokal RTX 4070; cloud yok).

## 2. Veri kategorileri ve teknik önlemler (kodda zorunlu)
| Veri | Önlem | Kanıt |
|---|---|---|
| Ses (chunk) | Ülke-içi MinIO, şifreli volume, pilot sonrası imha | deploy/minio + #34 |
| Transkript | Loglar transcript-free (#30); UI'da PII redaction | stream.py KVKK notları, #97 |
| PII (TC/IBAN/tel/email/secret) | Regex redaction; **LLM'e gitmeden ÖNCE zorunlu** (env ile kapatılamaz) | meeting-ai `_enforce_kvkk_redaction_boundary` (#49) |
| Audit | 7 yıl retention, ayrı consumer | #31/#32 |
| Konuşmacı kimliği | Yalnız anonim etiket; enrolment ayrı consent fazına ertelendi | #48 |

## 3. Consent çerçevesi (hazır şablonlar)
Katılımcı aydınlatma + açık rıza: `docs/templates/wer-pilot-consent-email.md`,
`wer-pilot-consent-modal.md`; kayıt protokolü `wer-pilot-meeting-recording-protocol.md` (#34).
**Pilot kayıt, ACCEPTED öncesi YASAK** (mevcut kural korunuyor).

## 4. Danışmana sorular
1. Aydınlatma + açık rıza metinleri KVKK md.5/9 ve Aydınlatma Yükümlülüğü
   Tebliği'ne yeterli mi? Eksik unsur?
2. İşveren-çalışan bağlamında toplantı kaydı için açık rıza yeterli zemin mi,
   meşru menfaat değerlendirmesi mi gerekli? (İK görüşü dahil)
3. LLM Option A senaryosunda (yurt dışı API, yalnız redacted metin) md.9
   aktarım rejimi: standart sözleşme / açık rıza / yeterlilik — hangisi?
   (Öneri paketi: `docs/issue-54-llm-option-decision-support.md`)
4. 7 yıl audit retention'ın dayanağı ve kapsam sınırı uygun mu?
5. VERBIS kaydı gerekli mi (bkz. `verbis-bildirim-karari.md`, #53)?

## 5. İstenen çıktı
Her soruya yazılı görüş + ADR-0030 için ACCEPT/REVISE kararı. ACCEPT halinde
ADR statüsü güncellenir, #52 ve #60 kapanır; pilot kayıt yolu açılır.
