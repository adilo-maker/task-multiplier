# -*- coding: utf-8 -*-
"""Build a standalone preview of the config UI with a mocked window.orca."""

import importlib.util
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "task_multiplier.py")

spec = importlib.util.spec_from_file_location("tm", PLUGIN)
tm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tm)

html = tm._config_ui_html()
legacy = os.environ.get("ORCA_PREVIEW_LEGACY") == "1"
if legacy:
    config = {"drop": True, "drop_gcode": "G0 Y123 F9999"}
    name = "orca_task_ui_preview_legacy.html"
else:
    config = tm.normalize_config({})
    name = "orca_task_ui_preview.html"
payload = json.dumps(config, ensure_ascii=False)
mock = (
    "<script>window.orca={getConfig:function(){return %s;},"
    "saveConfig:function(){},restoreDefaults:function(){},"
    "onConfig:function(cb){cb(%s);},getContext:function(){return {};}};</script>"
    % (payload, payload)
)
html = html.replace("<script>", mock + "\n<script>", 1)

out = os.path.join(os.environ.get("TEMP", "."), name)
with open(out, "w", encoding="utf-8") as f:
    f.write(html)
print(out)
