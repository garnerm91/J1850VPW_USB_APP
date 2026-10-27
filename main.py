"""J1850 VPW USB console - entry point.

Usage:  python main.py [path/to/settings.json]
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import dearpygui.dearpygui as dpg

from config_manager import load_presets_safe, load_settings, resolve_path
from gui_layout import MainWindow

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main() -> int:
    app_dir = Path(__file__).resolve().parent
    settings_path = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else app_dir / "settings.json"
    base_dir = settings_path.parent

    # Both loaders are fail-safe: worst case we get defaults + warnings.
    settings, warnings = load_settings(settings_path)
    presets_file = resolve_path(settings["presets_path"], base_dir) if settings["presets_path"] else None
    bundle = load_presets_safe(presets_file)
    warnings += bundle.warnings
    for w in warnings:
        logging.warning(w)

    dpg.create_context()
    try:
        win = MainWindow(settings, settings_path, bundle, warnings)
        win.show()
        while dpg.is_dearpygui_running():
            win.update()                      # drain serial queue, autoscroll
            dpg.render_dearpygui_frame()
        win.shutdown()
    finally:
        dpg.destroy_context()
    return 0


if __name__ == "__main__":
    sys.exit(main())
