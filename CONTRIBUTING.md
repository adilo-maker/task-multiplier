# Участие в разработке

Спасибо за интерес к проекту. Плагин небольшой, поэтому процесс простой.

## Требования

- Windows 10/11 (основная платформа OrcaSlicer);
- Python 3.12 или новее;
- OrcaSlicer 2.5.0-rc или новее — для ручной проверки плагина.

## Быстрая проверка

Из корня репозитория:

```powershell
py tests\run_all.py
```

Отдельные проверки:

```powershell
py tests\test_transform.py
py tests\test_plugin_api.py
py tests\check_ui.py
```

Тесты используют `testdata\sample_plate.gcode`. Другой файл можно указать так:

```powershell
$env:ORCA_SAMPLE_GCODE = "D:\gcode\sample.gcode"
py tests\test_transform.py
```

Предпросмотр интерфейса настроек:

```powershell
py tests\make_ui_preview.py
```

Команда печатает путь к HTML-файлу во временной папке. Для проверки
миграции старых настроек:

```powershell
$env:ORCA_PREVIEW_LEGACY = "1"
py tests\make_ui_preview.py
```

## Что менять

- `task_multiplier.py` — плагин, преобразование G-кода и HTML-интерфейс.
- `install.ps1` — установка в `%APPDATA%\OrcaSlicer\orca_plugins`.
- `tests/` — тесты; при изменении поведения обновляйте их.
- `README.md` и `CHANGELOG.md` — документация и история версий.

Правила:

1. Не коммитьте личные профили, `config.json`, реальные G-коды пользователей
   и сгенерированные файлы.
2. Не коммитьте `dist/`, `__pycache__/` и временные HTML-предпросмотры.
3. Сохраняйте `install.ps1` в UTF-8 с BOM: Windows PowerShell 5.1 иначе
   неправильно читает русские строки.
4. Для новых параметров конфигурации добавляйте значения по умолчанию,
   миграцию старых ключей и тесты.
5. Не меняйте поведение стартового/конечного G-кода и защиту от повторной
   обработки без тестов.

## Проверка перед pull request

```powershell
py tests\run_all.py
powershell -ExecutionPolicy Bypass -File .\scripts\package.ps1
```

Убедитесь, что:

- все тесты проходят;
- архив `dist\task_multiplier-<версия>.zip` собирается;
- версия в `task_multiplier.py`, `install.ps1`, `CHANGELOG.md` и README
  совпадает;
- в diff нет секретов, абсолютных пользовательских путей и лишних файлов.

## Лицензия

Отправляя изменения, вы соглашаетесь с тем, что они распространяются по
лицензии MIT, указанной в `LICENSE`.
