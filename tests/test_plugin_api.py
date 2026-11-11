# -*- coding: utf-8 -*-
"""Test the plugin class against a minimal mock of OrcaSlicer's `orca` module.

Run from the repository root:
    py tests\\test_plugin_api.py
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PLUGIN_PATH = os.path.join(ROOT, "task_multiplier.py")
DEFAULT_SAMPLE = os.path.join(ROOT, "testdata", "sample_plate.gcode")
SAMPLE = os.environ.get("ORCA_SAMPLE_GCODE", DEFAULT_SAMPLE)

registered = []
packages = []


def build_mock_orca():
    orca = types.ModuleType("orca")

    class Base(object):
        pass

    class SlicingPipelineCapabilityBase(object):
        def __init__(self):
            self._config = {}

        def get_config(self):
            return dict(self._config)

    class Step(object):
        psGCodePostProcess = "psGCodePostProcess"
        psLayerSlice = "psLayerSlice"

    class ExecutionResult(object):
        @staticmethod
        def success(message=""):
            return ("success", message)

        @staticmethod
        def failure(code, message):
            return ("failure", code, message)

    def plugin(cls):
        packages.append(cls)
        return cls

    def register_capability(capability):
        registered.append(capability)

    slicing = types.ModuleType("orca.slicing")
    slicing.SlicingPipelineCapabilityBase = SlicingPipelineCapabilityBase
    slicing.Step = Step

    orca.slicing = slicing
    orca.base = Base
    orca.plugin = plugin
    orca.register_capability = register_capability
    orca.ExecutionResult = ExecutionResult
    return orca


def load_plugin():
    sys.modules["orca"] = build_mock_orca()
    spec = importlib.util.spec_from_file_location("task_multiplier_api_test", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    if not os.path.isfile(SAMPLE):
        print("FAIL: sample G-code not found:", SAMPLE)
        return 1

    tm = load_plugin()

    # OrcaSlicer instantiates each decorated package and asks it to register.
    check(len(packages) == 1, "exactly one plugin package declared")
    packages[0]().register_capabilities()

    check(len(registered) == 1, "exactly one capability registered")
    capability_class = registered[0]
    check(capability_class is tm.TaskMultiplier, "registered capability class")
    check(issubclass(capability_class, tm.orca.slicing.SlicingPipelineCapabilityBase), "base class")

    cap = capability_class()
    check(cap.get_name() == "Task Multiplier", "capability name")

    defaults = cap.get_default_config()
    for key in ("copies", "drop", "drop_method", "loop", "loop_firmware", "loop_count", "language"):
        check(key in defaults, "default config missing: %s" % key)
    check(defaults["drop_method"] in ("head", "beam"), "default drop method")
    check(defaults["language"] == "en", "English must be the default UI language")

    html = cap.get_config_ui()
    check("__DEFAULTS__" not in html, "config UI must not contain the raw defaults placeholder")
    check('id="drop"' in html, "config UI should expose the drop checkbox")
    check('id="drop_method"' in html, "config UI should expose the drop method list")
    check('id="language"' in html, "config UI should expose the language selector")
    check('value="en"' in html, "English option present")
    check('value="ru"' in html, "Russian option present")
    check("Eject the last part" in html, "English drop label")
    check("Сброс последней детали" in html, "Russian drop label")
    check("Головой" in html and "Балкой оси X" in html, "drop method options")
    check("бесконечно" in html, "config UI should expose the infinite loop option")
    check("applyLanguage" in html and "I18N" in html, "language switcher code")
    check('drop_method").disabled' not in html, "ejection method must stay enabled")

    class Ctx(object):
        def __init__(self, step, path):
            self.step = step
            self.gcode_path = path
            self.output_name = os.path.basename(path)

    with open(SAMPLE, "r", encoding="utf-8", errors="replace", newline="") as f:
        original = f.read()

    tmpdir = tempfile.mkdtemp(prefix="task_multiplier_api_")
    try:
        # Geometry steps must be ignored.
        target = os.path.join(tmpdir, "geometry.gcode")
        with open(target, "w", encoding="utf-8", newline="") as f:
            f.write(original)
        result = cap.execute(Ctx(tm.orca.slicing.Step.psLayerSlice, target))
        check(result[0] == "success", "geometry step result")
        with open(target, "r", encoding="utf-8", errors="replace", newline="") as f:
            check(f.read() == original, "geometry step must not modify the file")

        # Post-process step: normal mode, last part ejected.
        target = os.path.join(tmpdir, "normal.gcode")
        with open(target, "w", encoding="utf-8", newline="") as f:
            f.write(original)
        cap._config = {"copies": 2, "drop": True, "drop_method": "head", "z_lift": 15}
        result = cap.execute(Ctx(tm.orca.slicing.Step.psGCodePostProcess, target))
        check(result[0] == "success", "post-process result")
        check("2 copies" in result[1], "success message should mention copies")
        check("ejection: head" in result[1], "success message should mention the drop method")
        check("last part: ejected" in result[1], "success message should mention the last part")
        with open(target, "r", encoding="utf-8", errors="replace", newline="") as f:
            text = f.read()
        check(text.count("TASK_MULTIPLIER: copy ") == 2, "two copies written")
        check(text.count("G0 X220 F12000") == 2, "head ejection per copy")

        # Post-process step: last part stays on the bed.
        target = os.path.join(tmpdir, "last_stays.gcode")
        with open(target, "w", encoding="utf-8", newline="") as f:
            f.write(original)
        cap._config = {"copies": 2, "drop": False, "drop_method": "head", "z_lift": 15}
        result = cap.execute(Ctx(tm.orca.slicing.Step.psGCodePostProcess, target))
        check(result[0] == "success", "last-stays post-process result")
        check("last part: stays on the bed" in result[1], "last-stays message")
        with open(target, "r", encoding="utf-8", errors="replace", newline="") as f:
            text = f.read()
        check(text.count("G0 X220 F12000") == 1, "only the intermediate copy is ejected")

        # Second call must be a no-op.
        result = cap.execute(Ctx(tm.orca.slicing.Step.psGCodePostProcess, target))
        check(result[0] == "success", "second post-process result")
        check("already multiplied" in result[1], "second call should be skipped")
        with open(target, "r", encoding="utf-8", errors="replace", newline="") as f:
            check(f.read() == text, "second call must not change the file")

        # Auto-detected firmware loop.
        target = os.path.join(tmpdir, "loop.gcode")
        with open(target, "w", encoding="utf-8", newline="") as f:
            f.write(original)
        cap._config = {"loop": True, "loop_firmware": "auto", "loop_count": 0}
        result = cap.execute(Ctx(tm.orca.slicing.Step.psGCodePostProcess, target))
        check(result[0] == "success", "loop result")
        check("firmware loop" in result[1], "loop success message")
        check("Marlin" in result[1] or "marlin" in result[1], "auto-detected Marlin")
        with open(target, "r", encoding="utf-8", errors="replace", newline="") as f:
            loop_text = f.read()
        check("M808 L0" in loop_text, "Marlin infinite loop in output")
        check("SDCARD_LOOP_BEGIN" not in loop_text, "no Klipper loop in Marlin output")

        print("PASS: plugin API (registration, config UI, execute, idempotence, auto loop)")
        return 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print("FAIL:", exc)
        sys.exit(1)
