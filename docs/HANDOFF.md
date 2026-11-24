# CODEX 24.11.2026 — ТЗ8 source-кандидат, не production

В изолированной копии исправлен `MaxAPI.download`: при ошибке удаления касаются
только собственного файла текущей попытки. Чужой существующий destination
сохраняется. Нет реальных PDF, факсимиле, MAX credentials или вызовов MAX.

До правки 3 из 5 новых transport regressions FAIL; после 5 PASS на Windows и
отдельный store test PASS. Linux и GitHub CI подтверждаются в source Draft и
отчёте AI Team. Это узкая транспортная приёмка, не полный `docs/DOD.md`.
Client PDF/font, полный signer suite и live MAX остаются отдельными блокерами.
Production не заменялся и не перезапускался. При откате revert source-кандидата;
service/data restore не нужен. Сначала review Draft, не merge/deploy автоматически.
