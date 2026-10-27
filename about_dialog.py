"""
about_dialog.py - a self-contained About / Help popup
"""

import math
import platform
import random
import time
import webbrowser
import serial as serial
import dearpygui.dearpygui as dpg

# --------------------------------------------------------------------------- #
# Configuration - edit here or override at runtime with configure(...)
# --------------------------------------------------------------------------- #
CONFIG = {
    "app_name": "USB J1850 VPW Console",
    "version": "1.0.0",
    "tagline": "Python application for test and debug.",
    "author": "M Garner",
    "email": "https://github.com/garnerm91",
    "description": (
        "This application is a Python-based console for communicating with J1850 VPW devices over USB. It provides a user-friendly interface for sending and receiving messages, as well as tools for debugging and testing."
    ),
    "links": [  # (button label, url)
 
    ],
    "help": [  # (section title, body text)
        ("Getting started", "To use this application, connect your J1850 VPW device to your computer via USB. Launch the application and configure the settings as needed. Use the console to send and receive messages."),
        ("Troubleshooting", "If you encounter issues, ensure that your device is properly connected and recognized by your operating system. Check the settings and consult the documentation for further assistance."),
    ],
    "accent": (74, 168, 224),    # gradient start / UI accent
    "accent2": (245, 250, 184),   # gradient end
}

_TAG = "about_dialog_window"
_THEME_TAG = "about_dialog_theme"
_W, _H = 520, 490
_HEADER_H = 96
_particles = []


# --------------------------------------------------------------------------- #
# Public callback
# --------------------------------------------------------------------------- #
def show_about(sender=None, app_data=None, user_data=None):
    """DPG callback: open the About / Help modal (or focus it if already open)."""
    if dpg.does_item_exist(_TAG):
        dpg.focus_item(_TAG)
        return

    _ensure_theme()
    vw, vh = dpg.get_viewport_client_width(), dpg.get_viewport_client_height()
    pos = (max((vw - _W) // 2, 0), max((vh - _H) // 2, 0))

    with dpg.window(
        label=f"About {CONFIG['app_name']}",
        tag=_TAG,
        modal=True,
        no_resize=True,
        no_collapse=True,
        no_move=True,
        width=_W,
        height=_H,
        pos=pos,
        on_close=lambda: dpg.delete_item(_TAG),
    ):
        _build_header()
        dpg.add_spacer(height=4)
        with dpg.tab_bar():
            _build_about_tab()
            _build_help_tab()
            _build_system_tab()
        dpg.add_spacer(height=4)
        dpg.add_button(
            label="Close",
            width=-1,
            height=30,
            callback=lambda: dpg.delete_item(_TAG),
        )

    dpg.bind_item_theme(_TAG, _THEME_TAG)
    _start_animation()


# --------------------------------------------------------------------------- #
# UI sections
# --------------------------------------------------------------------------- #
def _build_header():
    w = _W - 24  # window padding
    a, b = CONFIG["accent"], CONFIG["accent2"]
    mid = tuple((x + y) // 2 for x, y in zip(a, b))

    with dpg.drawlist(width=w, height=_HEADER_H, tag="about_header"):
        dpg.draw_rectangle(
            (0, 0), (w, _HEADER_H),
            fill=(*a, 255), color=(0, 0, 0, 0), rounding=8, multicolor=True,
            color_upper_left=(*a, 255), color_upper_right=(*mid, 255),
            color_bottom_right=(*b, 255), color_bottom_left=(*mid, 255),
        )
        # Floating bubbles
        _particles.clear()
        rng = random.Random(7)
        for i in range(16):
            p = {
                "x": rng.uniform(0, w),
                "r": rng.uniform(2, 7),
                "speed": rng.uniform(8, 22),
                "phase": rng.uniform(0, 200),
                "sway": rng.uniform(3, 10),
                "tag": f"about_particle_{i}",
            }
            dpg.draw_circle((p["x"], _HEADER_H), p["r"], fill=(255, 255, 255, 60),
                            color=(0, 0, 0, 0), tag=p["tag"])
            _particles.append(p)

        dpg.draw_text((18, 14), CONFIG["app_name"], size=30, color=(38, 40, 46, 255))
        dpg.draw_text((20, 54), CONFIG["tagline"], size=16, color=(38, 40, 46, 255))
        dpg.draw_text((w - 20 - 8 * len(f"v{CONFIG['version']}"), 70),
                      f"v{CONFIG['version']}", size=15, color=(38, 40, 46, 200))


def _tab_body(height=250):
    return dpg.child_window(height=height, border=False)


def _build_about_tab():
    with dpg.tab(label="About"):
        with _tab_body():
            dpg.add_spacer(height=4)
            dpg.add_text(CONFIG["description"], wrap=_W - 60)
            dpg.add_spacer(height=8)
            dpg.add_separator()
            dpg.add_spacer(height=4)
            _kv("Author", CONFIG["author"])
            _kv("GitHub", CONFIG["email"])
            _kv("Version", CONFIG["version"])
            dpg.add_spacer(height=8)
            if CONFIG["links"]:
                with dpg.group(horizontal=True):
                    for label, url in CONFIG["links"]:
                        dpg.add_button(label=label, callback=lambda s, a, u: webbrowser.open(u),
                                       user_data=url)


def _build_help_tab():
    with dpg.tab(label="Help"):
        with _tab_body():
            dpg.add_spacer(height=4)
            for i, (title, body) in enumerate(CONFIG["help"]):
                with dpg.collapsing_header(label=title, default_open=(i == 0)):
                    dpg.add_text(body, wrap=_W - 70, indent=10)
                    dpg.add_spacer(height=4)


def _build_system_tab():
    with dpg.tab(label="System"):
        with _tab_body():
            dpg.add_spacer(height=4)
            _kv("Python", platform.python_version())
            _kv("DearPyGui", dpg.get_dearpygui_version())
            _kv("Pyserial", serial.VERSION)
            _kv("Platform", f"{platform.system()} {platform.release()}")
            _kv("Machine", platform.machine())
            dpg.add_spacer(height=8)
            dpg.add_button(
                label="Copy to clipboard",
                callback=lambda: dpg.set_clipboard_text(
                    f"{CONFIG['app_name']} {CONFIG['version']}\n"
                    f"Python {platform.python_version()}\n"
                    f"DearPyGui {dpg.get_dearpygui_version()}\n"
                    f"Pyserial {serial.VERSION}\n"
                    f"{platform.platform()}"
                ),
            )


def _kv(key, value):
    with dpg.group(horizontal=True):
        dpg.add_text(f"{key}:", color=(*CONFIG["accent"], 255))
        dpg.add_text(str(value))


# --------------------------------------------------------------------------- #
# Animation (self-rescheduling frame callback; stops when the window is gone)
# --------------------------------------------------------------------------- #
def _start_animation():
    t0 = time.time()

    def tick():
        if not dpg.does_item_exist(_TAG):
            return
        t = time.time() - t0
        w = _W - 24
        for p in _particles:
            y = _HEADER_H + p["r"] - ((t * p["speed"] + p["phase"]) % (_HEADER_H + 2 * p["r"]))
            x = (p["x"] + math.sin(t * 0.8 + p["phase"]) * p["sway"]) % w
            fade = max(0.0, min(1.0, y / _HEADER_H))  # fade out near the top
            dpg.configure_item(p["tag"], center=(x, y),
                               fill=(255, 255, 255, int(20 + 70 * fade)))
        dpg.set_frame_callback(dpg.get_frame_count() + 1, tick)

    dpg.set_frame_callback(dpg.get_frame_count() + 1, tick)


# --------------------------------------------------------------------------- #
# Theme
# --------------------------------------------------------------------------- #
def _ensure_theme():
    if dpg.does_item_exist(_THEME_TAG):
        dpg.delete_item(_THEME_TAG)  # rebuild so configure() accent changes apply
    r, g, b = CONFIG["accent"]
    with dpg.theme(tag=_THEME_TAG):
        with dpg.theme_component(dpg.mvAll):
            dpg.add_theme_style(dpg.mvStyleVar_WindowRounding, 10, category=dpg.mvThemeCat_Core)
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 6, category=dpg.mvThemeCat_Core)
            dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 6, category=dpg.mvThemeCat_Core)
            dpg.add_theme_style(dpg.mvStyleVar_TabRounding, 6, category=dpg.mvThemeCat_Core)
            dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 12, 12, category=dpg.mvThemeCat_Core)
            dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 8, 6, category=dpg.mvThemeCat_Core)
            dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 10, 5, category=dpg.mvThemeCat_Core)

            dpg.add_theme_color(dpg.mvThemeCol_WindowBg, (24, 24, 32, 255), category=dpg.mvThemeCat_Core)
            dpg.add_theme_color(dpg.mvThemeCol_TitleBgActive, (r // 2, g // 2, b // 2, 255), category=dpg.mvThemeCat_Core)
            dpg.add_theme_color(dpg.mvThemeCol_Button, (r, g, b, 160), category=dpg.mvThemeCat_Core)
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (r, g, b, 230), category=dpg.mvThemeCat_Core)
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (r, g, b, 255), category=dpg.mvThemeCat_Core)
            dpg.add_theme_color(dpg.mvThemeCol_Tab, (r, g, b, 70), category=dpg.mvThemeCat_Core)
            dpg.add_theme_color(dpg.mvThemeCol_TabHovered, (r, g, b, 200), category=dpg.mvThemeCat_Core)
            dpg.add_theme_color(dpg.mvThemeCol_TabActive, (r, g, b, 255), category=dpg.mvThemeCat_Core)
            dpg.add_theme_color(dpg.mvThemeCol_Header, (r, g, b, 90), category=dpg.mvThemeCat_Core)
            dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered, (r, g, b, 160), category=dpg.mvThemeCat_Core)
            dpg.add_theme_color(dpg.mvThemeCol_HeaderActive, (r, g, b, 220), category=dpg.mvThemeCat_Core)
            dpg.add_theme_color(dpg.mvThemeCol_Separator, (r, g, b, 90), category=dpg.mvThemeCat_Core)

# --------------------------------------------------------------------------- #
# Demo: `python about_dialog.py`
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    dpg.create_context()
    dpg.create_viewport(title="About dialog", width=700, height=560)
    with dpg.window(tag="primary"):
        dpg.add_text("Click the button below.")
        dpg.add_button(label="About / Help", callback=show_about)
    dpg.setup_dearpygui()
    dpg.show_viewport()
    dpg.set_primary_window("primary", True)
    dpg.start_dearpygui()
    dpg.destroy_context()
