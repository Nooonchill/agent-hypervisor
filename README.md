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
uv run hypervisor create 123 --type feature
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
HYPERVISOR_AGENT_COMMAND="gigacode" uv run hypervisor create demo
```

## Группы, worktree и сессии

При создании группы Hypervisor:

1. находит корень текущего Git-репозитория;
2. создаёт скрытый соседний worktree `.<repository>-worktrees/<group>` и ветку
   `<тип>/<код задачи>`, например `fix/PROJ-123`;
3. резервирует новый UUID Qwen-сессии для каждого агента из YAML;
4. оставляет группу неактивной до нажатия **Activate group**.

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

## Агенты и flow в config.yaml

По умолчанию читается `config.yaml` в текущем каталоге. Другой путь:

```bash
uv run hypervisor --config /path/to/config.yaml check-config
uv run hypervisor --config /path/to/config.yaml
```

Также поддерживается `HYPERVISOR_CONFIG`. Если файла по умолчанию нет, доступны три обычных
терминала. Явно заданный отсутствующий или неверный файл вызывает ошибку.

Пример в корне репозитория содержит Spec Writer, Developer и Architect, описания — в `examples/agents/`.
Минимальный пример:

```yaml
version: 1
agents-dir: ./agents
agents:
  - agent-id: developer
    agent-name: Developer
    model: null
    reviewer-id: architect
    create-pr: false
  - agent-id: architect
    agent-name: Architect
flow:
  - id: implement
    agent-id: developer
    goal: Реализовать согласованные изменения и запустить тесты.
  - id: review
    agent-id: architect
    goal: Проверить результат и описать замечания.
```

Для каждого `agent-id` требуется `<agents-dir>/<agent-id>.md`. Пути разрешаются относительно
YAML-файла. Markdown может иметь YAML frontmatter в стиле Qwen; системные инструкции берутся
из тела документа. `model` передаётся Qwen флагом `--model`, `null` оставляет модель по умолчанию.
Описание подключается через `--append-system-prompt`: каждый терминал — самостоятельный Qwen CLI,
а не вызов встроенного инструмента субагентов. Поля frontmatter вроде `tools` или `executor`
не настраивают этот отдельный процесс.

`agent-id` уникален и используется для ссылок, CLI-команд и имени сессии. `agent-name` показывается
в панели. `reviewer-id` может быть `null` или ссылаться на существующего агента. `create-pr`
по умолчанию `false`. Flow — упорядоченный список этапов: `agent-id`, непустой `goal` и
необязательный уникальный `id` (без него генерируется `step-1`, `step-2`, …).

Flow, связи ревью и флаг создания PR пока только проверяются и сохраняются. Ничего не запускается
автоматически. Код проверяет неизвестные поля, типы, ссылки, дубли и доступность описаний до старта.

Число панелей равно числу агентов; сетка подстраивается, при нехватке высоты появляется прокрутка.
Группа сохраняет снимок агентов, моделей, инструкций и flow. Изменения YAML применяются к новым
группам; существующие сохраняют прежние связи и историю.

## Проверка

```bash
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q src tests
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
