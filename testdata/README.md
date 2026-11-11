# Тестовые данные

`sample_plate.gcode` — небольшой файл, нарезанный OrcaSlicer 2.5.0-rc:

- модель: куб 20 × 20 × 20 мм;
- профиль прошивки: Marlin 2 (`; gcode_flavor = marlin2`);
- стартовый и конечный G-код, `;LAYER_CHANGE`, `;TYPE:Custom`,
  `; EXECUTABLE_BLOCK_*` и `; CONFIG_BLOCK_*` присутствуют;
- личных путей и данных в файле нет.

Файл используется тестами `tests/test_transform.py` и
`tests/test_plugin_api.py`. Другой G-код можно подставить переменной
окружения `ORCA_SAMPLE_GCODE`.
