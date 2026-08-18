Loaded 1 case(s). Opening BCC…
Using BCC credentials in memory only.
Authenticated BCC page: https://bcc-app.bank.corp.centercredit.kz:4030/ecd/pkg_w_e_dossier.p_main?p_arm=CBS25A9F2F0536520E21BE53CAD7B5
[1] BIN 240540019483: searching...
BIN 240540019483: customer Товарищество с ограниченной ответственностью "AV TRANS CORPORATION" (12155298)
BIN 240540019483: dossier filter matched 1 dossier(s)
  dossier 2217408: OPP/2026/U/S/003722
    no target match in Платежные поручения; sample: Платежные поручения | ПП выдача | № - ; Платежные поручения | ПЛАТЕЖНОЕ ПОРУЧЕНИЕ | № 1 ; Платежные поручения | ПЛАТЕЖНОЕ ПОРУЧЕНИЕ аванс | № 2
    no target match in Договор гарантии; sample: Договор гарантии | Договор гарантии | № OPP/2026/W/P/00831 ; Договор гарантии | Договор гарантии | № OPP/2026/W/P/00831
    no target match in График договора; sample: График договора | График погашения основного долга и вознаграждения | № OPP/2026/U/S/003722/0001L
    no target match in Решение собрания участников Заемщика; sample: *Решение собрания участников Заемщика | Решение | № -
    no target match in Анкета заявителя (гаранта, поручителя); sample: *Анкета заявителя (гаранта, поручителя) | Анкета-заявление ФЛ | № - ; *Анкета заявителя (гаранта, поручителя) | Анкета-заявление ТОО | № PTR.00032645
    no target match in Опись документов; sample: Опись документов | Опись | № 1-А ; Опись документов | Опись | № 1
    no target match in Иные документы; sample: Иные документы | Заявление | № - ; Иные документы | СОГЛАШЕНИЕ О ПРЯМОМ ДЕБЕТОВАНИИ БАНКОВСКОГО СЧЁТА заемщика | № - ; Иные документы | СОГЛАШЕНИЕ О ПРЯМОМ ДЕБЕТОВАНИИ БАНКОВСКОГО СЧЁТА гаранта | № -
    no target match in Договор страхования; sample: Договор страхования | Приложение №1 | № 3106 ; Договор страхования | ПЛАТЕЖНОЕ ПОРУЧЕНИЕ | № 3 ; Договор страхования | Приложение № 4 к Анкете-заявлению по страхованию банковских-лизинговых продуктов юридических лиц | № 3106
    no target match in Договор GPS; sample: Договор GPS | ПЛАТЕЖНОЕ ПОРУЧЕНИЕ | № 4 ; Договор GPS | Договор | № Pilot/AVTRANSCORPORATION/050826
    no target match in Коммерческое предложение; sample: *Коммерческое предложение | Коммерческое предложение | № -
    no target match in Уведомление; sample: Уведомление | Письмо об одобрении | № -
    selected 2 important numbered document(s)
    selected leasing_contract: Заявление о присоединении (Договор лизинга) №OPP/2026/U/S/003722 к Стандартным условиям предоставления финансового лизинга в АО «BCC Leasing» (Договор присоединения) (далее - Договор присоединения) | № OPP/2026/U/S/003722 (match score 150)
    saved 01_Заявление о присоединении_Договор лизинга_№_OPP_2026_U_S_003722_04.08.2026.pdf
    selected purchase_contract: ДОГОВОР купли-продажи товара для последующей передачи в финансовый лизинг | № 665/BL/04-08 (match score 150)
    saved 02_Договор купли-продажи_№_665_BL_04-08_04.08.2026.pdf
BIN 240540019483: ERROR: [Errno 2] No such file or directory: 'C:\\Users\\lznusenbaym\\Downloads\\240540019483\\Client_12155298_Товарищество с ограниченной ответственностью _AV TRANS CORPORATION_\\Dossier_2217408_OPP_2026_U_S_003722\\01_LEASE_OPP_2026_U_S_003722\\01_Заявление о присоединении_Договор лизинга_№_OPP_2026_U_S_003722_04.08.2026.pdf'
Download/grouping finished. Report: C:\Users\lznusenbaym\Downloads\download_log.xlsx
Starting Analyzer v35 production gate…
Analyzer v35: BIN 240540019483 — pair folders: 1; unmatched files: 0
  [240540019483] [1/2] Анализ: 01_Заявление о присоединении_Договор лизинга_№_OPP_2026_U_S_003722_04.08.2026.pdf
  [240540019483]   Совпадение подтверждено.
  [240540019483] [2/2] Анализ: 02_Договор купли-продажи_№_665_BL_04-08_04.08.2026.pdf
  [240540019483]   Совпадение подтверждено.
  [240540019483] Сохранён: АНАЛИЗ_01_______OPP_2026_U_S_003722_04.08.2026_6822c970_240540019483.xlsx
  [240540019483]   Production Gate: AUTO_APPROVED
  [240540019483] Сохранён: АНАЛИЗ_02__-____665_BL_04-08_04.08.2026_e07c3cab_240540019483.xlsx
  [240540019483]   Production Gate: AUTO_APPROVED
  [240540019483] Production Gate: AUTO_APPROVED=2; QUARANTINED=0.
  [240540019483] Манифест: production_manifest.xlsx
Integrated production manifest: C:\Users\lznusenbaym\Downloads\integrated_production_manifest.xlsx
Analysis finished: 2 Excel result(s); AUTO_APPROVED=2; QUARANTINED=0; UNMATCHED=0.

