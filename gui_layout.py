"""DearPyGui layout, callbacks and console rendering.

All DPG calls happen on the main thread. The serial thread only talks to us
through `self.events` (a queue.Queue) which update() drains once per frame.
"""
from __future__ import annotations

import logging
import queue
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import dearpygui.dearpygui as dpg

import about_dialog

from config_manager import (ConfigError, Preset, PresetBundle, hex_str, load_presets,
                            parse_hex, resolve_path, save_settings)
from serial_handler import CMD_RX_FRAME, CMD_RX_MODE, CMD_SEND_FRAME, SerialEvent, SerialHandler

log = logging.getLogger(__name__)

PALETTES = {
    "dark": dict(tx=(100, 200, 255, 255), rx=(220, 220, 220, 255), ack=(110, 220, 120, 255),
                 nack=(255, 110, 110, 255), info=(150, 150, 150, 255),
                 warn=(255, 200, 80, 255), error=(255, 90, 90, 255)),
    "light": dict(tx=(0, 90, 170, 255), rx=(30, 30, 30, 255), ack=(0, 130, 40, 255),
                  nack=(190, 30, 30, 255), info=(110, 110, 110, 255),
                  warn=(170, 100, 0, 255), error=(190, 20, 20, 255)),
}
PRESETS_PER_ROW = 4
KNOWN_CMDS = (CMD_RX_FRAME, CMD_SEND_FRAME)


@dataclass
class LogEntry:
    """One console line, stored raw so display toggles can re-render history."""
    ts: float
    direction: str
    color_key: str = "info"
    text: str = ""                     # plain-text entries
    data: Optional[bytes] = None       # frame entries (None => text entry)
    cmd: Optional[int] = None
    label: str = ""                    # decoded rule label / preset name
    color: Optional[tuple] = None      # rule colour override


class MainWindow:
    def __init__(self, settings: Dict[str, Any], settings_path: Path,
                 bundle: PresetBundle, startup_warnings: List[str]) -> None:
        self.settings = settings
        self.settings_path = settings_path
        self.base_dir = settings_path.parent
        self.bundle = bundle
        self.startup_warnings = list(startup_warnings)

        self.events: "queue.Queue[SerialEvent]" = queue.Queue()
        self.serial = SerialHandler(self.events)
        self.colors = PALETTES[settings["theme"]]

        self.display = dict(settings["display"])          # runtime toggles (Settings menu)
        self.entries: deque = deque(maxlen=200_000)       # raw log history
        self._console_items: deque = deque()              # DPG ids, capped
        self._scroll_pending = False
        self._port_map: Dict[str, str] = {}               # combo label -> device

        self._setup_font()
        self._setup_theme()
        self._build()

    # ================================================================ setup
    def _setup_font(self) -> None:
        f = self.settings["font"]
        if not f["path"]:
            return
        path = resolve_path(f["path"], self.base_dir)
        if not path.is_file():
            self.startup_warnings.append(f"font not found ({path}); using default font")
            return
        try:
            with dpg.font_registry():
                font = dpg.add_font(str(path), float(f["size"]))
            dpg.bind_font(font)
        except Exception as exc:  # noqa: BLE001
            self.startup_warnings.append(f"could not load font {path}: {exc}")

    def _setup_theme(self) -> None:
        with dpg.theme() as theme:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 4)
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 8, 6)
                if self.settings["theme"] == "light":
                    for col, rgb in {
                        dpg.mvThemeCol_Text: (20, 20, 20), dpg.mvThemeCol_WindowBg: (243, 243, 243),
                        dpg.mvThemeCol_ChildBg: (255, 255, 255), dpg.mvThemeCol_PopupBg: (250, 250, 250),
                        dpg.mvThemeCol_FrameBg: (225, 228, 232), dpg.mvThemeCol_Button: (205, 215, 232),
                        dpg.mvThemeCol_ButtonHovered: (180, 200, 232), dpg.mvThemeCol_ButtonActive: (150, 180, 225),
                        dpg.mvThemeCol_MenuBarBg: (225, 225, 225), dpg.mvThemeCol_Header: (200, 215, 235),
                        dpg.mvThemeCol_HeaderHovered: (180, 200, 232), dpg.mvThemeCol_Border: (190, 190, 190),
                        dpg.mvThemeCol_CheckMark: (0, 100, 200),
                    }.items():
                        dpg.add_theme_color(col, (*rgb, 255))
        dpg.bind_theme(theme)

    def _build(self) -> None:
        # ---- file dialogs (hidden until requested)
        with dpg.file_dialog(directory_selector=False, show=False, modal=True, width=720, height=440,
                             callback=self._on_presets_file) as self.dlg_load:
            dpg.add_file_extension(".json", color=(150, 255, 150, 255))
            dpg.add_file_extension(".*")
        with dpg.file_dialog(directory_selector=False, show=False, modal=True, width=720, height=440,
                             default_filename="console_log", callback=self._on_save_file) as self.dlg_save:
            dpg.add_file_extension(".txt", color=(150, 255, 150, 255))
            dpg.add_file_extension(".log")

        # ---- menu bar
        with dpg.viewport_menu_bar():
            with dpg.menu(label="File"):
                dpg.add_menu_item(label="Load Presets JSON...", callback=lambda: dpg.show_item(self.dlg_load))
                dpg.add_menu_item(label="Save Console Log...", callback=lambda: dpg.show_item(self.dlg_save))
                dpg.add_separator()
                dpg.add_menu_item(label="Exit", callback=lambda: dpg.stop_dearpygui())
            with dpg.menu(label="Connection"):
                self.port_combo = dpg.add_combo([], label="Port", width=260, callback=self._on_port_selected)
                dpg.add_menu_item(label="Refresh Ports", callback=lambda: self.refresh_ports())
                self.connect_item = dpg.add_menu_item(label="Connect", callback=lambda: self.toggle_connection())
                dpg.add_separator()
                self.rx_item = dpg.add_menu_item(label="RX Mode (0x01)", check=True,
                                                 callback=self._on_rx_toggle)
            with dpg.menu(label="Settings"):
                for key, label in (("trim_crc", "Trim CRC from RX frames"),
                                   ("show_timestamp", "Show Timestamp"),
                                   ("show_command", "Show Command (CMD byte)")):
                    dpg.add_menu_item(label=label, check=True, default_value=self.display[key],
                                      callback=self._on_display_toggle, user_data=key)
                dpg.add_separator()
                dpg.add_menu_item(label="Save Current as Defaults",
                                  callback=lambda: self.save_display_defaults())
            with dpg.menu(label="Help"):
                dpg.add_menu_item(label="About",  callback=about_dialog.show_about)

        # ---- main window
        with dpg.window(tag="main_window") as self.window:
            dpg.add_spacer(height=20)
            with dpg.group(horizontal=True):
                self.status_text = dpg.add_text("Disconnected")
                dpg.add_spacer(width=20)
                self.autoscroll_cb = dpg.add_checkbox(label="Auto-scroll",
                                                      default_value=self.settings["console"]["auto_scroll"])
                dpg.add_button(label="Clear", callback=lambda: self.clear_console())

            with dpg.child_window(height=-215, border=True) as self.console:
                pass

            dpg.add_separator()
            dpg.add_text("Preset Frames")
            with dpg.child_window(height=100, border=True) as self.preset_area:
                pass

            dpg.add_text("Manual Frame (hex bytes, CRC added by firmware)")
            with dpg.group(horizontal=True):
                self.manual_input = dpg.add_input_text(
                    hint="e.g. 6C 10 40 01 02", width=-90, on_enter=True,   # Enter => send
                    callback=lambda: self.on_manual_send())
                dpg.add_button(label="Send", width=80, callback=lambda: self.on_manual_send())

        self.refresh_ports(select=self.settings["serial"]["port"])
        self._update_connection_ui()
        self.rebuild_preset_buttons()

        for w in self.startup_warnings:
            self.log_text("WARN", w, "warn")
        if self.bundle.presets or self.bundle.rules:
            self.log_text("INFO", f"Loaded {len(self.bundle.presets)} presets, "
                                  f"{len(self.bundle.rules)} rules from {self.bundle.source}", "info")

    def show(self) -> None:
        w = self.settings["window"]
        dpg.create_viewport(title=w["title"], width=w["width"], height=w["height"])
        dpg.setup_dearpygui()
        dpg.show_viewport()
        dpg.set_primary_window(self.window, True)
        dpg.focus_item(self.manual_input)

    # ============================================================ main loop
    def update(self) -> None:
        if self._scroll_pending:
            self._scroll_pending = False
            if dpg.get_value(self.autoscroll_cb):
                dpg.set_y_scroll(self.console, -1.0)
        for _ in range(500):                      # bounded work per frame
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                break
            self._handle_event(ev)

    def shutdown(self) -> None:
        self.serial.disconnect()
        try:  # remember window size + last port (best effort)
            self.settings["window"]["width"] = dpg.get_viewport_client_width()
            self.settings["window"]["height"] = dpg.get_viewport_client_height()
            save_settings(self.settings, self.settings_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not save settings: %s", exc)

    # ============================================================== console
    def log_text(self, direction: str, text: str, color_key: str = "info",
                 ts: Optional[float] = None) -> None:
        self._add_entry(LogEntry(ts or time.time(), direction, color_key, text=text))

    def log_frame(self, direction: str, data: bytes, ts: Optional[float] = None,
                  cmd: Optional[int] = None, note: str = "") -> None:
        rule = self.bundle.decode(data)   # always decoded on the FULL frame (incl. CRC),
        label = " ".join(x for x in (rule.label if rule else "", note) if x)  # so rules
        self._add_entry(LogEntry(ts or time.time(), direction,        # don't change with toggles
                                 "tx" if direction == "TX" else "rx",
                                 data=data, cmd=cmd, label=label,
                                 color=rule.color if rule else None))

    def _render(self, e: LogEntry) -> str:
        """Build the display line for an entry using the current Settings toggles."""
        if e.data is None:
            body = e.text
        else:
            data = e.data
            if (self.display["trim_crc"] and e.direction == "RX"
                    and e.cmd == CMD_RX_FRAME and len(data) > 1):
                data = data[:-1]                              # drop trailing CRC byte
            extras = [e.label] if e.label else []
            # An unexpected CMD is always shown so unknown packets aren't mistaken for frames.
            if e.cmd is not None and (self.display["show_command"] or e.cmd not in KNOWN_CMDS):
                extras.append(f"(CMD 0x{e.cmd:02X})")
            body = hex_str(data) + (f"  | {' '.join(extras)}" if extras else "")
        prefix = f"{e.direction:<4}| "
        if self.display["show_timestamp"]:
            prefix = f"[{datetime.fromtimestamp(e.ts).strftime('%H:%M:%S.%f')[:-3]}] " + prefix
        return prefix + body

    def _add_item(self, e: LogEntry) -> None:
        self._console_items.append(
            dpg.add_text(self._render(e), parent=self.console, color=e.color or self.colors[e.color_key]))

    def _add_entry(self, e: LogEntry) -> None:
        self.entries.append(e)
        self._add_item(e)
        while len(self._console_items) > self.settings["console"]["max_lines"]:
            dpg.delete_item(self._console_items.popleft())
        self._scroll_pending = True

    def rerender_console(self) -> None:
        """Redraw existing history after a display toggle changes."""
        dpg.delete_item(self.console, children_only=True)
        self._console_items.clear()
        for e in list(self.entries)[-self.settings["console"]["max_lines"]:]:
            self._add_item(e)
        self._scroll_pending = True

    def clear_console(self) -> None:
        dpg.delete_item(self.console, children_only=True)
        self._console_items.clear()
        self.entries.clear()

    # ------------------------------------------------------ display settings
    def _on_display_toggle(self, sender, app_data, user_data) -> None:
        self.display[user_data] = bool(app_data)
        self.rerender_console()

    def save_display_defaults(self) -> None:
        self.settings["display"] = dict(self.display)
        try:
            save_settings(self.settings, self.settings_path)
            self.log_text("INFO", "Display settings saved as defaults in settings.json", "info")
        except OSError as exc:
            self.log_text("ERR", f"Could not save settings: {exc}", "error")

    def _on_save_file(self, sender, app_data) -> None:
        path = app_data.get("file_path_name", "")
        if path.endswith(".*"):
            path = path[:-2]
        if not path:
            return
        p = Path(path)
        if not p.suffix:
            p = p.with_suffix(".txt")
        try:   # saved with the *current* display settings
            p.write_text("\n".join(self._render(e) for e in self.entries) + "\n", encoding="utf-8")
            self.log_text("INFO", f"Console log saved to {p}", "info")
        except OSError as exc:
            self.log_text("ERR", f"Could not save log: {exc}", "error")

    # ============================================================ connection
    def refresh_ports(self, select: str = "") -> None:
        current = select or self._port_map.get(dpg.get_value(self.port_combo), "")
        ports = SerialHandler.list_ports()
        self._port_map = {f"{dev} - {desc}" if desc else dev: dev for dev, desc in ports}
        dpg.configure_item(self.port_combo, items=list(self._port_map))
        label = next((l for l, d in self._port_map.items() if d == current), "")
        dpg.set_value(self.port_combo, label)

    def _on_port_selected(self, sender, app_data) -> None:
        dev = self._port_map.get(app_data, "")
        if dev:
            self.settings["serial"]["port"] = dev

    def toggle_connection(self) -> None:
        if self.serial.is_connected:
            self.serial.disconnect()
            self.log_text("INFO", "Disconnected", "info")
        else:
            dev = self._port_map.get(dpg.get_value(self.port_combo), "")
            if not dev:
                self.log_text("WARN", "Select a serial port first (Connection > Port)", "warn")
                return
            baud = int(self.settings["serial"]["baud_rate"])
            try:
                self.serial.connect(dev, baud)
                self.log_text("INFO", f"Connected to {dev} @ {baud} baud", "info")
            except Exception as exc:  # noqa: BLE001
                self.log_text("ERR", f"Could not open {dev}: {exc}", "error")
        self._update_connection_ui()

    def _update_connection_ui(self) -> None:
        up = self.serial.is_connected
        dpg.configure_item(self.connect_item, label="Disconnect" if up else "Connect")
        dpg.set_value(self.status_text, f"Connected: {self.serial.port_name} @ {self.serial.baud}"
                      if up else "Disconnected")
        dpg.configure_item(self.status_text, color=self.colors["ack" if up else "nack"])
        if not up:
            dpg.set_value(self.rx_item, False)

    def _on_rx_toggle(self, sender, app_data) -> None:
        enable = bool(app_data)
        if not self.serial.is_connected:
            self.log_text("WARN", "Connect first to change RX mode", "warn")
            dpg.set_value(self.rx_item, False)
            return
        try:
            self.serial.send(CMD_RX_MODE, bytes([0x01 if enable else 0x00]))
            self.log_text("TX", f"RX mode {'ENABLE' if enable else 'DISABLE'} (CMD 01 {int(enable):02X})", "tx")
        except Exception as exc:  # noqa: BLE001
            self.log_text("ERR", f"RX mode command failed: {exc}", "error")
            dpg.set_value(self.rx_item, not enable)

    # ================================================================ events
    def _handle_event(self, ev: SerialEvent) -> None:
        if ev.kind == "frame":
            self.log_frame("RX", ev.data, ev.ts, cmd=ev.cmd)
        elif ev.kind == "ack":
            self.log_text("DEV", "ACK", "ack", ev.ts)
        elif ev.kind == "nack":
            self.log_text("DEV", "NACK", "nack", ev.ts)
        elif ev.kind == "noise":
            self.log_text("WARN", f"Unframed bytes: {hex_str(ev.data)}", "warn", ev.ts)
        elif ev.kind == "disconnected":
            self.serial.disconnect()
            self.log_text("ERR", f"Connection lost: {ev.message}", "error", ev.ts)
            self._update_connection_ui()

    # ============================================================ transmit
    def transmit(self, data: bytes, note: str = "") -> None:
        """Send raw J1850 bytes as [STX | LEN | 0x02 | data | ETX]."""
        try:
            self.serial.send(CMD_SEND_FRAME, data)
        except Exception as exc:  # noqa: BLE001
            self.log_text("ERR", f"Send failed: {exc}", "error")
            return
        self.log_frame("TX", data, cmd=CMD_SEND_FRAME, note=note)

    def on_manual_send(self) -> None:
        try:
            data = parse_hex(dpg.get_value(self.manual_input))
        except ValueError as exc:
            self.log_text("ERR", f"Bad hex input: {exc}", "error")
            return
        if not data:
            return
        self.transmit(data)
        dpg.focus_item(self.manual_input)         # keep typing after Enter

    def _on_preset_click(self, sender, app_data, preset: Preset) -> None:
        self.transmit(preset.data, note=f"[{preset.name}]")

    # ============================================================== presets
    def rebuild_preset_buttons(self) -> None:
        dpg.delete_item(self.preset_area, children_only=True)
        if not self.bundle.presets:
            dpg.add_text("(no presets loaded - File > Load Presets JSON...)", parent=self.preset_area)
            return
        row = None
        for i, p in enumerate(self.bundle.presets):
            if i % PRESETS_PER_ROW == 0:
                row = dpg.add_group(horizontal=True, parent=self.preset_area)
            btn = dpg.add_button(label=p.name, width=170, parent=row,
                                 callback=self._on_preset_click, user_data=p)
            with dpg.tooltip(btn):
                if p.description:
                    dpg.add_text(p.description)
                dpg.add_text(hex_str(p.data))

    def _on_presets_file(self, sender, app_data) -> None:
        path = app_data.get("file_path_name", "")
        if not path:
            return
        try:
            bundle = load_presets(Path(path))
        except ConfigError as exc:                # keep current presets on failure
            self.log_text("ERR", str(exc), "error")
            return
        self.bundle = bundle
        self.rebuild_preset_buttons()
        for w in bundle.warnings:
            self.log_text("WARN", w, "warn")
        self.log_text("INFO", f"Loaded {len(bundle.presets)} presets, {len(bundle.rules)} rules "
                              f"from {path}", "info")
