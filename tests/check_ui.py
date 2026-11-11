# -*- coding: utf-8 -*-
"""Sanity checks for the generated configuration UI."""

import importlib.util
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "task_multiplier.py")

spec = importlib.util.spec_from_file_location("tm", PLUGIN)
tm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tm)

html = tm._config_ui_html()
ids = re.findall(r'id="([^"]+)"', html)
refs = re.findall(r'getElementById\("([^"]+)"\)', html)

dups = sorted({i for i in ids if ids.count(i) > 1})
missing = sorted({r for r in refs if r not in ids})

expected = {
    "drop checkbox": 'id="drop"' in html,
    "drop method list": 'id="drop_method"' in html,
    "language selector": 'id="language"' in html,
    "English default label": "Eject the last part" in html,
    "Russian drop label": "Сброс последней детали" in html,
    "head method option": "Головой" in html,
    "beam method option": "Балкой оси X" in html,
    "beam template": 'id="beam_eject_gcode"' in html,
    "old bed template removed": 'id="bed_eject_gcode"' not in html,
    "getConfig bridge": "window.orca.getConfig" in html,
    "saveConfig bridge": "window.orca.saveConfig" in html,
    "onConfig bridge": "window.orca.onConfig" in html,
}
failed = sorted(name for name, ok in expected.items() if not ok)

print("ui bytes:", len(html))
print("ids:", len(ids), "unique:", len(set(ids)))
print("duplicate ids:", dups)
print("missing getElementById targets:", missing)
for name, ok in expected.items():
    print("%-28s %s" % (name + ":", "OK" if ok else "FAIL"))

out = os.path.join(os.environ.get("TEMP", "."), "orca_task_ui.html")
with open(out, "w", encoding="utf-8") as f:
    f.write(html)
print("written:", out)

sys.exit(1 if dups or missing or failed else 0)
