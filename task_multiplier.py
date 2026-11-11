# -*- coding: utf-8 -*-
# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "Task Multiplier"
# description = "Prints the same part N times in one job: the start/end G-code is kept once, the print body is repeated, with optional part ejection (by the head or by the X-axis beam), pause, filament prime, beeps and parking between copies. Supports infinite loops on Klipper (SDCARD_LOOP) and Marlin (M808). Written with AI assistance."
# author = "adilo321"
# version = "1.0.0"
# type = "slicing-pipeline"
# ///
"""Task Multiplier for OrcaSlicer.

This is a native OrcaSlicer slicing-pipeline plugin. It runs at the
``Step.psGCodePostProcess`` seam on the exported G-code file and rewrites it
in place:

    header + start G-code          <- printed once
    body (copy 1)
    eject / parking / beep / pause <- between copies
    body (copy 2)
    ...
    body (copy N)
    end G-code + statistics        <- printed once

The start block is everything from the beginning of the file up to the first
layer marker (``;LAYER_CHANGE`` or ``;LAYER:``). The end block starts at the
last ``;TYPE:Custom`` before ``; EXECUTABLE_BLOCK_END`` (OrcaSlicer emits the
machine end G-code there). Everything in between is the repeated body.

Configuration is stored by OrcaSlicer in
``%APPDATA%\\OrcaSlicer\\orca_plugins\\config.json`` and is edited from
Plugins -> Task Multiplier -> Config (the plugin provides a Russian HTML UI
when the host supports it, otherwise the JSON editor is used).

The module is intentionally importable without the ``orca`` runtime so the
transformation can be tested from a normal Python interpreter; the plugin
class is only declared when ``orca`` is available.
"""

from __future__ import annotations

import json
import math
import os
import re

try:  # The real module only exists inside OrcaSlicer's embedded interpreter.
    import orca  # type: ignore
except Exception:  # pragma: no cover - exercised by the local test harness
    orca = None


# ---------------------------------------------------------------------------
# Defaults (faithful to the original "Множитель задач" settings.ini)
# ---------------------------------------------------------------------------

# Ejection by the X-axis beam (the original app's "drop" routine): move the
# beam to the rear, lift Z, then jerk it to the front.
DEFAULT_BEAM_EJECT_GCODE = (
    "G1 X1 Y220 F3000\n"
    "G0 Z{Z} F3600\n"
    "G0 Y0 F14000\n"
    "G0 Y220 F3600"
)

# Ejection by moving the head across the bed. The Z height is the same {Z}
# placeholder, so set "z_lift" slightly above the part height.
DEFAULT_HEAD_EJECT_GCODE = (
    "G0 Z{Z} F3600\n"
    "G0 X1 F3000\n"
    "G0 X220 F12000\n"
    "G0 X1 F12000"
)

DEFAULT_CLEAN_GCODE = (
    "G1 X{X} Y20 Z0.28 F5000.0\n"
    "G1 X{X} Y145.0 Z0.28 F1500.0 E15\n"
    "G1 X{X}.5 Y145.0 Z0.28 F5000.0\n"
    "G1 X{X}.5 Y20 Z0.28 F1500.0 E30"
)

DEFAULT_TURNOFF_GCODE = (
    "M106 S0 ; Auto generated turn-off fan\n"
    "M104 S0 ; Auto generated Turn-off hotend\n"
    "M140 S0 ; Auto generated Turn-off bed\n"
    "M84 X Y Z E\n"
    "M82\n"
    "M104 S0"
)

DEFAULT_PAUSE_BEFORE_GCODE = (
    "M106 S0 ; Turn-off fan\n"
    "M104 S0 ; Turn-off hotend\n"
    "M140 S0 ; Turn-off bed\n"
    "M84 X Y Z E ; Turn-off drivers"
)

DEFAULT_BEEP_GCODE = "M300 S440 P200\nM300 S660 P250\nM300 S880 P300"

DEFAULTS = {
    # UI language: "en" (default) or "ru". The language is stored in the
    # plugin config so the choice survives OrcaSlicer restarts.
    "language": "en",
    # Number of parts in one job (total copies, not "extra repeats").
    "copies": 2,
    # Firmware-side loop: the body is written once and repeated by the printer.
    # Klipper needs [sdcard_loop] and an SD/virtual-SD print; Marlin needs
    # GCODE_REPEAT_MARKERS and an SD print. loop_count 0 means "forever".
    "loop": False,
    # auto: detect from OrcaSlicer's "; gcode_flavor = ..." config block.
    "loop_firmware": "auto",
    "loop_count": 0,
    # Last-part ejection. Intermediate copies are always ejected so the bed is
    # clear for the next copy; this switch controls whether the final part is
    # ejected too. When it is off, the last part stays on the bed.
    # "drop_method" selects the routine and is always available:
    #   head - sweep the print head across the bed (head_eject_gcode)
    #   beam - hit the part with the X-axis beam (beam_eject_gcode)
    "drop": False,
    "drop_method": "beam",
    "head_eject_gcode": DEFAULT_HEAD_EJECT_GCODE,
    "beam_eject_gcode": DEFAULT_BEAM_EJECT_GCODE,
    "z_lift": 30.0,
    # Intermediate parking after every part.
    "parking": False,
    "parking_gcode": "G28 ;Home",
    # Beep after every part (including the last one).
    "beep": False,
    "beep_gcode": DEFAULT_BEEP_GCODE,
    # Filament prime / clean line before every copy.
    "clean": False,
    "clean_gcode": DEFAULT_CLEAN_GCODE,
    "clean_start": 1.0,
    "clean_step": 1.0,
    "clean_axis": "X",
    # Long pause between batches of parts.
    "pause": False,
    "pause_every": 40,
    "pause_minutes": 30,
    "pause_before_gcode": DEFAULT_PAUSE_BEFORE_GCODE,
    "pause_after_gcode": "",
    "pause_rehome": True,
    "pause_reheat": True,
    # Extra shutdown at the very end (usually the slicer end G-code does it).
    "turnoff": False,
    "turnoff_gcode": DEFAULT_TURNOFF_GCODE,
    # Disable Marlin power-loss recovery (M413 S0) after the start block.
    "disable_autosave": False,
    # Extra final template. Supports {COUNT}, {INDEX}, {Z}, {X}, {Y}.
    "final_gcode": "",
    # Comments that delimit copies (safe to disable if a firmware dislikes them).
    "marker_comments": True,
    # Add a note to the G-code header.
    "header_note": True,
}


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def _as_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on", "да", "вкл"):
            return True
        if text in ("0", "false", "no", "off", "нет", "выкл", ""):
            return False
    return default


def _as_int(value, default, minimum=None, maximum=None):
    try:
        result = int(float(value))
    except (TypeError, ValueError):
        result = int(default)
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def _as_float(value, default, minimum=None):
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = float(default)
    if not math.isfinite(result):
        result = float(default)
    if minimum is not None:
        result = max(minimum, result)
    return result


def _as_text(value, default):
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def normalize_config(raw):
    """Merge a stored/partial config with the defaults and coerce types."""
    cfg = dict(DEFAULTS)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = {}
    raw_dict = raw if isinstance(raw, dict) else {}
    if isinstance(raw, dict):
        cfg.update(raw)

    # Backwards compatibility:
    #   1.0: boolean "drop" + a single "drop_gcode" routine.
    #   1.1: "eject_mode" = none/head/bed/custom.
    # Both migrate to the new "drop" switch + "drop_method" = head/beam.
    legacy_mode = _as_text(raw_dict.get("eject_mode"), "").strip().lower()
    if "drop" in raw_dict:
        cfg["drop"] = _as_bool(raw_dict["drop"], DEFAULTS["drop"])
    elif legacy_mode:
        cfg["drop"] = legacy_mode != "none"
    else:
        cfg["drop"] = DEFAULTS["drop"]

    if "drop_method" in raw_dict:
        method = _as_text(raw_dict["drop_method"], DEFAULTS["drop_method"]).strip().lower()
    elif legacy_mode == "head":
        method = "head"
    else:
        method = "beam"
    cfg["drop_method"] = method if method in ("head", "beam") else "beam"

    # The old "bed" mode and the 1.0 "drop_gcode" are both the original
    # X-axis-beam routine; keep the user's edited template when migrating.
    if "beam_eject_gcode" in raw_dict:
        beam_template = raw_dict["beam_eject_gcode"]
    elif "bed_eject_gcode" in raw_dict:
        beam_template = raw_dict["bed_eject_gcode"]
    else:
        beam_template = raw_dict.get("drop_gcode")
    cfg["beam_eject_gcode"] = _as_text(
        beam_template, DEFAULTS["beam_eject_gcode"]
    )

    cfg["copies"] = _as_int(cfg["copies"], DEFAULTS["copies"], 1, 1000)
    cfg["loop_count"] = _as_int(cfg["loop_count"], DEFAULTS["loop_count"], 0, 1000000)
    cfg["loop_firmware"] = _as_text(cfg["loop_firmware"], "auto").strip().lower()
    if cfg["loop_firmware"] not in ("auto", "klipper", "marlin"):
        cfg["loop_firmware"] = "auto"
    cfg["language"] = _as_text(cfg.get("language"), "en").strip().lower()
    if cfg["language"] not in ("en", "ru"):
        cfg["language"] = "en"
    cfg["z_lift"] = _as_float(cfg["z_lift"], DEFAULTS["z_lift"], 0.0)
    cfg["clean_start"] = _as_float(cfg["clean_start"], DEFAULTS["clean_start"])
    cfg["clean_step"] = _as_float(cfg["clean_step"], DEFAULTS["clean_step"])
    cfg["clean_axis"] = _as_text(cfg["clean_axis"], "X").strip().upper()
    if cfg["clean_axis"] not in ("X", "Y"):
        cfg["clean_axis"] = "X"
    cfg["pause_every"] = _as_int(cfg["pause_every"], DEFAULTS["pause_every"], 1, 100000)
    cfg["pause_minutes"] = _as_float(cfg["pause_minutes"], DEFAULTS["pause_minutes"], 0.0)

    for key in (
        "drop",
        "loop",
        "parking",
        "beep",
        "clean",
        "pause",
        "pause_rehome",
        "pause_reheat",
        "turnoff",
        "disable_autosave",
        "marker_comments",
        "header_note",
    ):
        cfg[key] = _as_bool(cfg[key], DEFAULTS[key])

    for key in (
        "head_eject_gcode",
        "beam_eject_gcode",
        "parking_gcode",
        "beep_gcode",
        "clean_gcode",
        "pause_before_gcode",
        "pause_after_gcode",
        "turnoff_gcode",
        "final_gcode",
    ):
        cfg[key] = _as_text(cfg[key], DEFAULTS[key])

    # Legacy-only keys are fully represented by the new pair and template.
    for legacy_key in ("eject_mode", "bed_eject_gcode", "drop_gcode"):
        cfg.pop(legacy_key, None)

    return cfg


def load_plugin_config(capability):
    """Read the capability config from OrcaSlicer (falls back to defaults)."""
    raw = {}
    try:
        getter = getattr(capability, "get_config", None)
        if callable(getter):
            raw = getter()
    except Exception:
        raw = {}
    return normalize_config(raw)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def _eol_of(lines):
    for line in lines:
        if line.endswith("\r\n"):
            return "\r\n"
        if line.endswith("\n"):
            return "\n"
        if line.endswith("\r"):
            return "\r"
    return "\n"


def _strip_eol(line):
    if line.endswith("\r\n"):
        return line[:-2]
    if line.endswith("\n") or line.endswith("\r"):
        return line[:-1]
    return line


def _fmt_number(value):
    """1.0 -> '1', 1.5 -> '1.5', 1.25 -> '1.25'."""
    value = float(value)
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    text = ("%.3f" % value).rstrip("0").rstrip(".")
    return text or "0"


def _clean_coordinate(cfg, index, total):
    """Coordinate of the prime line for a 1-based copy index."""
    step = cfg["clean_step"]
    return cfg["clean_start"] + step * (index - 1)


def _replace_numbers(text, cfg, index, total, clean_coord):
    replacements = {
        "{Z}": _fmt_number(cfg["z_lift"]),
        "{COUNT}": str(total),
        "{TOTAL}": str(total),
        "{INDEX}": str(index),
        "{COPY}": str(index),
    }
    if cfg["clean_axis"] == "Y":
        replacements["{Y}"] = _fmt_number(clean_coord)
        replacements["{X}"] = _fmt_number(clean_coord)
    else:
        replacements["{X}"] = _fmt_number(clean_coord)
        replacements["{Y}"] = _fmt_number(clean_coord)
    for token, value in replacements.items():
        if token in text:
            text = text.replace(token, value)
    return text


def _normalize_template(text, eol):
    """Make a config template use the file's line ending and a trailing EOL."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    if not lines:
        return ""
    return eol.join(lines) + eol


def _expand_template(text, cfg, index, total, eol, clean_coord=None):
    if clean_coord is None:
        clean_coord = _clean_coordinate(cfg, index, total)
    text = _replace_numbers(text, cfg, index, total, clean_coord)
    return _normalize_template(text, eol)


def _render_clean(cfg, index, total, eol):
    coord = _clean_coordinate(cfg, index, total)
    return _expand_template(cfg["clean_gcode"], cfg, index, total, eol, coord).rstrip("\r\n")


def _capture_temperatures(prefix_lines):
    """Last non-zero hotend/bed temperature set in the start G-code."""
    hotend = None
    bed = None
    for line in prefix_lines:
        code = line.split(";", 1)[0].strip()
        if not code:
            continue
        for match in re.finditer(r"\bM10[49]\b[^\n;]*?\bS(\d+(?:\.\d+)?)", code):
            value = float(match.group(1))
            if value > 0:
                hotend = value
        for match in re.finditer(r"\bM1[49]0\b[^\n;]*?\bS(\d+(?:\.\d+)?)", code):
            value = float(match.group(1))
            if value > 0:
                bed = value
    return hotend, bed


# ---------------------------------------------------------------------------
# G-code layout
# ---------------------------------------------------------------------------

_LAYER_MARKERS = (";LAYER_CHANGE", ";LAYER:")
_END_BLOCK_MARKER = ";TYPE:Custom"
_EXEC_START_MARKER = "; EXECUTABLE_BLOCK_START"
_EXEC_END_MARKER = "; EXECUTABLE_BLOCK_END"


class Layout(object):
    def __init__(self, prefix, body_start, body_end, suffix_start, suffix, exec_start, exec_end):
        self.prefix = prefix
        self.body_start = body_start
        self.body_end = body_end
        self.suffix_start = suffix_start
        self.suffix = suffix
        self.exec_start = exec_start
        self.exec_end = exec_end


def _find_exec_start(lines):
    for i, line in enumerate(lines):
        if line.strip().startswith(_EXEC_START_MARKER):
            return i
    return -1


def _find_exec_end(lines):
    result = -1
    for i, line in enumerate(lines):
        if line.strip().startswith(_EXEC_END_MARKER):
            result = i
    return result


def _is_layer_marker(line):
    stripped = line.strip()
    return any(stripped.startswith(marker) for marker in _LAYER_MARKERS)


def analyze_gcode(lines):
    """Split lines into (prefix, body, suffix).

    `prefix` already contains the start G-code; `suffix` starts at the final
    `;TYPE:Custom` block (machine end G-code) and includes the config/statistics
    block after `; EXECUTABLE_BLOCK_END`.
    """
    if not lines:
        raise ValueError("G-code file is empty")

    exec_start = _find_exec_start(lines)
    exec_end = _find_exec_end(lines)

    search_from = exec_start + 1 if exec_start >= 0 else 0
    search_to = exec_end if exec_end >= 0 else len(lines)

    body_start = -1
    for i in range(search_from, search_to):
        if _is_layer_marker(lines[i]):
            body_start = i
            break
    if body_start < 0:
        raise ValueError(
            "first layer marker not found (expected ;LAYER_CHANGE or ;LAYER:). "
            "This does not look like OrcaSlicer/PrusaSlicer G-code."
        )

    end_start = -1
    for i in range(body_start, search_to):
        if lines[i].strip().startswith(_END_BLOCK_MARKER):
            end_start = i
    if end_start < 0:
        # No machine end G-code block: the suffix is only the executable-end
        # marker and the statistics block.
        end_start = search_to
        body_end = search_to
        suffix_start = search_to
    else:
        body_end = end_start
        suffix_start = end_start

    if body_end <= body_start:
        raise ValueError("print body is empty; nothing to repeat")

    prefix = list(lines[:body_start])
    suffix = list(lines[suffix_start:])
    return Layout(prefix, body_start, body_end, suffix_start, suffix, exec_start, exec_end)


# ---------------------------------------------------------------------------
# Transformation
# ---------------------------------------------------------------------------


def _comment(cfg, eol, text):
    if not cfg["marker_comments"]:
        return ""
    return "; TASK_MULTIPLIER: " + text + eol


def _insert_header_note(prefix, cfg, eol, firmware="", source="selected manually"):
    if not cfg["header_note"]:
        return prefix
    if cfg["loop"]:
        repeats = "infinite" if cfg["loop_count"] == 0 else str(cfg["loop_count"])
        note = (
            "; TASK_MULTIPLIER: firmware loop (%s, %s), %s repeats, "
            "body written once, start/end G-code printed once%s"
            % (firmware or cfg["loop_firmware"], source, repeats, eol)
        )
    else:
        note = (
            "; TASK_MULTIPLIER: %d copies, start/end G-code printed once, body repeated %d times%s"
            % (cfg["copies"], cfg["copies"], eol)
        )
    for i, line in enumerate(prefix):
        if line.strip() == "; HEADER_BLOCK_END":
            prefix.insert(i + 1, note)
            return prefix
    prefix.insert(0, note)
    return prefix


def _set_temperature_code(hotend, bed, eol):
    lines = []
    if hotend is not None:
        lines.append("M104 S%s ; TASK_MULTIPLIER: restore hotend" % _fmt_number(hotend))
    if bed is not None:
        lines.append("M140 S%s ; TASK_MULTIPLIER: restore bed" % _fmt_number(bed))
    if bed is not None:
        lines.append("M190 S%s ; TASK_MULTIPLIER: wait for bed" % _fmt_number(bed))
    if hotend is not None:
        lines.append("M109 S%s ; TASK_MULTIPLIER: wait for hotend" % _fmt_number(hotend))
    return eol.join(lines) + eol if lines else ""


def _render_body_line(line, cfg, index, total, eol, clean_text):
    """Expand per-copy placeholders in one body line."""
    if "{" not in line:
        return line

    content = _strip_eol(line)
    line_eol = line[len(content):] or eol

    if "{CLEAN}" in content:
        if cfg["clean"]:
            replacement = clean_text
        else:
            replacement = "; TASK_MULTIPLIER: {CLEAN} removed (filament prime disabled)"
        content = content.replace("{CLEAN}", replacement)

    if "{" in content:
        content = _replace_numbers(
            content, cfg, index, total, _clean_coordinate(cfg, index, total)
        )
    return content + line_eol


def _body_contains_clean(lines, body_start, body_end):
    for i in range(body_start, body_end):
        if "{CLEAN}" in lines[i]:
            return True
    return False


_EJECT_LABELS = {
    "head": "head ejection",
    "beam": "X-axis beam ejection",
}


def _eject_template(cfg, eject=True):
    """Return (label, template) for the selected drop method.

    ``eject`` is False for the final copy when the user wants the last part to
    stay on the bed. Intermediate copies always eject.
    """
    if not eject:
        return "", ""
    method = cfg["drop_method"]
    if method == "head":
        return _EJECT_LABELS[method], cfg["head_eject_gcode"]
    return _EJECT_LABELS["beam"], cfg["beam_eject_gcode"]


def _routine_after_copy(cfg, index, total, eol, hotend, bed, allow_pause=True, eject=True):
    """Ejection / parking / beep / optional long pause after one finished copy."""
    chunks = []

    label, template = _eject_template(cfg, eject)
    if label:
        chunks.append(_comment(cfg, eol, "%s after copy %d" % (label, index)))
        chunks.append(_expand_template(template, cfg, index, total, eol))

    if cfg["parking"]:
        chunks.append(_comment(cfg, eol, "parking after copy %d" % index))
        chunks.append(_expand_template(cfg["parking_gcode"], cfg, index, total, eol))

    if cfg["beep"]:
        chunks.append(_comment(cfg, eol, "beep after copy %d" % index))
        chunks.append(_expand_template(cfg["beep_gcode"], cfg, index, total, eol))

    if (
        allow_pause
        and cfg["pause"]
        and cfg["pause_minutes"] > 0
        and index < total
        and index % cfg["pause_every"] == 0
    ):
        chunks.append(_comment(cfg, eol, "pause after copy %d (%s min)" % (index, _fmt_number(cfg["pause_minutes"]))))
        chunks.append(_expand_template(cfg["pause_before_gcode"], cfg, index, total, eol))
        seconds = int(round(cfg["pause_minutes"] * 60.0))
        chunks.append("G4 S%d ; TASK_MULTIPLIER: pause %s min%s" % (seconds, _fmt_number(cfg["pause_minutes"]), eol))

        after = _expand_template(cfg["pause_after_gcode"], cfg, index, total, eol)
        if after:
            chunks.append(after)
        else:
            if cfg["pause_rehome"]:
                chunks.append("G28 ; TASK_MULTIPLIER: home after pause" + eol)
            if cfg["pause_reheat"]:
                chunks.append(_set_temperature_code(hotend, bed, eol))

    return "".join(chunks)


def _render_suffix(suffix, cfg, eol, total):
    """Expand {COUNT} in the end block and append optional final code."""
    chunks = []
    insert_at = len(suffix)
    for i, line in enumerate(suffix):
        if line.strip().startswith(_EXEC_END_MARKER):
            insert_at = i
            break

    for i, line in enumerate(suffix):
        if i == insert_at:
            if cfg["turnoff"]:
                chunks.append(_comment(cfg, eol, "final shutdown (turnoff)"))
                chunks.append(_expand_template(cfg["turnoff_gcode"], cfg, total, total, eol))
            if cfg["final_gcode"].strip():
                chunks.append(_comment(cfg, eol, "final user code"))
                chunks.append(_expand_template(cfg["final_gcode"], cfg, total, total, eol))
        chunks.append(_render_body_line(line, cfg, total, total, eol, ""))
    if insert_at >= len(suffix):
        if cfg["turnoff"]:
            chunks.append(_comment(cfg, eol, "final shutdown (turnoff)"))
            chunks.append(_expand_template(cfg["turnoff_gcode"], cfg, total, total, eol))
        if cfg["final_gcode"].strip():
            chunks.append(_comment(cfg, eol, "final user code"))
            chunks.append(_expand_template(cfg["final_gcode"], cfg, total, total, eol))
    return "".join(chunks)


_FLAVOR_RE = re.compile(r"gcode_flavor\s*=\s*([A-Za-z0-9_.-]+)", re.IGNORECASE)

# Commands that are emitted by OrcaSlicer printer profiles for one firmware.
_KLIPPER_MARKERS = (
    "SET_VELOCITY_LIMIT",
    "BED_MESH_CALIBRATE",
    "EXCLUDE_OBJECT_START",
    "PRINT_START",
    "SAVE_VARIABLE",
)
_MARLIN_MARKERS = (
    "M808 L",
    "M900 ",
    "M413 ",
)


def _contains_any(lines, markers):
    for line in lines:
        for marker in markers:
            if marker in line:
                return True
    return False


def detect_firmware(lines):
    """Return ("klipper"|"marlin", reason) or (None, reason).

    OrcaSlicer writes ``; gcode_flavor = klipper`` / ``marlin2`` into the
    config block at the end of the file; that is the authoritative source. A
    profile may still be labelled Marlin while the printer actually runs
    Klipper, so Klipper-only commands take precedence. If nothing is found,
    look for commands that are specific to one of the two firmwares.
    """
    flavor = ""
    for line in reversed(lines):
        match = _FLAVOR_RE.search(line)
        if match:
            flavor = match.group(1).lower()
            break

    if "klipper" in flavor:
        return "klipper", "gcode_flavor=%s" % flavor

    # "Marlin" flavor with Klipper macros is a common OrcaSlicer setup.
    if _contains_any(lines, _KLIPPER_MARKERS):
        return "klipper", "Klipper-specific commands found"

    if "marlin" in flavor or flavor.startswith("reprap"):
        return "marlin", "gcode_flavor=%s" % flavor

    if _contains_any(lines, _MARLIN_MARKERS):
        return "marlin", "Marlin-specific commands found"
    if flavor:
        return None, "unsupported gcode_flavor=%s" % flavor
    return None, "gcode_flavor not found"


def resolve_loop_firmware(cfg, lines):
    """Resolve "auto" to a concrete firmware.

    Returns ``(firmware, source, reliable)`` where ``source`` is shown in the
    G-code header/plugin message and ``reliable`` is False only when the
    firmware had to be assumed because nothing could be detected.
    """
    if cfg["loop_firmware"] in ("klipper", "marlin"):
        return cfg["loop_firmware"], "selected manually", True
    firmware, reason = detect_firmware(lines)
    if firmware is None:
        # Never refuse to generate the file: assume Klipper (the most common
        # host for [sdcard_loop]) and leave a warning in the G-code.
        return "klipper", "assumed, %s" % reason, False
    return firmware, "auto-detected: %s" % reason, True


def _loop_commands(cfg, firmware):
    """Return (begin, end) commands for the resolved firmware loop."""
    if firmware == "marlin":
        return "M808 L%d" % cfg["loop_count"], "M808"
    return "SDCARD_LOOP_BEGIN COUNT=%d" % cfg["loop_count"], "SDCARD_LOOP_END"


def _iter_copy(
    body,
    cfg,
    eol,
    body_has_clean,
    hotend,
    bed,
    index,
    total,
    allow_pause=True,
    is_last=False,
):
    """Yield one copy of the print body followed by the inter-copy routine."""
    if total == 0:
        yield _comment(cfg, eol, "copy %d (infinite loop)" % index)
    else:
        yield _comment(cfg, eol, "copy %d of %d" % (index, total))

    clean_text = _render_clean(cfg, index, total, eol) if cfg["clean"] else ""
    if cfg["clean"] and not body_has_clean:
        yield clean_text + eol

    for line in body:
        yield _render_body_line(line, cfg, index, total, eol, clean_text)

    # Intermediate copies are always ejected. The final copy is ejected only
    # when "drop" (eject the last part) is enabled; otherwise it stays on the
    # bed. Firmware loops pass is_last=False because the firmware repeats the
    # body and cannot distinguish the last iteration.
    eject = (not is_last) or cfg["drop"]
    yield _routine_after_copy(
        cfg, index, total, eol, hotend, bed, allow_pause, eject=eject
    )


def _iter_output(lines, cfg, stats):
    """Yield the transformed G-code chunk by chunk."""
    eol = _eol_of(lines)
    layout = analyze_gcode(lines)

    hotend, bed = _capture_temperatures(layout.prefix)
    body = lines[layout.body_start : layout.body_end]
    body_has_clean = _body_contains_clean(lines, layout.body_start, layout.body_end)
    total = cfg["loop_count"] if cfg["loop"] else cfg["copies"]

    firmware = ""
    source = ""
    reliable = True
    if cfg["loop"]:
        firmware, source, reliable = resolve_loop_firmware(cfg, lines)

    stats.update(
        {
            "copies": cfg["copies"],
            "loop": cfg["loop"],
            "loop_firmware": firmware or cfg["loop_firmware"],
            "loop_firmware_source": source,
            "loop_firmware_detected": source.startswith("auto-detected"),
            "loop_firmware_reason": source,
            "loop_count": cfg["loop_count"],
            "drop": cfg["drop"],
            "drop_method": cfg["drop_method"],
            "body_lines": len(body),
            "source_lines": len(lines),
            "hotend": hotend,
            "bed": bed,
        }
    )

    # ---- start block (printed once) -------------------------------------
    prefix = list(layout.prefix)
    _insert_header_note(prefix, cfg, eol, firmware, source)

    if cfg["disable_autosave"] and not any("M413" in line for line in prefix):
        prefix.append("M413 S0 ; TASK_MULTIPLIER: disable power-loss recovery" + eol)

    prefix.append(_comment(cfg, eol, "start of repeated section"))
    for line in prefix:
        yield line

    # ---- repeated print body --------------------------------------------
    if cfg["loop"]:
        begin, end = _loop_commands(cfg, firmware)
        repeats = "infinite" if cfg["loop_count"] == 0 else "%d repeats" % cfg["loop_count"]
        if not reliable:
            yield _comment(
                cfg,
                eol,
                "WARNING: firmware auto-detect failed (%s); assuming %s - "
                "set the firmware manually if the loop does not work"
                % (source.replace("assumed, ", ""), firmware),
            )
        yield _comment(cfg, eol, "firmware loop begin (%s, %s)" % (firmware, repeats))
        yield begin + eol

        # The body is written once. Periodic pauses cannot be scheduled
        # reliably across firmware-side iterations, so they are skipped.
        for chunk in _iter_copy(
            body, cfg, eol, body_has_clean, hotend, bed, 1, total, allow_pause=False
        ):
            yield chunk

        yield _comment(cfg, eol, "firmware loop end")
        yield end + eol
    else:
        for i in range(cfg["copies"]):
            for chunk in _iter_copy(
                body,
                cfg,
                eol,
                body_has_clean,
                hotend,
                bed,
                i + 1,
                cfg["copies"],
                is_last=(i + 1 == cfg["copies"]),
            ):
                yield chunk

    yield _comment(cfg, eol, "end of repeated section")

    # ---- end block (printed once) ---------------------------------------
    yield _render_suffix(layout.suffix, cfg, eol, total)


def transform_lines(lines, raw_config=None):
    """Return the transformed G-code as a single string (used by tests)."""
    cfg = normalize_config(raw_config)
    stats = {}
    return "".join(_iter_output(lines, cfg, stats))


# ---------------------------------------------------------------------------
# File transformation
# ---------------------------------------------------------------------------

_PROCESSED_MARKER = "; TASK_MULTIPLIER: start of repeated section"


def _already_processed(lines):
    return any(line.startswith(_PROCESSED_MARKER) for line in lines)


def transform_file(path, raw_config=None):
    """Rewrite `path` in place. Returns a small statistics dict."""
    cfg = normalize_config(raw_config)

    with open(path, "r", encoding="utf-8", errors="replace", newline="") as handle:
        text = handle.read()
    lines = text.splitlines(keepends=True)
    del text

    if not lines:
        raise ValueError("G-code file is empty")

    # Safety net: never multiply an already multiplied file. OrcaSlicer always
    # hands us a fresh slice, but a manual re-run must not explode the G-code.
    if _already_processed(lines):
        layout = analyze_gcode(lines)
        firmware, source = "", ""
        if cfg["loop"]:
            firmware, source, _reliable = resolve_loop_firmware(cfg, lines)
        return {
            "copies": cfg["copies"],
            "loop": cfg["loop"],
            "loop_firmware": firmware or cfg["loop_firmware"],
            "loop_firmware_source": source,
            "loop_firmware_detected": source.startswith("auto-detected"),
            "loop_firmware_reason": source,
            "loop_count": cfg["loop_count"],
            "drop": cfg["drop"],
            "drop_method": cfg["drop_method"],
            "body_lines": layout.body_end - layout.body_start,
            "source_lines": len(lines),
            "hotend": None,
            "bed": None,
            "already_processed": True,
        }

    stats = {"already_processed": False}
    # Read everything first, then overwrite: this avoids a temporary file
    # (which would need extra permissions) and keeps the operation atomic from
    # the plugin's point of view.
    with open(path, "w", encoding="utf-8", newline="") as out:
        for chunk in _iter_output(lines, cfg, stats):
            out.write(chunk)

    return stats


# ---------------------------------------------------------------------------
# Native OrcaSlicer plugin
# ---------------------------------------------------------------------------


def _config_ui_html():
    """Bilingual configuration UI, English by default (en/ru)."""
    html = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<style>
  :root { color-scheme: light dark; }
  body { font: 14px/1.45 "Segoe UI", Arial, sans-serif; margin: 0; padding: 14px 16px 22px; background: transparent; color: var(--orca-text-color, #1f2328); }
  h2 { font-size: 17px; margin: 0 0 4px; }
  p.note { margin: 0 0 14px; opacity: .75; }
  fieldset { border: 1px solid var(--orca-border-color, #c8c8c8); border-radius: 8px; margin: 0 0 12px; padding: 10px 12px 12px; }
  legend { font-weight: 600; padding: 0 5px; }
  label { display: flex; align-items: center; gap: 7px; margin: 5px 0; }
  label.check { font-weight: 600; }
  .row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 6px 0; }
  .row > span { min-width: 210px; }
  input[type=number] { width: 92px; padding: 4px 6px; border: 1px solid var(--orca-border-color, #b8b8b8); border-radius: 5px; background: var(--orca-input-bg, #fff); color: inherit; }
  textarea { width: 100%; box-sizing: border-box; min-height: 68px; resize: vertical; padding: 6px 8px; border: 1px solid var(--orca-border-color, #b8b8b8); border-radius: 5px; font: 12px/1.35 Consolas, "Courier New", monospace; background: var(--orca-input-bg, #fff); color: inherit; }
  details { margin: 8px 0; }
  summary { cursor: pointer; font-weight: 600; }
  .hint { display: block; margin: 2px 0 6px 0; opacity: .7; font-size: 12px; }
  .buttons { display: flex; gap: 10px; align-items: center; margin-top: 14px; }
  button { padding: 7px 16px; border-radius: 6px; border: 1px solid var(--orca-border-color, #b8b8b8); background: var(--orca-button-bg, #f3f3f3); color: inherit; cursor: pointer; font-weight: 600; }
  button.primary { background: #1f883d; border-color: #1a7f37; color: #fff; }
  button:disabled { opacity: .5; cursor: default; }
  #status { margin-left: 4px; opacity: .8; font-size: 13px; }
  .readonly { pointer-events: none; opacity: .65; }
</style>
</head>
<body>
<h2 data-i18n="title">Task Multiplier</h2>
<p class="note" data-i18n="subtitle">Prints one part N times in a single G-code file. The start and end blocks run once; only the print body is repeated.</p>

<div class="row">
  <span data-i18n="language">Language:</span>
  <select id="language" style="padding:4px 6px;border-radius:5px;">
    <option value="en">English</option>
    <option value="ru">Русский</option>
  </select>
</div>

<fieldset>
  <legend data-i18n="section_main">Main</legend>
  <div class="row"><span data-i18n="copies">Parts per job:</span><input id="copies" type="number" min="1" max="1000" step="1"></div>
  <label class="check"><input id="drop" type="checkbox"> <span data-i18n="drop">Eject the last part</span></label>
  <div class="row"><span data-i18n="drop_method">Ejection method:</span>
    <select id="drop_method" style="padding:4px 6px;border-radius:5px;">
      <option value="head" data-i18n="method_head">Head</option>
      <option value="beam" data-i18n="method_beam">X-axis beam</option>
    </select>
  </div>
  <div class="row"><span data-i18n="z_lift">Z lift, mm:</span><input id="z_lift" type="number" min="0" step="0.1"></div>
  <span class="hint" data-i18n="hint_drop">Intermediate copies are always ejected so the bed is clear. When this is off, only the last part stays on the bed. The ejection method is always active.</span>
</fieldset>

<fieldset>
  <legend data-i18n="section_between">Between copies</legend>
  <label class="check"><input id="parking" type="checkbox"> <span data-i18n="parking">Park the head with G28 after every part</span></label>
  <span class="hint" data-i18n="hint_parking">Leave this off if head parking is not needed: the head stays in place after the part is ejected.</span>
  <label class="check"><input id="beep" type="checkbox"> <span data-i18n="beep">Beep after every part</span></label>
  <label class="check"><input id="clean" type="checkbox"> <span data-i18n="clean">Prime filament before every part</span></label>
  <div class="row"><span data-i18n="clean_start">Prime start coordinate, mm:</span><input id="clean_start" type="number" step="0.5"></div>
  <div class="row"><span data-i18n="clean_step">Coordinate step per copy, mm:</span><input id="clean_step" type="number" step="0.5"></div>
  <div class="row"><span data-i18n="clean_axis">Prime axis:</span>
    <select id="clean_axis" style="padding:4px 6px;border-radius:5px;">
      <option value="X">X</option>
      <option value="Y">Y</option>
    </select>
  </div>
  <span class="hint" data-i18n="hint_clean">The prime routine is inserted before each copy. If the print body contains a {CLEAN} line, that line is replaced with the prime code for the current copy.</span>
</fieldset>

<fieldset>
  <legend data-i18n="section_loop">Firmware loop (infinite printing)</legend>
  <label class="check"><input id="loop" type="checkbox"> <span data-i18n="loop">Repeat the body in firmware (the body is written to the file once)</span></label>
  <div class="row"><span data-i18n="loop_firmware">Firmware:</span>
    <select id="loop_firmware" style="padding:4px 6px;border-radius:5px;">
      <option value="auto" data-i18n="loop_auto">Auto (detect from G-code)</option>
      <option value="klipper">Klipper — SDCARD_LOOP</option>
      <option value="marlin">Marlin — M808</option>
    </select>
  </div>
  <div class="row"><span data-i18n="loop_count">Repeats (0 = infinite):</span><input id="loop_count" type="number" min="0" max="1000000" step="1"></div>
  <span class="hint" data-i18n="hint_loop">Auto detection reads the ; gcode_flavor = ... line from OrcaSlicer's config block, then looks for firmware-specific commands. Klipper requires [sdcard_loop] and printing from SD or virtual SD; stop the loop with SDCARD_LOOP_DESIST. Marlin requires GCODE_REPEAT_MARKERS and printing from SD. With an infinite loop, the end G-code runs only after the loop is stopped.</span>
  <span class="hint" data-i18n="hint_loop_pause">Per-part pauses are not inserted in loop mode because the firmware does not expose the iteration number. Ejection, parking, beep, and priming still run on every iteration.</span>
</fieldset>

<fieldset>
  <legend data-i18n="section_pause">Long pause</legend>
  <label class="check"><input id="pause" type="checkbox"> <span data-i18n="pause">Pause between batches of parts</span></label>
  <div class="row"><span data-i18n="pause_every">Pause every N parts:</span><input id="pause_every" type="number" min="1" step="1"></div>
  <div class="row"><span data-i18n="pause_minutes">Pause duration, minutes:</span><input id="pause_minutes" type="number" min="1" step="1"></div>
  <label class="check"><input id="pause_rehome" type="checkbox"> <span data-i18n="pause_rehome">Run G28 after the pause</span></label>
  <label class="check"><input id="pause_reheat" type="checkbox"> <span data-i18n="pause_reheat">Restore temperatures after the pause</span></label>
  <span class="hint" data-i18n="hint_pause">Useful for printers whose motors or hotend overheat during long jobs. Position and heating are restored automatically after the pause.</span>
</fieldset>

<fieldset>
  <legend data-i18n="section_finish">Finish and extras</legend>
  <label class="check"><input id="turnoff" type="checkbox"> <span data-i18n="turnoff">Turn off heaters and motors at the very end</span></label>
  <label class="check"><input id="disable_autosave" type="checkbox"> <span data-i18n="disable_autosave">Disable power-loss recovery (M413 S0)</span></label>
  <label class="check"><input id="marker_comments" type="checkbox"> <span data-i18n="marker_comments">Add copy separator comments</span></label>
  <label class="check"><input id="header_note" type="checkbox"> <span data-i18n="header_note">Add a note to the G-code header</span></label>
  <details>
    <summary data-i18n="templates">G-code templates</summary>
    <span class="hint" data-i18n="hint_templates">Available placeholders: {Z} — lift, {X}/{Y} — prime coordinate, {COUNT} — number of copies, {INDEX} — copy number.</span>
    <label><span data-i18n="head_eject_label">Head ejection G-code (method “Head”)</span><br><textarea id="head_eject_gcode"></textarea></label>
    <label><span data-i18n="beam_eject_label">X-axis beam ejection G-code (method “X-axis beam”)</span><br><textarea id="beam_eject_gcode"></textarea></label>
    <label><span data-i18n="parking_label">Parking G-code</span><br><textarea id="parking_gcode"></textarea></label>
    <label><span data-i18n="beep_label">Beep G-code</span><br><textarea id="beep_gcode"></textarea></label>
    <label><span data-i18n="clean_label">Filament prime G-code</span><br><textarea id="clean_gcode"></textarea></label>
    <label><span data-i18n="pause_before_label">G-code before the pause</span><br><textarea id="pause_before_gcode"></textarea></label>
    <label><span data-i18n="pause_after_label">G-code after the pause (empty = restore automatically)</span><br><textarea id="pause_after_gcode"></textarea></label>
    <label><span data-i18n="turnoff_label">Full shutdown G-code</span><br><textarea id="turnoff_gcode"></textarea></label>
    <label><span data-i18n="final_label">Extra G-code at the very end</span><br><textarea id="final_gcode"></textarea></label>
  </details>
</fieldset>

<div class="buttons">
  <button id="save" class="primary" data-i18n="save">Save</button>
  <button id="restore" data-i18n="restore">Restore defaults</button>
  <span id="status"></span>
</div>

<script>
(function () {
  var DEFAULTS = __DEFAULTS__;
  var FIELDS = ["language","copies","drop","drop_method","z_lift","loop","loop_firmware","loop_count","parking","beep","clean","clean_start","clean_step","clean_axis","pause","pause_every","pause_minutes","pause_rehome","pause_reheat","turnoff","disable_autosave","marker_comments","header_note","head_eject_gcode","beam_eject_gcode","parking_gcode","beep_gcode","clean_gcode","pause_before_gcode","pause_after_gcode","turnoff_gcode","final_gcode"];
  var numberFields = ["copies","z_lift","loop_count","clean_start","clean_step","pause_every","pause_minutes"];
  var checkFields = ["drop","loop","parking","beep","clean","pause","pause_rehome","pause_reheat","turnoff","disable_autosave","marker_comments","header_note"];
  var textFields = ["head_eject_gcode","beam_eject_gcode","parking_gcode","beep_gcode","clean_gcode","pause_before_gcode","pause_after_gcode","turnoff_gcode","final_gcode"];
  var status = document.getElementById("status");
  var currentLanguage = "en";
  var statusKey = "";

  var I18N = {
    en: {
      title: "Task Multiplier",
      subtitle: "Prints one part N times in a single G-code file. The start and end blocks run once; only the print body is repeated.",
      language: "Language:",
      section_main: "Main",
      copies: "Parts per job:",
      drop: "Eject the last part",
      drop_method: "Ejection method:",
      method_head: "Head",
      method_beam: "X-axis beam",
      z_lift: "Z lift, mm:",
      hint_drop: "Intermediate copies are always ejected so the bed is clear. When this is off, only the last part stays on the bed. The ejection method is always active.",
      section_between: "Between copies",
      parking: "Park the head with G28 after every part",
      hint_parking: "Leave this off if head parking is not needed: the head stays in place after the part is ejected.",
      beep: "Beep after every part",
      clean: "Prime filament before every part",
      clean_start: "Prime start coordinate, mm:",
      clean_step: "Coordinate step per copy, mm:",
      clean_axis: "Prime axis:",
      hint_clean: "The prime routine is inserted before each copy. If the print body contains a {CLEAN} line, that line is replaced with the prime code for the current copy.",
      section_loop: "Firmware loop (infinite printing)",
      loop: "Repeat the body in firmware (the body is written to the file once)",
      loop_firmware: "Firmware:",
      loop_auto: "Auto (detect from G-code)",
      loop_count: "Repeats (0 = infinite):",
      hint_loop: "Auto detection reads the ; gcode_flavor = ... line from OrcaSlicer's config block, then looks for firmware-specific commands. Klipper requires [sdcard_loop] and printing from SD or virtual SD; stop the loop with SDCARD_LOOP_DESIST. Marlin requires GCODE_REPEAT_MARKERS and printing from SD. With an infinite loop, the end G-code runs only after the loop is stopped.",
      hint_loop_pause: "Per-part pauses are not inserted in loop mode because the firmware does not expose the iteration number. Ejection, parking, beep, and priming still run on every iteration.",
      section_pause: "Long pause",
      pause: "Pause between batches of parts",
      pause_every: "Pause every N parts:",
      pause_minutes: "Pause duration, minutes:",
      pause_rehome: "Run G28 after the pause",
      pause_reheat: "Restore temperatures after the pause",
      hint_pause: "Useful for printers whose motors or hotend overheat during long jobs. Position and heating are restored automatically after the pause.",
      section_finish: "Finish and extras",
      turnoff: "Turn off heaters and motors at the very end",
      disable_autosave: "Disable power-loss recovery (M413 S0)",
      marker_comments: "Add copy separator comments",
      header_note: "Add a note to the G-code header",
      templates: "G-code templates",
      hint_templates: "Available placeholders: {Z} — lift, {X}/{Y} — prime coordinate, {COUNT} — number of copies, {INDEX} — copy number.",
      head_eject_label: "Head ejection G-code (method “Head”)",
      beam_eject_label: "X-axis beam ejection G-code (method “X-axis beam”)",
      parking_label: "Parking G-code",
      beep_label: "Beep G-code",
      clean_label: "Filament prime G-code",
      pause_before_label: "G-code before the pause",
      pause_after_label: "G-code after the pause (empty = restore automatically)",
      turnoff_label: "Full shutdown G-code",
      final_label: "Extra G-code at the very end",
      save: "Save",
      restore: "Restore defaults",
      status_saved: "Saved",
      status_no_host: "No connection to OrcaSlicer: settings were not saved",
      status_restore_requested: "Defaults restore requested",
      status_defaults_shown: "Default values are shown",
      status_language: "Language changed. Click Save to keep it."
    },
    ru: {
      title: "Множитель задач",
      subtitle: "Одна деталь печатается N раз подряд в одном G-коде. Стартовый и конечный блоки выполняются один раз; повторяется только тело печати.",
      language: "Язык:",
      section_main: "Основное",
      copies: "Количество деталей за задание:",
      drop: "Сброс последней детали",
      drop_method: "Способ сброса:",
      method_head: "Головой",
      method_beam: "Балкой оси X",
      z_lift: "Высота подъёма оси Z, мм:",
      hint_drop: "Промежуточные копии сбрасываются всегда, чтобы освободить стол. Если выключено, на столе остаётся только последняя деталь. Способ сброса доступен всегда.",
      section_between: "Между копиями",
      parking: "Парковка головы командой G28 после каждой детали",
      hint_parking: "Если парковка головой не нужна, оставьте выключенным: головка останется на месте после сброса детали.",
      beep: "Звуковой сигнал после каждой детали",
      clean: "Протяжка филамента перед каждой деталью",
      clean_start: "Начальная координата протяжки, мм:",
      clean_step: "Шаг координаты на копию, мм:",
      clean_axis: "Ось протяжки:",
      hint_clean: "Протяжка вставляется перед каждой копией. Если в теле G-кода есть строка {CLEAN}, она заменяется кодом протяжки с нужной координатой.",
      section_loop: "Цикл (бесконечная печать)",
      loop: "Повторять тело силами прошивки (тело записывается в файл один раз)",
      loop_firmware: "Прошивка:",
      loop_auto: "Авто (определить по G-коду)",
      loop_count: "Повторов (0 = бесконечно):",
      hint_loop: "Автоопределение читает строку ; gcode_flavor = ... из конфиг-блока OrcaSlicer, а если её нет — ищет характерные команды прошивки. Klipper: нужен раздел [sdcard_loop] и печать с SD/виртуальной SD; остановка цикла — команда SDCARD_LOOP_DESIST. Marlin: нужен GCODE_REPEAT_MARKERS и печать с SD. При бесконечном цикле конечный G-код выполнится только после остановки цикла.",
      hint_loop_pause: "В циклическом режиме паузы по счётчику деталей не вставляются: прошивка не сообщает номер итерации. Сброс, парковка, сигнал и протяжка выполняются в каждой итерации.",
      section_pause: "Длинная пауза",
      pause: "Делать паузу между партиями деталей",
      pause_every: "Пауза через каждые, деталей:",
      pause_minutes: "Длительность паузы, минут:",
      pause_rehome: "Выполнять G28 после паузы",
      pause_reheat: "Восстанавливать температуры после паузы",
      hint_pause: "Полезно для принтеров, у которых перегреваются моторы или голова при длительной работе. После паузы позиция и нагрев восстанавливаются автоматически.",
      section_finish: "Завершение и прочее",
      turnoff: "Выключить нагрев и моторы в самом конце",
      disable_autosave: "Отключить автосохранение позиции (M413 S0)",
      marker_comments: "Добавлять комментарии-разделители копий",
      header_note: "Добавлять пометку в заголовок G-кода",
      templates: "Шаблоны G-кода",
      hint_templates: "Доступные подстановки: {Z} — подъём, {X}/{Y} — координата протяжки, {COUNT} — число копий, {INDEX} — номер копии.",
      head_eject_label: "Код сброса головой (способ «Головой»)",
      beam_eject_label: "Код сброса балкой оси X (способ «Балкой оси X»)",
      parking_label: "Код парковки",
      beep_label: "Код звукового сигнала",
      clean_label: "Код протяжки филамента",
      pause_before_label: "Код перед паузой",
      pause_after_label: "Код после паузы (пусто = восстановить самому)",
      turnoff_label: "Код полного выключения",
      final_label: "Дополнительный код в самом конце",
      save: "Сохранить",
      restore: "Сбросить настройки",
      status_saved: "Сохранено",
      status_no_host: "Нет связи с OrcaSlicer: настройки не сохранены",
      status_restore_requested: "Запрошен сброс настроек",
      status_defaults_shown: "Показаны значения по умолчанию",
      status_language: "Язык изменён. Нажмите «Сохранить», чтобы запомнить."
    }
  };

  function t(key) {
    var table = I18N[currentLanguage] || I18N.en;
    if (Object.prototype.hasOwnProperty.call(table, key)) return table[key];
    return Object.prototype.hasOwnProperty.call(I18N.en, key) ? I18N.en[key] : key;
  }

  function setStatus(key) {
    statusKey = key || "";
    status.textContent = statusKey ? t(statusKey) : "";
  }

  function applyLanguage(lang) {
    currentLanguage = (lang === "ru") ? "ru" : "en";
    document.documentElement.lang = currentLanguage;
    var nodes = document.querySelectorAll("[data-i18n]");
    for (var i = 0; i < nodes.length; i++) {
      nodes[i].textContent = t(nodes[i].getAttribute("data-i18n"));
    }
    if (statusKey) status.textContent = t(statusKey);
  }

  function syncLoopState() {
    var loop = document.getElementById("loop").checked;
    document.getElementById("copies").disabled = loop;
    document.getElementById("pause").disabled = loop;
    var pauseOn = document.getElementById("pause").checked && !loop;
    ["pause_every","pause_minutes","pause_rehome","pause_reheat"].forEach(function (key) {
      document.getElementById(key).disabled = !pauseOn;
    });
  }

  function mergeConfig(source) {
    if (typeof source === "string") {
      try { source = JSON.parse(source); } catch (err) { source = {}; }
    }
    if (!source || typeof source !== "object") source = {};
    var cfg = {};
    FIELDS.forEach(function (key) {
      cfg[key] = (source[key] !== undefined && source[key] !== null) ? source[key] : DEFAULTS[key];
    });

    // The host passes the stored config as-is, so migrate the old 1.0/1.1
    // keys here as well. Otherwise an old "eject_mode" would show as "off".
    var legacy = String(source.eject_mode || "").toLowerCase();
    if (source.drop === undefined || source.drop === null) {
      if (legacy) cfg.drop = legacy !== "none";
    }
    if (source.drop_method === undefined || source.drop_method === null) {
      cfg.drop_method = legacy === "head" ? "head" : "beam";
    }
    if (source.beam_eject_gcode === undefined || source.beam_eject_gcode === null) {
      if (source.bed_eject_gcode !== undefined && source.bed_eject_gcode !== null) {
        cfg.beam_eject_gcode = source.bed_eject_gcode;
      } else if (source.drop_gcode !== undefined && source.drop_gcode !== null) {
        cfg.beam_eject_gcode = source.drop_gcode;
      }
    }
    return cfg;
  }

  function readConfig() {
    var source = {};
    try {
      if (window.orca && typeof window.orca.getConfig === "function") {
        source = window.orca.getConfig() || {};
      }
    } catch (err) {
      source = {};
    }
    return mergeConfig(source);
  }

  function writeConfig(cfg) {
    numberFields.forEach(function (key) {
      var el = document.getElementById(key);
      var n = parseFloat(cfg[key]);
      el.value = isFinite(n) ? n : DEFAULTS[key];
    });
    checkFields.forEach(function (key) { document.getElementById(key).checked = !!cfg[key]; });
    var method = document.getElementById("drop_method");
    method.value = ["head","beam"].indexOf(cfg.drop_method) >= 0 ? cfg.drop_method : "beam";
    var firmware = document.getElementById("loop_firmware");
    firmware.value = ["auto","klipper","marlin"].indexOf(cfg.loop_firmware) >= 0 ? cfg.loop_firmware : "auto";
    document.getElementById("clean_axis").value = (cfg.clean_axis === "Y") ? "Y" : "X";
    var language = document.getElementById("language");
    language.value = (cfg.language === "ru") ? "ru" : "en";
    textFields.forEach(function (key) {
      document.getElementById(key).value = cfg[key] || "";
    });
    syncLoopState();
    applyLanguage(language.value);
  }

  function collectConfig() {
    var cfg = {};
    numberFields.forEach(function (key) {
      var n = parseFloat(document.getElementById(key).value);
      cfg[key] = isFinite(n) ? n : DEFAULTS[key];
    });
    checkFields.forEach(function (key) { cfg[key] = document.getElementById(key).checked; });
    cfg.drop_method = document.getElementById("drop_method").value === "head" ? "head" : "beam";
    cfg.loop_firmware = document.getElementById("loop_firmware").value;
    cfg.clean_axis = document.getElementById("clean_axis").value === "Y" ? "Y" : "X";
    cfg.language = document.getElementById("language").value === "ru" ? "ru" : "en";
    textFields.forEach(function (key) {
      cfg[key] = document.getElementById(key).value;
    });
    return cfg;
  }

  document.getElementById("language").addEventListener("change", function () {
    applyLanguage(this.value);
    setStatus("status_language");
  });
  document.getElementById("loop").addEventListener("change", syncLoopState);
  document.getElementById("pause").addEventListener("change", syncLoopState);

  document.getElementById("save").addEventListener("click", function () {
    var cfg = collectConfig();
    if (cfg.copies < 1) { cfg.copies = 1; document.getElementById("copies").value = 1; }
    if (!(cfg.loop_count >= 0)) { cfg.loop_count = 0; document.getElementById("loop_count").value = 0; }
    if (window.orca && typeof window.orca.saveConfig === "function") {
      window.orca.saveConfig(cfg);
      setStatus("status_saved");
    } else {
      setStatus("status_no_host");
    }
  });

  document.getElementById("restore").addEventListener("click", function () {
    if (window.orca && typeof window.orca.restoreDefaults === "function") {
      window.orca.restoreDefaults();
      setStatus("status_restore_requested");
    } else {
      writeConfig(DEFAULTS);
      setStatus("status_defaults_shown");
    }
  });

  // Show the stored config immediately. onConfig() is then used for live host
  // updates (for example after "Restore defaults"), but the page must not
  // depend on it to initialize: getConfig() always has the current values.
  writeConfig(readConfig());
  if (window.orca && typeof window.orca.onConfig === "function") {
    window.orca.onConfig(function (cfg) {
      writeConfig(mergeConfig(cfg));
      if (window.orca.getContext && window.orca.getContext().readOnly) {
        document.body.classList.add("readonly");
      }
    });
  }
})();
</script>
<p class="note" style="margin-top:18px;opacity:.7;font-size:12px;">by:adilo321 · AI-assisted · Task Multiplier 1.0.0 · MIT</p>
</body>
</html>
"""
    return html.replace("__DEFAULTS__", json.dumps(DEFAULTS, ensure_ascii=False))


if orca is not None:

    class TaskMultiplier(orca.slicing.SlicingPipelineCapabilityBase):
        """Slicing-pipeline capability: repeat the print body N times."""

        def get_name(self):
            return "Task Multiplier"

        def get_default_config(self):
            return dict(DEFAULTS)

        def has_config_ui(self):
            return True

        def get_config_ui(self):
            return _config_ui_html()

        def execute(self, ctx):
            # Geometry steps are irrelevant: only the final G-code is edited.
            if ctx.step != orca.slicing.Step.psGCodePostProcess:
                return orca.ExecutionResult.success()

            if not ctx.gcode_path:
                return orca.ExecutionResult.success(
                    "Task Multiplier: no G-code path at the post-process step"
                )

            cfg = load_plugin_config(self)
            try:
                stats = transform_file(ctx.gcode_path, cfg)
            except Exception as exc:  # surface a clear slicing error
                return orca.ExecutionResult.failure(
                    "task-multiplier",
                    "Task Multiplier: failed to process G-code: %s" % exc,
                )

            name = ctx.output_name or os.path.basename(ctx.gcode_path)
            if stats.get("already_processed"):
                message = (
                    "Task Multiplier: '%s' is already multiplied; skipped to avoid "
                    "duplicating the job." % name
                )
            elif stats["loop"]:
                repeats = (
                    "infinite" if stats["loop_count"] == 0 else "%d repeats" % stats["loop_count"]
                )
                source = stats.get("loop_firmware_source") or "selected manually"
                message = (
                    "Task Multiplier: firmware loop on %s (%s, %s) for '%s' "
                    "(%d body lines); start/end G-code written once."
                    % (
                        stats["loop_firmware"],
                        source,
                        repeats,
                        name,
                        stats["body_lines"],
                    )
                )
            else:
                last_part = (
                    "ejected (%s)" % stats["drop_method"]
                    if stats["drop"]
                    else "stays on the bed"
                )
                message = (
                    "Task Multiplier: %d copies of '%s' (%d body lines each, ejection: %s; "
                    "last part: %s); start/end G-code written once."
                    % (
                        stats["copies"],
                        name,
                        stats["body_lines"],
                        stats["drop_method"],
                        last_part,
                    )
                )
            return orca.ExecutionResult.success(message)


    @orca.plugin
    class TaskMultiplierPackage(orca.base):
        def register_capabilities(self):
            orca.register_capability(TaskMultiplier)
