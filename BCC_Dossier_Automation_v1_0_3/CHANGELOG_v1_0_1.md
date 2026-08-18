# v1.0.1

- Исправлена совместимость embedded Analyzer v35 со старыми версиями Python, используемыми на рабочих ПК.
- Убраны f-string выражения с escaped quotes/backslashes, которые вызывали `f-string expression part cannot include a backslash`.
- Исправлены оба найденных места в `folder_batch.py` и `safe_regression_fixes.py`.
- Downloader / DFL-DKP grouping logic не менялся.
- Embedded analyzer modules import successfully.
- Downloader regression: 29/29 tests passed.
