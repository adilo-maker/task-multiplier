# -*- coding: utf-8 -*-
"""Local tests for the Task Multiplier transformation core.

Run from the repository root:
    py tests\\test_transform.py
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PLUGIN_PATH = os.path.join(ROOT, "task_multiplier.py")
DEFAULT_SAMPLE = os.path.join(ROOT, "testdata", "sample_plate.gcode")
SAMPLE = os.environ.get("ORCA_SAMPLE_GCODE", DEFAULT_SAMPLE)


def load_plugin_module():
    spec = importlib.util.spec_from_file_location("task_multiplier_under_test", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def count(text, needle):
    return text.count(needle)


def count_lines(text, needle):
    return sum(1 for line in text.splitlines() if line.strip() == needle)


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as handle:
        return handle.read()


def write_text(path, text):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def transformed(tm, tmpdir, name, text, cfg):
    target = os.path.join(tmpdir, name)
    write_text(target, text)
    stats = tm.transform_file(target, cfg)
    return read_text(target), stats


def main():
    tm = load_plugin_module()

    if not os.path.isfile(SAMPLE):
        print("FAIL: sample G-code not found:", SAMPLE)
        return 1

    original = read_text(SAMPLE)
    original_lines = original.splitlines(keepends=True)
    original_layout = tm.analyze_gcode(original_lines)
    body_layers = sum(
        1
        for line in original_lines[original_layout.body_start : original_layout.body_end]
        if ";LAYER_CHANGE" in line
    )
    suffix_layers = sum(
        1 for line in original_lines[original_layout.suffix_start :] if ";LAYER_CHANGE" in line
    )
    single_layers = body_layers + suffix_layers

    tmpdir = tempfile.mkdtemp(prefix="task_multiplier_test_")
    try:
        # ------------------------------------------------------------------
        # 1. Normal mode: N copies, all inter-copy routines.
        # ------------------------------------------------------------------
        cfg = {
            "copies": 3,
            "drop": True,
            "drop_method": "beam",
            "z_lift": 30,
            "parking": True,
            "beep": True,
            "clean": True,
            "clean_start": 1,
            "clean_step": 1,
            "clean_axis": "X",
            "pause": True,
            "pause_every": 2,
            "pause_minutes": 30,
            "disable_autosave": True,
        }
        result, stats = transformed(tm, tmpdir, "plate_1.gcode", original, cfg)

        check(stats["copies"] == 3, "stats.copies")
        check(stats["body_lines"] > 0, "stats.body_lines")

        # Structural markers must remain unique. Use whole lines for markers
        # that also appear as escaped text inside CONFIG_BLOCK comments.
        for marker in (
            "; HEADER_BLOCK_START",
            "; EXECUTABLE_BLOCK_START",
            "; EXECUTABLE_BLOCK_END",
            "; CONFIG_BLOCK_START",
            "G28 ; home all",
            "M104 S0 ; turn off hotend",
        ):
            check(
                count_lines(result, marker) == 1,
                "marker repeated: %s (%d)" % (marker, count_lines(result, marker)),
            )

        check(
            count(result, ";LAYER_CHANGE") == body_layers * 3 + suffix_layers,
            "layer count: expected %d, got %d"
            % (body_layers * 3 + suffix_layers, count(result, ";LAYER_CHANGE")),
        )
        check(count(result, "TASK_MULTIPLIER: copy ") == 3, "copy markers")
        check(count(result, "TASK_MULTIPLIER: start of repeated section") == 1, "section start marker")
        check(count(result, "TASK_MULTIPLIER: end of repeated section") == 1, "section end marker")

        # Options.
        check(count(result, "G0 Z30 F3600") == 3, "ejection should run after every copy")
        check(count(result, "G28 ;Home") == 3, "parking code should run after every copy")
        check(count(result, "M300 S880 P300") == 3, "beep should run after every copy")
        check(count(result, "G1 X1 Y20 Z0.28 F5000.0") == 1, "clean copy 1")
        check(count(result, "G1 X2 Y20 Z0.28 F5000.0") == 1, "clean copy 2")
        check(count(result, "G1 X3 Y20 Z0.28 F5000.0") == 1, "clean copy 3")
        check(count(result, "G4 S1800") == 1, "one pause after copy 2 of 3")
        check(count(result, "M413 S0") == 1, "autosave disabled once")
        check(count(result, "M109 S215") == 2, "pause reheat wait command exactly once")

        # No unexpanded placeholders.
        for token in ("{CLEAN}", "{Z}", "{X}", "{Y}", "{COUNT}", "{INDEX}"):
            check(token not in result, "unexpanded token: %s" % token)

        print("PASS: 3 copies, all inter-copy routines, start/end once")

        # ------------------------------------------------------------------
        # 2. Single copy must not add multiplication routines.
        # ------------------------------------------------------------------
        single, _ = transformed(
            tm,
            tmpdir,
            "single.gcode",
            original,
            {
                "copies": 1,
                "drop": True,
                "drop_method": "beam",
                "beep": True,
                "clean": True,
                "pause": True,
                "pause_every": 1,
            },
        )
        check(count(single, ";LAYER_CHANGE") == single_layers, "single copy layer count")
        check(count(single, "G0 Z30 F3600") == 1, "single copy still ejects at the end")
        check(count(single, "G4 S1800") == 0, "no pause with a single copy")
        check(count(single, "G1 X1 Y20 Z0.28 F5000.0") == 1, "single clean line")
        print("PASS: single copy keeps one body and one final ejection")

        # ------------------------------------------------------------------
        # 3. Pause schedule: 41 copies, pause every 40 -> exactly one pause.
        # ------------------------------------------------------------------
        many, _ = transformed(tm, tmpdir, "many.gcode", original, {"copies": 41, "pause": True, "pause_every": 40})
        check(count(many, "G4 S1800") == 1, "pause every 40 -> one pause in 41 copies")
        check(count(many, ";LAYER_CHANGE") == body_layers * 41 + suffix_layers, "41 copies layer count")
        print("PASS: pause schedule (41 copies, every 40)")

        # ------------------------------------------------------------------
        # 4. Last-part ejection: intermediate copies always eject, the final
        #    copy is controlled by the "drop" switch. Method is always active.
        # ------------------------------------------------------------------
        none, stats = transformed(tm, tmpdir, "eject_none.gcode", original, {"copies": 2})
        check(count(none, "G0 Z30 F3600") == 1, "drop off: intermediate copy still ejects")
        check(count(none, "G0 X220 F12000") == 0, "drop off: no head sweep")
        check(count(none, "G0 Y0 F14000") == 1, "drop off: intermediate beam ejection only")
        check(stats["drop"] is False, "drop off stats")
        check(stats["drop_method"] == "beam", "drop method is kept while the last part stays")

        disabled, _ = transformed(
            tm,
            tmpdir,
            "eject_disabled.gcode",
            original,
            {"copies": 2, "drop": False, "drop_method": "head"},
        )
        check(count(disabled, "G0 X220 F12000") == 1, "drop off with head selected: intermediate sweep only")

        single_stays, _ = transformed(
            tm,
            tmpdir,
            "eject_single_stays.gcode",
            original,
            {"copies": 1, "drop": False, "drop_method": "head"},
        )
        check(count(single_stays, "G0 X220 F12000") == 0, "single part with drop off stays on the bed")

        single_ejects, _ = transformed(
            tm,
            tmpdir,
            "eject_single_ejects.gcode",
            original,
            {"copies": 1, "drop": True, "drop_method": "head"},
        )
        check(count(single_ejects, "G0 X220 F12000") == 1, "single part with drop on is ejected")

        head, stats = transformed(
            tm,
            tmpdir,
            "eject_head.gcode",
            original,
            {"copies": 2, "drop": True, "drop_method": "head"},
        )
        check(count(head, "G0 X220 F12000") == 2, "head: one sweep per copy")
        check(count(head, "G0 Y0 F14000") == 0, "head: no beam jerk")
        check(stats["drop"] is True and stats["drop_method"] == "head", "head stats")

        beam, stats = transformed(
            tm,
            tmpdir,
            "eject_beam.gcode",
            original,
            {"copies": 2, "drop": True, "drop_method": "beam"},
        )
        check(count(beam, "G0 Y0 F14000") == 2, "beam: one jerk per copy")
        check(count(beam, "G0 X220 F12000") == 0, "beam: no head sweep")
        check(stats["drop"] is True and stats["drop_method"] == "beam", "beam stats")

        # 1.0 config: drop + drop_gcode. 1.1 config: eject_mode values.
        legacy_v1, stats = transformed(
            tm, tmpdir, "eject_legacy_v1.gcode", original, {"copies": 2, "drop": True}
        )
        check(count(legacy_v1, "G0 Y0 F14000") == 2, "1.0 drop=true maps to beam")
        check(stats["drop_method"] == "beam", "1.0 migration method")

        legacy_head, stats = transformed(
            tm, tmpdir, "eject_legacy_head.gcode", original, {"copies": 2, "eject_mode": "head"}
        )
        check(count(legacy_head, "G0 X220 F12000") == 2, "1.1 eject_mode=head maps to head")
        check(stats["drop"] is True and stats["drop_method"] == "head", "1.1 head migration")

        legacy_bed, stats = transformed(
            tm, tmpdir, "eject_legacy_bed.gcode", original, {"copies": 2, "eject_mode": "bed"}
        )
        check(count(legacy_bed, "G0 Y0 F14000") == 2, "1.1 eject_mode=bed maps to beam")
        check(stats["drop_method"] == "beam", "1.1 bed migration")

        legacy_custom, _ = transformed(
            tm,
            tmpdir,
            "eject_legacy_custom.gcode",
            original,
            {"copies": 1, "eject_mode": "custom", "drop_gcode": "G0 Y123 F9999"},
        )
        check(count(legacy_custom, "G0 Y123 F9999") == 1, "1.1 custom drop_gcode is preserved")

        migrated = tm.normalize_config(
            {"eject_mode": "bed", "bed_eject_gcode": "G0 Y777 F9999"}
        )
        check(migrated["drop"] is True, "bed mode migrates to drop on")
        check(migrated["drop_method"] == "beam", "bed mode migrates to beam")
        check(migrated["beam_eject_gcode"] == "G0 Y777 F9999", "bed template migrates")
        check("eject_mode" not in migrated, "legacy eject_mode removed")
        check("drop_gcode" not in migrated, "legacy drop_gcode removed")
        check(migrated["language"] == "en", "language defaults to English")
        check(tm.normalize_config({"language": "ru"})["language"] == "ru", "Russian is kept")
        check(tm.normalize_config({"language": "de"})["language"] == "en", "unknown language falls back to English")
        print("PASS: last-part ejection (head / beam / stays), language default, legacy migration")

        # ------------------------------------------------------------------
        # 5. Klipper infinite loop: body once, firmware loop, end block after.
        # ------------------------------------------------------------------
        klipper, stats = transformed(
            tm,
            tmpdir,
            "loop_klipper.gcode",
            original,
            {
                "copies": 3,
                "loop": True,
                "loop_firmware": "klipper",
                "loop_count": 0,
                "drop": True,
                "drop_method": "beam",
                "beep": True,
                "pause": True,
                "pause_every": 1,
                "pause_minutes": 5,
            },
        )
        check(stats["loop_firmware"] == "klipper", "klipper loop stats")
        check(count_lines(klipper, "SDCARD_LOOP_BEGIN COUNT=0") == 1, "klipper infinite begin once")
        check(count_lines(klipper, "SDCARD_LOOP_END") == 1, "klipper loop end once")
        check(count(klipper, ";LAYER_CHANGE") == single_layers, "klipper loop body written once")
        check(count(klipper, "TASK_MULTIPLIER: copy ") == 1, "klipper loop: one copy marker")
        check(count(klipper, "G0 Y0 F14000") == 1, "klipper loop: ejection once inside loop")
        check(count(klipper, "M300 S880 P300") == 1, "klipper loop: beep once inside loop")
        check(count(klipper, "G4 S300") == 0, "klipper loop: periodic pause skipped")
        check(count_lines(klipper, "; EXECUTABLE_BLOCK_END") == 1, "klipper loop: end block once")
        check(klipper.index("SDCARD_LOOP_END") < klipper.index("; EXECUTABLE_BLOCK_END"), "end block after loop")
        print("PASS: Klipper infinite loop (SDCARD_LOOP COUNT=0)")

        # ------------------------------------------------------------------
        # 6. Marlin finite loop: M808 L5 ... M808, end block after.
        # ------------------------------------------------------------------
        marlin, stats = transformed(
            tm,
            tmpdir,
            "loop_marlin.gcode",
            original,
            {
                "copies": 3,
                "loop": True,
                "loop_firmware": "marlin",
                "loop_count": 5,
                "drop": True,
                "drop_method": "head",
            },
        )
        check(stats["loop_firmware"] == "marlin", "marlin loop stats")
        check(count_lines(marlin, "M808 L5") == 1, "marlin loop marker once")
        check(count_lines(marlin, "M808") == 1, "marlin loop terminator once")
        check(count(marlin, ";LAYER_CHANGE") == single_layers, "marlin loop body written once")
        check(count(marlin, "G0 X220 F12000") == 1, "marlin loop: head ejection once")
        check(marlin.index("M808\n") < marlin.index("; EXECUTABLE_BLOCK_END"), "marlin end block after loop")
        print("PASS: Marlin finite loop (M808 L5)")

        # ------------------------------------------------------------------
        # 7. Auto-detection: sample has "; gcode_flavor = marlin2".
        # ------------------------------------------------------------------
        auto, stats = transformed(
            tm,
            tmpdir,
            "loop_auto.gcode",
            original,
            {"copies": 3, "loop": True, "loop_firmware": "auto", "loop_count": 0},
        )
        check(stats["loop_firmware"] == "marlin", "auto-detect flavor")
        check(stats["loop_firmware_detected"] is True, "auto-detect flag")
        check(count_lines(auto, "M808 L0") == 1, "auto-detect emits M808 L0")
        check(count(auto, "SDCARD_LOOP_BEGIN") == 0, "auto-detect does not emit Klipper loop")
        print("PASS: firmware auto-detection (marlin2)")

        # ------------------------------------------------------------------
        # 8. Auto-detection fallback: strip gcode_flavor -> assume Klipper.
        # ------------------------------------------------------------------
        stripped = "".join(
            line for line in original_lines if "gcode_flavor" not in line
        )
        fallback, stats = transformed(
            tm,
            tmpdir,
            "loop_fallback.gcode",
            stripped,
            {"copies": 2, "loop": True, "loop_firmware": "auto", "loop_count": 0},
        )
        check(stats["loop_firmware"] == "klipper", "fallback firmware")
        check(stats["loop_firmware_detected"] is False, "fallback detection flag")
        check("WARNING: firmware auto-detect failed" in fallback, "fallback warning")
        check(count_lines(fallback, "SDCARD_LOOP_BEGIN COUNT=0") == 1, "fallback loop command")
        print("PASS: auto-detection fallback (no gcode_flavor -> Klipper with warning)")

        # ------------------------------------------------------------------
        # 9. Idempotence guard: transforming twice must not duplicate the job.
        # ------------------------------------------------------------------
        twice, stats = transformed(
            tm,
            tmpdir,
            "twice.gcode",
            result,
            {"copies": 7},
        )
        check(stats["already_processed"] is True, "idempotence statistics")
        check(twice == result, "already-processed file must stay unchanged")
        print("PASS: idempotence guard")

        return 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print("FAIL:", exc)
        sys.exit(1)
