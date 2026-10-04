#!/usr/bin/env bash
# Controlled release of "Примени Подпись" on VPS1: GitHub -> backup -> checks -> install -> restart -> health -> rollback.
#
#   deploy.sh status            read-only: service, queue by state, current release
#   deploy.sh deploy <commit>   full SHA (or unambiguous prefix) that is already on origin/main
#   deploy.sh rollback [id]     restore the previous (or given) release from autosign-releases
#
# Run as root on VPS1. Only the four code files are replaced. Never touched:
# config.local.json, private/ (env, CA bundle, SQLite, jobs), the print image, Caddy, the systemd unit.
set -euo pipefail

APP_DIR=${APP_DIR:-/home/devuser/autosign}
SRC_DIR=${SRC_DIR:-/home/devuser/autosign-src}
REL_DIR=${REL_DIR:-/home/devuser/autosign-releases}
SERVICE=${SERVICE:-primeni-podpis}
OWNER=${OWNER:-devuser}
REPO_URL=${REPO_URL:-git@github.com:sergeymalinkin/pdf-signer.git}
BRANCH=${BRANCH:-main}
DEPLOY_KEY=${DEPLOY_KEY:-/root/.ssh/pdf_signer_deploy}
PYTHON=${PYTHON:-$APP_DIR/.venv/bin/python}
DB=${DB:-$APP_DIR/private/max-state/state.sqlite3}
SYSTEMCTL=${SYSTEMCTL:-systemctl}
HEALTH_CMD=${HEALTH_CMD:-curl -fsS --max-time 3 http://127.0.0.1:8098/healthz}
SKIP_FETCH=${SKIP_FETCH:-0}   # tests only
FILES=(signer.py max_api.py max_workflow.py max_bot.py)

say() { printf '%s\n' "$*"; }
die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || [ "${ALLOW_NON_ROOT:-0}" = 1 ] || die "запускать от root"
mkdir -p "$REL_DIR"; chmod 700 "$REL_DIR"
exec 9>"$REL_DIR/.lock"
flock -n 9 || die "другой deploy уже выполняется"

queue_report() {  # exit 3 when something is in flight and a restart could lose it
  "$PYTHON" - "$1" <<'PY'
import sqlite3, sys
db = sqlite3.connect('file:' + sys.argv[1] + '?mode=ro', uri=True)
jobs = dict(db.execute('SELECT state, COUNT(*) FROM jobs GROUP BY state'))
events = dict(db.execute('SELECT state, COUNT(*) FROM events GROUP BY state'))
print('задания по статусам:', jobs or '-')
print('события по статусам:', events or '-')
busy = sum(jobs.get(s, 0) for s in ('CREATED', 'NOTIFYING', 'PROCESSING', 'SENDING')) + events.get('BUSY', 0)
if busy:
    print('В РАБОТЕ прямо сейчас:', busy)
    sys.exit(3)
PY
}

wait_healthy() {
  local i
  for i in $(seq 1 30); do
    if "$SYSTEMCTL" is-active --quiet "$SERVICE" && $HEALTH_CMD >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  return 1
}

current_sha() { cat "$APP_DIR/.deployed-sha" 2>/dev/null || echo unknown; }

restore_release() {  # $1 = release dir; service must be stopped
  local rel=$1 f
  for f in "${FILES[@]}"; do
    [ -f "$rel/code/$f" ] || die "в релизе $rel нет $f"
  done
  for f in "${FILES[@]}"; do install -o "$OWNER" -g "$OWNER" -m 644 "$rel/code/$f" "$APP_DIR/$f"; done
  cp -f "$rel/deployed-sha" "$APP_DIR/.deployed-sha" 2>/dev/null || rm -f "$APP_DIR/.deployed-sha"
  # Old code inserts 3 values into deliveries; drop the extra columns, keep every row (all stay blocked).
  if ! grep -q migrate_deliveries "$APP_DIR/max_workflow.py"; then
    "$PYTHON" - "$DB" <<'PY'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
if 'status' in [r[1] for r in db.execute('PRAGMA table_info(deliveries)')]:
    db.executescript('''BEGIN;
    CREATE TABLE deliveries_old(chat INTEGER, sha TEXT, job TEXT, PRIMARY KEY(chat, sha));
    INSERT INTO deliveries_old SELECT chat, sha, job FROM deliveries;
    DROP TABLE deliveries;
    ALTER TABLE deliveries_old RENAME TO deliveries;
    COMMIT;''')
PY
    chown "$OWNER:$OWNER" "$DB"
  fi
}

do_rollback() {
  local rel
  if [ -n "${1:-}" ]; then rel="$REL_DIR/$1"; else rel=$(ls -1d "$REL_DIR"/2* 2>/dev/null | tail -n 1); fi
  [ -n "$rel" ] && [ -d "$rel" ] || die "релиз для отката не найден"
  say "Откат к релизу: $(basename "$rel") (версия $(cat "$rel/deployed-sha" 2>/dev/null || echo неизвестна))"
  "$SYSTEMCTL" stop "$SERVICE"
  restore_release "$rel"
  "$SYSTEMCTL" start "$SERVICE"
  wait_healthy || die "после отката сервис не отвечает: проверьте journalctl -u $SERVICE"
  say "Откат выполнен, сервис работает."
}

cmd=${1:-status}
case "$cmd" in
status)
  say "Версия на сервере: $(current_sha)"
  say "Сервис: $("$SYSTEMCTL" is-active "$SERVICE" || true)"
  $HEALTH_CMD >/dev/null 2>&1 && say "health: OK" || say "health: НЕ ОТВЕЧАЕТ"
  queue_report "$DB" || say "Перезапускать сейчас нельзя."
  ;;
rollback)
  do_rollback "${2:-}"
  ;;
deploy)
  want=${2:-}
  [[ "$want" =~ ^[0-9a-f]{7,40}$ ]] || die "укажите хеш коммита: deploy.sh deploy <commit>"
  export GIT_SSH_COMMAND="ssh -i $DEPLOY_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes"
  if [ "$SKIP_FETCH" != 1 ]; then
    [ -d "$SRC_DIR/.git" ] || git clone -q "$REPO_URL" "$SRC_DIR"
    git -C "$SRC_DIR" fetch -q origin "$BRANCH"
  fi
  sha=$(git -C "$SRC_DIR" rev-parse --verify "$want^{commit}") || die "коммит $want не найден"
  git -C "$SRC_DIR" merge-base --is-ancestor "$sha" "origin/$BRANCH" || die "коммит $sha не входит в origin/$BRANCH"
  git -C "$SRC_DIR" checkout -q --detach "$sha"
  say "Выкладываю версию $sha (сейчас на сервере: $(current_sha))"

  for f in "${FILES[@]}"; do [ -f "$SRC_DIR/$f" ] || die "в коммите нет $f"; done
  "$PYTHON" -c 'import ast,sys; [ast.parse(open(f,encoding="utf-8").read(),f) for f in sys.argv[1:]]' \
    $(printf "$SRC_DIR/%s " "${FILES[@]}") || die "ошибка синтаксиса"

  "$SYSTEMCTL" is-active --quiet "$SERVICE" || die "сервис сейчас не активен: сначала разберитесь с ним (status)"
  queue_report "$DB" || die "в очереди есть задания в работе. Подождите и повторите. Ничего не изменено."

  rel="$REL_DIR/$(date +%Y%m%d-%H%M%S)"
  mkdir -m 700 "$rel" "$rel/code"
  for f in "${FILES[@]}"; do cp -p "$APP_DIR/$f" "$rel/code/$f"; done
  current_sha > "$rel/deployed-sha"
  "$PYTHON" - "$DB" "$rel/state.sqlite3" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2])
src.backup(dst); dst.close(); src.close()
PY
  say "Резервная копия: $rel (файлы + база)"

  # Dry-run of the new code's migration on a COPY of the database.
  cp "$rel/state.sqlite3" "$rel/migration-check.sqlite3"
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$SRC_DIR" "$PYTHON" - "$rel/migration-check.sqlite3" <<'PY' || die "проверка миграции на копии базы не прошла. Ничего не изменено."
import sqlite3, sys
from max_workflow import Store
Store(sys.argv[1])
rows = sqlite3.connect(sys.argv[1]).execute('SELECT status, COUNT(*) FROM deliveries GROUP BY status').fetchall()
print('после миграции (копия):', dict(rows) or '-')
PY
  rm -f "$rel/migration-check.sqlite3"

  for f in "${FILES[@]}"; do install -o "$OWNER" -g "$OWNER" -m 644 "$SRC_DIR/$f" "$APP_DIR/$f"; done
  printf '%s\n' "$sha" > "$APP_DIR/.deployed-sha"; chown "$OWNER:$OWNER" "$APP_DIR/.deployed-sha"
  "$SYSTEMCTL" restart "$SERVICE"
  if wait_healthy; then
    say "ГОТОВО: версия $sha работает. Релиз для отката: $(basename "$rel")"
    say "Теперь живая проверка в MAX: отправьте тестовую заявку в рабочую группу."
    say "Если что-то не так:  deploy.sh rollback"
  else
    say "Сервис не поднялся после обновления, откатываю автоматически."
    "$SYSTEMCTL" stop "$SERVICE" || true
    restore_release "$rel"
    "$SYSTEMCTL" start "$SERVICE"
    wait_healthy || die "ОТКАТ НЕ ПОМОГ, нужен ручной разбор: journalctl -u $SERVICE -n 50"
    die "новая версия не запустилась, восстановлена предыдущая ($(current_sha))"
  fi
  ;;
*) die "команды: status | deploy <commit> | rollback [id]" ;;
esac
