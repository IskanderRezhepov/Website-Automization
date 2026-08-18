# v1.0.3

- Исправлена оставшаяся ошибка Windows MAX_PATH на этапе download/grouping.
- Укорочена структура `BIN / customer / dossier / lease pair`.
- Файлы внутри пар переименовываются в компактные `01_DFL`, `01_LEASE`, `02_DKP`.
- Добавлен динамический бюджет длины полного пути.
- Полные исходные реквизиты остаются в Excel-журналах для аудита.
- Downloader pairing и Analyzer v35 production gate сохранены без изменения бизнес-правил.
