# Agent Hypervisor

TUI-оболочка для независимых Qwen Code-агентов в task group. Каждый агент занимает отдельный терминал.
Жизненным циклом управляет `tmux`: закрытие Hypervisor не останавливает агентов, а новый запуск
автоматически находит уже существующие группы.

## Быстрый старт

Требуются Python 3.14 (устанавливается через `uv`), Textual 8+, tmux 3.2+, Git и
Node.js 22+ с npm. Qwen Code запускается через
`npm exec --yes --package=@qwen-code/qwen-code -- qwen`, поэтому глобально добавлять `qwen`
в `PATH` необязательно.

На Debian/Ubuntu tmux и Git можно установить через `apt install tmux git`.
Python проекта управляется `uv`; системный Python менять не нужно.

```bash
uv sync
uv run hypervisor
```

В интерфейсе:

- `F9` — новая группа, `F10` — переименовать;
- `F6` — активировать или деактивировать выбранную группу;
- `Alt+1` … `Alt+9` — выбрать агента по номеру панели, `F3` / `F4` — предыдущий / следующий;
- колесо мыши — листать встроенную историю Qwen выбранного терминала;
- `Alt+↑` / `Alt+↓` — переключить группу;
- `F5` — развернуть терминал / вернуться к сетке;
- `F11` / `Ctrl+Shift+C` — скопировать выделенный текст;
- `F7` — запустить заново, `F8` — остановить, `F12` — закрыть только TUI.

Start/Stop и переключатель режима находятся в одной строке с названием и статусом терминала.
Roadmap внутри шапки группы занимает всю её ширину: каждый участок между
точками — этап. Ожидающий путь белый, завершённый — зелёный сплошной, текущий — зелёный
пунктирный. Наведение показывает название и состояние; нажатие открывает детали этапа.
Метка `◈` обозначает ручное подтверждение. После выбора дорожки стрелки `←` / `→`
переключают этапы, `Enter` открывает детали. Длина пути подстраивается под размер окна.

Рядом со Start/Stop каждого выбранного агента находится переключатель **◇ / ◆**.
**◆ и подпись MANUAL** означают ручной режим: новые автоматические промпты этому агенту
не поступают, сессия и текущая работа продолжаются. Ожидающее задание сохраняется в журнале,
roadmap показывает `Manual control`. Повторное нажатие возвращает агента в workflow и доставляет
отложенное задание без новой попытки этапа. Другие агенты сохраняют свои режимы.
Перед возвратом отправьте или очистите незавершённый промпт: переключатель не меняет черновик.
Уже отправленные в Qwen задания не отменяются. Режим сохраняется после закрытия TUI.

То же управление без интерфейса:

```bash
uv run hypervisor mode TASK-123 developer manual
uv run hypervisor mode TASK-123 developer workflow
```

Автоматические вызовы `send` также учитывают ручной режим; прямой ввод клавиш пользователем
остаётся доступен. Для групп без настроенного workflow переключатель не нужен и недоступен.

Текст в терминалах и подписях Hypervisor можно выделять перетаскиванием мыши.
При отпускании кнопки мыши выделение копируется автоматически. Выделенная терминальная панель
сохраняет снимок до снятия выделения кликом, чтобы новый вывод не менял читаемый текст.
Для системного буфера используются доступные `wl-copy`, `xclip`, `xsel` или KDE Klipper;
дополнительно Textual отправляет OSC 52 терминалу. `Ctrl+C` в терминале продолжает работать в Qwen.

Во всех модальных окнах стрелки перемещают фокус между вариантами, `Enter` выбирает,
`Esc` закрывает окно. В полях ввода `←` и `→` продолжают перемещать курсор.

Клавиши `Tab`, `Shift+Tab`, `Ctrl+Q`, `Ctrl+R`, `Ctrl+S`, `Ctrl+X` и остальные сочетания
Qwen Code не перехватываются оболочкой. Например, `Ctrl+Q` продолжает добавлять промпт во
встроенную очередь Qwen, а `Shift+Tab` переключает approval mode.

Терминал можно выбрать кликом и сразу печатать в нем; `Enter`, Backspace, стрелки и управляющие
клавиши также передаются в tmux. Если терминал остановлен или Qwen завершился, выберите его и нажмите
`Start`: появится выбор между новой сессией и подходящими сессиями, которые Hypervisor ранее
запускал именно в этом терминале. Экран каждого терминала берется
через `tmux capture-pane`, поэтому Hypervisor не реализует собственный эмулятор терминала.
Qwen использует собственную Virtualized History, поэтому колесо преобразуется в его нативные
`Shift+↑` / `Shift+↓`. `PgUp`, `PgDn` и `Ctrl+Home` / `Ctrl+End` можно также вводить напрямую.

## CLI

```bash
uv run hypervisor check-config
uv run hypervisor create 123 --type feature --request "Описать задачу и реализовать изменения"
uv run hypervisor create PROJ-123 --flow intent-implementation \
  --request "Добавить экспорт" --goal "Реализовать экспорт по требованиям" \
  --source openspec/intent.md --constraint "Сохранить обратную совместимость"
uv run hypervisor list
uv run hypervisor status 123
uv run hypervisor activate 123
uv run hypervisor deactivate 123
uv run hypervisor sessions 123 1
uv run hypervisor send 123 1 "Проверь документацию"
uv run hypervisor send 123 developer "Проверь реализацию"
uv run hypervisor key 123 1 ctrl-q
uv run hypervisor stop 123 1
uv run hypervisor start 123 1 --session SESSION_UUID
uv run hypervisor capture 123 1
uv run hypervisor rename 123 docs-123
uv run hypervisor remove docs-123
uv run hypervisor remove docs-123 --remove-worktree
```

`send` добавляет промпт в очередь Qwen через `Ctrl+Q`. Терминалы можно запускать параллельно
вручную; workflow при этом последовательно передаёт задание следующему этапу только после
проверок и ревью. Создание группы не запускает workflow. Чтобы начать его сразу при создании,
добавьте `--start-workflow`; для уже созданной группы используйте явную команду `workflow start`.

Workflow-команды:

```bash
uv run hypervisor workflow status PROJ-123
uv run hypervisor workflow events PROJ-123
uv run hypervisor workflow start PROJ-123
uv run hypervisor workflow resume PROJ-123
uv run hypervisor workflow report PROJ-123 reports/ATTEMPT-review.json
```

Операции исполнителя, независимого ревью и ручного подтверждения доступны из CLI и могут
вызываться пользователем из TUI. `workflow events` показывает события, в том числе текущие
`attempt` и UUID сессии из `stage.started` или `review.started`. Их нужно передать в callback:

```bash
uv run hypervisor --json stage complete PROJ-123 implementation \
  --attempt ATTEMPT_FROM_EVENTS --session EXECUTOR_SESSION_FROM_EVENTS \
  --summary "Изменения реализованы; добавлен tests/test_export.py"

uv run hypervisor --json review submit PROJ-123 implementation \
  --attempt ATTEMPT_FROM_EVENTS --session REVIEWER_SESSION_FROM_EVENTS \
  --decision approved --summary "Изменения соответствуют intent"

uv run hypervisor --json human approve PROJ-123 implementation --attempt ATTEMPT_FROM_EVENTS
uv run hypervisor --json human reject PROJ-123 implementation \
  --attempt ATTEMPT_FROM_EVENTS --comment "Исправить обработку пустого файла"
```

Сгенерированные задания исполнителю и ревьюеру содержат соответствующую callback-команду.
Команда завершения — только заявка: Hypervisor выполняет hard gates, затем запускает отдельное
ревью. Для каждого этапа создаётся новая UUID-сессия ревьюера, даже если тот же агент был
исполнителем раньше. Терминальные окна и их история сохраняются при смене роли, поэтому работающий
агент не завершается; окна остаются до удаления группы.

Если доставка промпта оборвалась между записью события и отправкой, Hypervisor не повторяет её
молча: сначала осмотрите терминал и `workflow events`, затем при необходимости явно повторите
команду через `workflow resume PROJ-123 --retry-delivery`.

Диагностика TUI, включая события прокрутки:

```bash
uv run hypervisor logs --lines 200
tail -f ~/.local/state/agent-hypervisor/hypervisor.log
```

Лог ротируется и ограничен 1 МБ на файл; хранятся не более трёх файлов.

Для автоматизации доступен флаг `--json`. Команду агента можно изменить переменной
`HYPERVISOR_AGENT_COMMAND` или опцией `create --agent-command`. Команда должна поддерживать
Qwen-совместимые аргументы `--session-id`, `--resume` и `--chat-recording`. Это позволит позднее
заменить Qwen Code на совместимый GigaCode CLI без изменения управления сессиями. Например:

```bash
HYPERVISOR_AGENT_COMMAND="gigacode" uv run hypervisor create demo --request "Проверить запуск GigaCode"
```

## Группы, worktree и сессии

При создании группы Hypervisor:

1. находит корень текущего Git-репозитория;
2. создаёт скрытый соседний worktree `.<repository>-worktrees/<group>` и ветку
   `<тип>/<код задачи>`, например `fix/PROJ-123`;
3. резервирует новый UUID Qwen-сессии для каждого агента из YAML;
4. оставляет терминалы неактивными до нажатия **Activate group**.

Если для группы выбран flow, его снимок и исходный запрос записываются в `events.jsonl` в
worktree. Сам факт создания не стартует этап: начало запускается через `workflow start` или
флаг `create --start-workflow`. Workflow выполняет этапы по порядку; ручной запуск разных
терминалов возможен независимо от этой последовательности.

При первом запуске сессии встроенная команда Qwen `/rename` задаёт имя
`<agent-id>-<8 hex символов UUID>`, например `developer-a3f02c91`.
Это имя показывается и в списке восстановления Hypervisor. При resume имя Qwen не меняется.

Неактивная группа не имеет tmux-сессии и не запускает Qwen-процессы. Активация запускает все
терминала внутри worktree; деактивация завершает всю tmux-сессию, сохраняя worktree и реестр UUID.

Если имя пути или ветки занято, добавляется суффикс `-2`, `-3` и так далее. При удалении группы
TUI сначала просит подтверждение, затем отдельно предлагает сохранить или удалить worktree.
Worktree с незакоммиченными изменениями автоматически не удаляется. Git-ветка сохраняется в любом
случае.

Реестр групп и UUID хранится атомарно в
`~/.local/state/agent-hypervisor/groups.json`. Путь можно переопределить переменной
`HYPERVISOR_STATE_PATH`.

В TUI создание состоит из ввода кода задачи и выбора типа ветки: `feature`, `fix`, `refactor`,
`docs`, `test`, `chore` или собственный тип. В CLI тип задаётся через `create --type`,
по умолчанию `feature`. Тип — строчные латинские буквы, цифры и дефисы.

Группы представлены tmux-сессиями с префиксом `agent-hypervisor-`, а терминалы —
внутренними окнами `terminal-1` … `terminal-N`. Вся логика tmux находится в одном backend-классе и
используется CLI и TUI, поэтому поверх нее можно позже добавить MCP без переработки интерфейсов.
Контракт `HypervisorBackend` отделён от реализации `TmuxBackend`, поэтому TUI можно тестировать без tmux,
а новые адаптеры не требуют изменения UI.

## Агенты и workflow в config.yaml

Подписи, обязательность и начальные значения четырёх полей формы задаются в `task-form`:

```yaml
task-form:
  request:
    label: Запрос задачи
    required: true
    default: ""
  goal:
    label: Общая цель
    required: false
    default: ""
  sources:
    label: Источники (через запятую)
    required: false
    default: [openspec/intent.md]
  constraints:
    label: Ограничения
    required: false
    default: Не менять публичный API без необходимости.
```

Внутренние ключи `request`, `goal`, `sources`, `constraints` фиксированы; меняются их подписи,
обязательность, видимость (`visible`, по умолчанию `true`) и defaults. Для `sources` default — список строк, для остальных — строка.
В форме обязательные поля отмечены `*`. Проверка одинакова в TUI и CLI и выполняется до создания
worktree. В CLI пропущенный параметр берёт значение из YAML; явно заданная пустая строка очищает
необязательное поле. Без `task-form` остаются стандартные подписи и пустые необязательные поля.

Фактические значения и снимок настроек формы сохраняются в `task.created` в
`<worktree>/.hypervisor/events.jsonl`. Пустая общая цель заполняется исходным запросом.
Запрос, цель, источники и ограничения включаются в задания исполнителя и ревьюера; источники
передаются как ссылки/пути, содержимое файлов автоматически не прикладывается. Задания поступают
в Qwen через очередь Ctrl+Q. Изменения YAML действуют на новые группы.

Для конкретного workflow задайте `task-form` внутри его записи в `flows`:

```yaml
flows:
  - id: documentation
    name: Documentation
    task-form:
      request:
        label: Какие документы обновить?
      goal:
        visible: false
        default: Обновить документацию по запросу.
      sources:
        default: [README.md]
      constraints:
        visible: false
        default: Изменять только документацию.
    steps: # обычные этапы workflow
```

Настройки наследуются по отдельным свойствам: стандартные → общий `task-form` → `flows[].task-form`.
После выбора flow TUI показывает его форму; CLI использует те же defaults и проверки через `--flow`.
Скрытые поля не отображаются, но их defaults попадают в начальный контекст. Скрытое обязательное поле
должно иметь непустой default. Форма только собирает ввод: порядок работы, цели этапов и проверки
по-прежнему задаются в `steps`.

Рабочий пример с двумя разными формами:

```bash
uv run hypervisor --config examples/workflow-forms.yaml check-config
uv run hypervisor --config examples/workflow-forms.yaml
```

По умолчанию читается `config.yaml` в текущем каталоге. Другой путь:

```bash
uv run hypervisor --config /path/to/config.yaml check-config
uv run hypervisor --config /path/to/config.yaml
```

Также поддерживается `HYPERVISOR_CONFIG`. Если файла по умолчанию нет, доступны три обычных
терминала. Явно заданный отсутствующий или неверный файл вызывает ошибку.

Пример в корне репозитория содержит Spec Writer, Developer и Architect, описания — в `examples/agents/`.
Минимальный пример v2 с именованным flow:

```yaml
version: 2
agents-dir: ./agents
data-dir: .hypervisor
agents:
  - agent-id: developer
    agent-name: Developer
  - agent-id: architect
    agent-name: Architect
flows:
  - id: implementation
    name: Реализация
    steps:
      - id: implement
        agent-id: developer
        goal: Реализовать согласованные изменения и запустить тесты.
        reviewer-id: architect
        review-goal: Проверить результат по требованиям и найти конкретные дефекты.
        review-scope: ["src/**", "tests/**"]
        checks:
          - id: unit-tests
            command: [python, -m, unittest]
            timeout: 300
        auto-commit: false
        create-pr: false
        human-approval: false
```

Для каждого `agent-id` требуется `<agents-dir>/<agent-id>.md`. Пути разрешаются относительно
YAML-файла. Markdown может иметь YAML frontmatter в стиле Qwen; системные инструкции берутся
из тела документа. `model` передаётся Qwen флагом `--model`, `null` оставляет модель по умолчанию.
Описание подключается через `--append-system-prompt`: каждый терминал — самостоятельный Qwen CLI,
а не вызов встроенного инструмента субагентов. Поля frontmatter вроде `tools` или `executor`
не настраивают этот отдельный процесс.

`agent-id` уникален и используется для ссылок, CLI-команд и имени сессии. `agent-name` показывается
в панели. Роли и цели назначаются в каждом этапе через `agent-id`, `reviewer-id`, `goal` и
`review-goal`; у агента нет настроек ревью или PR. У flow есть `id`, `name` и непустой список
этапов. Идентификаторы проверяются на уникальность, ссылки — на наличие соответствующих агентов.

`checks` — список hard gates. `command` — argv-массив, который запускается напрямую, без shell;
если нужен shell, укажите его явно, например `[bash, -lc, "python -m unittest"]`. Таймаут
задаётся положительным целым числом в секундах, по умолчанию 300. Проверки запускаются из корня
worktree. `review-scope` — непустой список относительных путей или glob-шаблонов без `..`.

При `auto-commit: false` исполнитель сам создаёт коммит перед отправкой этапа; проверки требуют
чистый worktree и существующий HEAD. При `true` Hypervisor коммитит после успешных gates. После
любого ревью проверяются чистота worktree и неизменность HEAD. Если ревьюер изменил файлы или
HEAD, этап блокируется для решения человека; изменения автоматически не удаляются.
`create-pr: true` только записывает запрос со статусом `not_integrated`; PR пока не создаётся и
этот флаг никогда не блокирует workflow. `human-approval: true` отдельно приостанавливает этап до
`human approve` или возврата на доработку через `human reject`.

Workflow хранит источник истины состояния в `.hypervisor/events.jsonl` (или в относительном
`data-dir` из конфигурации). Отчёты hard gates и ревью хранятся рядом в `reports/`, а события
ссылаются на них. Hypervisor добавляет каталог данных в локальный Git `info/exclude`, не изменяя
файлы проекта. Общий реестр групп и снимки сессий остаются в `~/.local/state/agent-hypervisor/`;
он помогает найти worktree и восстановить терминалы, но не заменяет журнал workflow. Данные
workflow и отчёты находятся в worktree своей группы.

Flow и агенты фиксируются в группе при создании. Изменения YAML применяются к новым группам;
старые конфиги версии 1 загружаются через слой миграции для обратной совместимости. Без выбранного
flow группа остаётся в ручном режиме. При нескольких flows укажите `create --flow`.

В TUI можно смотреть roadmap и продолжать работу через prompt callbacks: выбор этапа и передача
задания остаются последовательными, а callbacks CLI доступны агентам и пользователю. Состояние
roadmap вычисляется из `events.jsonl`; закрытие TUI не останавливает workflow.

Число панелей равно числу агентов; сетка подстраивается, при нехватке высоты появляется прокрутка.
Группа сохраняет снимок агентов, моделей, инструкций и flow. Изменения YAML применяются к новым
группам; существующие сохраняют прежние связи и историю.

## Проверка

```bash
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q src tests
```

Целевые тесты конфигурации и CLI workflow можно запускать отдельно:

```bash
uv run python -m unittest discover -s tests -p 'test_project_config.py' -v
uv run python -m unittest discover -s tests -p 'test_workflow_cli.py' -v
```

Интеграционный тест запускается на отдельном временном tmux-сервере:

```bash
HYPERVISOR_TEST_TMUX=/usr/bin/tmux uv run python -m unittest tests.test_tmux_integration -v
```

## Настройка интерфейса

- `src/agent_hypervisor/config.py` — действия, хоткеи, иконки, подсказки, тексты,
  шаблоны названий терминалов и сессий, интервалы обновления;
- `src/agent_hypervisor/styles.tcss` — палитра, размеры и состояния кнопок;
- `src/agent_hypervisor/dialogs.py` — общий класс и содержимое модальных окон;
- `src/agent_hypervisor/tui.py` — взаимодействие интерфейса с backend.

В `ACTIONS` хоткей и подпись задаются вместе, из них строятся bindings и кнопки.
Состав агентов и отображаемые имена задаются в `config.yaml`. `TERMINAL_LABEL` используется
как запасная подпись обычных терминалов без YAML-описаний.
