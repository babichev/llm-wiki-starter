# Локальный инструмент Wiki

`wiki.py` использует только стандартную библиотеку Python 3.10+. Он ведёт источники, lease, индекс и резервные копии. Он не читает почту, не делает смысловой синтез, не подключает приложения, не создаёт расписания и не публикует GitHub.

Из папки комплекта:

```bash
python3 scripts/wiki.py init --vault /absolute/path/to/my-wiki
python3 scripts/wiki.py --vault /absolute/path/to/my-wiki status
```

Целевая папка должна находиться отдельно от комплекта. `init` копирует `vault/`, `skills/` в `.agents/skills/` и `scripts/wiki.py` в `system/scripts/wiki.py`. Существующие равные файлы пропускает, при любом конфликте отказывается до первого копирования. Чужие файлы не удаляет. Пути с symlink не принимает. На macOS используй полный реальный путь (например, `/private/tmp`, если `/tmp` — ссылка).

Внутри новой базы:

```bash
python3 system/scripts/wiki.py start-run --owner my-agent --ttl-minutes 60
```

Ответ содержит `run_id`. Подставь его вместо `RUN_ID`:

```bash
python3 system/scripts/wiki.py ingest --run RUN_ID --kind local --key example-source --title 'Пример' --content-file /absolute/path/to/source.txt --coverage full --access private --evidence-kind primary
python3 system/scripts/wiki.py mark --run RUN_ID --key example-source --expected-sha256 SOURCE_SHA256 --state processed --links wiki/topics/example.md
python3 system/scripts/wiki.py index --run RUN_ID
python3 system/scripts/wiki.py audit
python3 system/scripts/wiki.py finish-run --run RUN_ID
```

Подставь `SOURCE_SHA256` из ответа `ingest` именно на прочитанную версию. До `mark` агент сам читает источник и создаёт либо обновляет существующую каноническую страницу, затем перечитывает её. Не отмечай `processed` только по факту сохранения snapshot. `mark` требует `--expected-sha256`, сверяет его с текущей версией и проверяет наличие и хеш raw-снимка; старый SHA и отсутствующий либо повреждённый snapshot приводят к отказу. SHA защищает содержание; перед `mark` также перечитай текущие metadata, поскольку они могут измениться при том же тексте. Причину отметки можно сохранить через `--reason TEXT`. `--state needs-review` используется для неполного разбора; связь всё равно должна вести на существующий документ с вопросом. `mark` объединяет связи, не удаляет старые.

`ingest` принимает UTF-8 текст. Вместо `--content-file` можно передать `--content TEXT`; личные данные безопаснее не передавать в аргументах командной строки. Для версии сохраняется SHA-256; прежние снимки не перезаписываются. Повтор того же содержания с теми же metadata не меняет `processed` обратно на `pending`; новое содержание или изменение title, url, source_updated_at, coverage, evidence_kind, access ставит источник в `pending`. При одинаковом тексте обновление metadata добавляет запись версии с прежним raw-снимком, не создавая второй файл. Даже неизменный повтор отказывается от работы, если текущий raw-снимок отсутствует или повреждён; сначала восстанови его из проверенной копии. У каждой записи версии сохраняются дата получения, дата источника, coverage, evidence_kind, access, title и url.

Один и тот же стабильный `--key` используй для новых версий одного источника; его `--kind` неизменен. Укажи `--source-updated-at ISO_DATE`, если дата известна. Дата загрузки не заменяет дату события. `--access` принимает private (по умолчанию), confidential, internal или public; `--evidence-kind` — непустую категорию источника (по умолчанию unknown; например primary, participant-claim, ai-summary или analysis). Эти поля описывают происхождение и чувствительность, но не являются техническим контролем доступа.

Lease обязательна для изменения реестра и индекса. Срок можно продлить `renew-run --run RUN_ID --ttl-minutes 60`. Активную чужую и истёкшую lease инструмент не присваивает. Свою истёкшую lease можно завершить её токеном и начать новую. При аварии проверь владельца и отсутствие процесса, сохрани metadata; ручное удаление lock допустимо только после этой проверки и решения владельца базы. Инструмент не удаляет lock автоматически.

`audit` проверяет наличие основных YAML-ключей, дубли ID, цель относительной Markdown-ссылки или wikilink и просроченный `review_after`. Это базовая структурная проверка: она не разбирает весь YAML, не проверяет якоря заголовков, внешние URL или правдивость текста. Код возврата 1 — найдены ошибки; 2 — ошибка выполнения.

Индекс меняется только между `<!-- GENERATED:START -->` и `<!-- GENERATED:END -->` в `wiki/index.md`. YAML и текст снаружи сохраняются дословно.

## Резервная копия и проверенное восстановление

```bash
python3 system/scripts/wiki.py backup --destination /outside/vault/backups/snapshot-001 --run RUN_ID
python3 system/scripts/wiki.py verify-backup --snapshot /outside/vault/backups/snapshot-001 --manifest-sha256 RECEIPT_HASH
python3 system/scripts/wiki.py restore --snapshot /outside/vault/backups/snapshot-001 --target /empty/restore-check --manifest-sha256 RECEIPT_HASH
```

Папка snapshot не должна существовать и не может быть внутри базы либо её предком. При активной lease backup требует её токен; без активного writer токен необязателен. Инструмент сверяет чтение после копирования и при изменении исходных файлов оставляет незавершённую папку без manifest. Такая папка не считается копией. Повтор делай в новой папке.

Копия не шифруется. Она исключает `.git`, caches, symlink, активную lease, `.env` и несколько типовых credential-файлов; список пропусков есть в manifest. Секреты с произвольными именами эти эвристики не распознают: не храни их в базе и выбирай защищённый backup-каталог. Копия на том же диске не защищает от потери диска. Историю и удаление старых копий настраивает владелец; скрипт их не удаляет.

`manifest_sha256` сохрани из ответа backup отдельно от snapshot. Без него проверяются контрольные суммы файлов, но изменение manifest вместе с файлами не обнаруживается. Restore проверяет manifest, пути, отсутствие symlink, каждую сумму и readback; пишет только в новую или пустую папку. Активную базу он не заменяет. После восстановления запусти audit на копии и проверь несколько известных ответов; подключи аккаунты отдельно.

Проверки комплекта:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```
