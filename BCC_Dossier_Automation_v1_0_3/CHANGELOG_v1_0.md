# v1.0

- Объединены BCC Downloader v2.0 и Analyzer v35.0.
- Analyzer автоматически запускается после скачивания и DFL/DKP grouping.
- Анализ выполняется отдельно по каждому BIN/IIN tree с explicit BIN/IIN validation.
- `_REVIEW_UNMATCHED` исключён из автоматического анализа/production approval.
- Перед повторным запуском очищается старый Production Gate output для BIN/IIN, чтобы избежать stale approvals.
- Добавлен единый dashboard: lease folders, analyzed PDFs, AUTO_APPROVED, QUARANTINED, unmatched.
- Добавлен `integrated_production_manifest.xlsx`.
- Downloader regression: 29/29 tests passed.
- Integrated regression: 8/8 PDFs processed, 4 AUTO_APPROVED / 4 QUARANTINED, matching standalone v35 behavior.
