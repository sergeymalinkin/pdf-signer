# Выкладка «Примени Подпись» на VPS1

Схема: правки и тесты делаются вне сервера → вы утверждаете → версия попадает в `main` на GitHub → на VPS1 выполняется `deploy.sh deploy <коммит>`. Скрипт сам делает резервную копию, проверяет очередь, пробует миграцию на копии базы, ставит четыре файла, перезапускает сервис и проверяет health. Если сервис не поднялся, откатывает.

Меняются только `signer.py`, `max_api.py`, `max_workflow.py`, `max_bot.py`. Не затрагиваются: `config.local.json`, `private/` (env, CA, SQLite, jobs), изображение печати, Caddy, unit systemd.

## Однократная настройка (делает Claude Code на VPS1 по слову Сергея)

1. Ключ только для чтения этого репозитория, под root:
   `ssh-keygen -t ed25519 -N '' -C pdf-signer-deploy -f /root/.ssh/pdf_signer_deploy`
2. Показать Сергею публичный ключ (`/root/.ssh/pdf_signer_deploy.pub`). Он добавляет его на GitHub: репозиторий `pdf-signer` → Settings → Deploy keys → Add deploy key, **галочку Allow write access не ставить**.
3. Добавить хост GitHub в известные: `ssh-keyscan -t ed25519 github.com >> /root/.ssh/known_hosts` и сверить отпечаток с опубликованным на docs.github.com.
4. Клонировать и установить скрипт:
   `GIT_SSH_COMMAND='ssh -i /root/.ssh/pdf_signer_deploy -o IdentitiesOnly=yes' git clone git@github.com:sergeymalinkin/pdf-signer.git /home/devuser/autosign-src`
   `install -m 700 /home/devuser/autosign-src/deploy/deploy.sh /usr/local/sbin/pdf-signer-deploy`
5. Проверка без изменений: `pdf-signer-deploy status`.

Сам скрипт в `/usr/local/sbin` обновляется вручную тем же `install` и только по явной просьбе.

## Обычный выпуск

1. `pdf-signer-deploy status`: сервис active, health OK, нет заданий в работе.
2. `pdf-signer-deploy deploy <полный хеш коммита>`. Коммит обязан быть в `origin/main`.
3. Живая проверка в MAX: тестовая заявка в рабочую группу, затем «Подписать» и получение файла.
4. Что-то не так: `pdf-signer-deploy rollback`.

Что скрипт отказывается делать: выкладывать коммит вне `main`, перезапускать при заданиях CREATED / NOTIFYING / PROCESSING / SENDING или событиях BUSY, трогать конфигурацию и секреты. Секреты не печатает.

## Откат и база

Каждый релиз хранится в `/home/devuser/autosign-releases/<время>/`: старые четыре файла, `deployed-sha` и копия SQLite (`state.sqlite3`, права 700).

Откат возвращает файлы. Новая версия добавляет в `deliveries` колонки `status` и `updated`, а старый код вставляет в эту таблицу три значения. Поэтому при откате скрипт убирает лишние колонки и оставляет все строки: старый код блокирует повтор по любой строке, то есть безопаснее, чем нужно.

## Первый выпуск: P1 «статусы доставки»

При первом запуске новой версии таблица `deliveries` мигрирует автоматически, по состоянию задания:

| было | станет |
|---|---|
| задание SENT | CONFIRMED |
| задание ERROR или CANCELLED | запись удаляется: до отправки в группу публикации не было, документ можно подписать заново |
| задание PROCESSING | RESERVED (при старте освобождается) |
| DELIVERY_UNKNOWN, SENDING, задание не найдено | UNCERTAIN, остаётся заблокированным |

`deploy.sh` перед установкой показывает результат миграции на копии базы. Сверьте счётчики со `status` и только потом подтверждайте.
