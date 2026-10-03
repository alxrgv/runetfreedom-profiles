## RunetFreedom GeoData Updater

Репозиторий генерирует готовые routing-конфигурации для HAPP и INCY, через которые клиенты получают актуальные файлы `geoip.dat` и `geosite.dat` из проекта `runetfreedom`, а также отдельные ruleset-файлы для клиентов на основе Mihomo.

При каждом запуске workflow получает метаданные последнего GitHub Release upstream-проекта и записывает в профили прямые ссылки на его `geoip.dat` и `geosite.dat`. Поэтому опубликованный профиль закреплён за конкретным выпуском, а не за изменяемой веткой `release`; при новом выпуске обновляются ссылки и `LastUpdated`.

## Pipeline

```text
prepare-geodata
    ├── build-xray   (HAPP + INCY)
    └── build-mihomo (MRS + classical extras)
             ↓
          publish (результаты успешных сборок)
```

`prepare-geodata` получает последний стабильный выпуск `runetfreedom/russia-v2ray-rules-dat`, скачивает оба `.dat` и проверяет их непустоту и официальные SHA-256. Данные передаются через один Actions artifact; оба build-job повторно проверяют его контрольные суммы и используют именно эти файлы. Они не скачивают GeoData независимо.

`build-xray` сохраняет существующие пути HAPP/INCY, template, названия профилей, JSON-поля и deeplink-схемы. `LastUpdated` повышается только при изменении данных, ссылок выпуска или ручном `force_refresh`. Тот же timestamp используется в обоих профилях.

`geodata.lock.json` хранит repository, release tag, URL и SHA-256 данных опубликованных HAPP/INCY. Подготовка создаёт кандидата lock только внутри artifact. `publish` дожидается завершения обеих сборок и запускается, если хотя бы одна успешна и workflow не отменён. Он скачивает и публикует только результаты успешных сборок: ошибка Mihomo не блокирует HAPP/INCY, ошибка HAPP/INCY не блокирует Mihomo. GeoData lock обновляется только вместе с HAPP/INCY. При частичном успехе профили и Mihomo могут использовать разные GeoData releases до следующей успешной сборки. Если обе сборки неуспешны, публикация пропускается.

Один publisher записывает доступные результаты одним коммитом, поэтому параллельные сборки не конкурируют за push. Workflow сериализован через concurrency; если ветка изменилась во время сборки, публикация завершается ошибкой и требует нового запуска. Force push и перенос устаревших результатов через rebase не используются.

Расписание и публикация в ту же ветку сохранены. Изменения generated-файлов не вызывают рекурсивный workflow. Для простоты все категории пересобираются при каждом запуске, включая изменения config/tool pins при неизменных `.dat`.

Вся Bash-оркестрация находится в `.github/workflows/update-geodata.yml`. YAML anchors объявлены при первом использовании внутри jobs: checkout, setup-python, получение shared artifact и установка Bash helpers. Последующие jobs переиспользуют целые шаги через aliases. Файл helpers создаётся только во время job и загружается через `BASH_ENV`; отдельных `.sh` в репозитории нет. Python выполняет post-processing, parity и запись file hashes в lock, а маленький Go audit читает source через upstream protobuf-типы: converter сам не предоставляет строгую проверку неизвестных selectors.

## Mihomo

Списки категорий хранятся в `config/mihomo-rulesets.json`: 11 geosite-категорий из пользовательской Xray-конфигурации и `geoip:private`. Workflow читает массивы через `jq` и передаёт Python-генератору через `--geosite` / `--geoip`. Сам Python не зависит от config-файла: для локального запуска можно передать CSV (`reddit,steam,category-ru`) или JSON array (`["reddit","steam","category-ru"]`). Значение `[]` отключает соответствующий список; хотя бы один список должен быть непустым. Добавление/удаление категории не требует изменений build-кода. Поддерживаются положительные attribute-категории вроде `google@cn`; `include` уже разрешён upstream-сборкой `.dat`. Неизвестные, пустые или непредставимые категории завершают сборку с ошибкой.

Pinned repository revisions, Mihomo version и Go version заданы в блоке `env` файла `.github/workflows/update-geodata.yml`. CI собирает [MetaCubeX/meta-rules-converter](https://github.com/MetaCubeX/meta-rules-converter) и Mihomo из исходников. Готовый бинарник converter не скачивается. Небольшой `geodata-audit.go` читает выбранные записи через готовые protobuf-типы Mihomo для проверки полноты; конвертацию выполняет upstream converter. Собственного protobuf-парсера нет.

Версии можно менять через GitHub UI: **Settings → Secrets and variables → Actions → Variables → New repository variable**. Поддерживаются `GO_VERSION`, `MIHOMO_VERSION`, `MIHOMO_REVISION`, `CONVERTER_REVISION`, а также `MIHOMO_REPOSITORY` и `CONVERTER_REPOSITORY`. Workflow использует `${{ vars.NAME || 'default' }}`: если переменной нет или она пустая, действует значение из YAML. Для обновления Mihomo задайте version и соответствующий полный commit SHA вместе; revisions converter и Mihomo проверяются перед сборкой. После изменения Variables запустите workflow вручную или дождитесь расписания.

Результаты:

- `MIHOMO/geosite/<category>.mrs` — `behavior: domain`, `format: mrs`: full → exact domain, domain → root + suffix;
- `MIHOMO/geoip/<category>.mrs` — `behavior: ipcidr`, `format: mrs`: IPv4 + IPv6;
- `MIHOMO/classical/<category>-extra.list` — для каждой выбранной geosite-категории: только DOMAIN-KEYWORD и DOMAIN-REGEX, `behavior: classical`, `format: text`;
- `MIHOMO/rule-providers.yaml` — готовые HTTP providers со стабильными URL;
- `MIHOMO/mihomo.lock.json` — только размер и SHA-256 каждого generated-файла (кроме самого lock): `geosite:<category>`, `geoip:<category>`, `geosite:<category>:extra` и `rule-providers.yaml`. Суффикс `:extra` обозначает classical-список из той же geosite-категории и не пересекается с именами категорий. Клиенту этот файл не нужен; CI использует его для проверки artifacts и сравнения размеров.

Extra-файл и provider публикуются для каждой выбранной geosite-категории, даже если keyword/regexp отсутствуют. В таком случае файл содержит только комментарий и не совпадает ни с одним запросом. Сейчас правила есть только в `epicgames-extra.list`. При появлении или удалении keyword/regexp в следующем upstream-выпуске содержимое обновится автоматически; путь и имя provider останутся прежними. GeoIP extra-файлов не требует. Если geosite-категория содержит только keyword/regexp, публикуется только extra-provider: пустой domain MRS не создаётся. [MRS поддерживает domain и ipcidr](https://wiki.metacubex.one/en/config/rule-providers/); keyword/regexp сохраняются отдельно и не дублируют DOMAIN/DOMAIN-SUFFIX.

Проверки сравнивают исходные selector counts и значения с текстовыми экспортами, затем декодируют MRS реальным Mihomo и сравнивают покрытие доменов и IP. Дедупликация и объединение подсетей допустимы только при эквивалентной семантике. Неизвестные selector types/fields, inverse GeoIP, неверные regex и непредставимые значения (например, разделитель `,` или literal wildcard в full/domain) отклоняются. Все providers включаются в минимальный локальный config, проверяемый `mihomo -t`. Данные публикуются только после успешных проверок.

Это три разных проверки: декодирование проверяет читаемость MRS данным core; parity проверяет сохранение правил; `-t` проверяет конфигурацию providers и ссылки RULE-SET. В pinned Mihomo `-t` не вызывает загрузку provider-файлов, поэтому не заменяет декодирование. Эти проверки не измеряют память iOS и не тестируют доступность сайтов через реальные прокси.

Actions summary показывает размеры каждого файла и общий объём. При росте отдельного файла минимум в 5 раз или общего объёма минимум в 3 раза относительно опубликованного lock выводится warning. Lock не содержит timestamps, source metadata или tool revisions: одинаковые файлы всегда дают одинаковый lock. Результаты parity остаются в логах сборки.

Скопируйте `rule-providers` из `MIHOMO/rule-providers.yaml` в клиентский config. Файл использует JSON-синтаксис, допустимый в YAML. Пример правил:

```yaml
rules:
  - RULE-SET,geoip-private,DIRECT,no-resolve
  - RULE-SET,geosite-reddit,DIRECT
  - RULE-SET,geosite-reddit-extra,DIRECT
  - RULE-SET,geosite-antifilter-download-community,PROXY
  - RULE-SET,geosite-antifilter-download-community-extra,PROXY
  - RULE-SET,geosite-epicgames,DIRECT
  - RULE-SET,geosite-epicgames-extra,DIRECT
```

Основной и extra-provider одной категории должны вести в одну группу. `PROXY` замените именем группы клиента. Это только пример использования: порядок routing, DNS и пользовательские домены задаются в клиенте. Для этих providers клиенту не нужны монолитные `.dat`, `geox-url`, `geodata-mode` или geo-auto-update. Провайдеры обновляются с интервалом 21600 секунд.

Каталоги `MIHOMO/geosite`, `MIHOMO/geoip`, `MIHOMO/classical` содержат только generated-файлы. При удалении категории её artifacts удаляются при следующей успешной публикации.

## Local verification

Нужны Python 3.10+, Bash, jq и Go версии из блока `env` workflow (с учётом выбранных overrides). Python-зависимости устанавливать не требуется. Команды сборки converter/core находятся в шаге `Build converter, source audit, and Mihomo from pinned source` workflow. После сборки этих tools из pinned revisions передайте использованные pins генератору:

```sh
python scripts/generate-mihomo-rulesets.py \
  --geosite "reddit,steam,category-ru" \
  --geoip "private" \
  --geosite-dat /path/to/verified/geosite.dat \
  --geoip-dat /path/to/verified/geoip.dat \
  --converter .build/tools/converter \
  --audit .build/tools/geodata-audit \
  --mihomo .build/tools/mihomo \
  --mihomo-version v1.19.17 \
  --base-url https://raw.githubusercontent.com/alxrgv/runetfreedom-profiles/main/MIHOMO
python -m unittest discover -s tests
```

При локальной генерации `.dat` необходимо проверить заранее: authoritative checksum/download validation выполняет job `prepare-geodata`. Downstream и publish проверяют shared artifact через `verify-geodata.py`; `verify-mihomo-lock.py` проверяет финальные размеры, hashes и отсутствие лишних или пропущенных generated-файлов.
