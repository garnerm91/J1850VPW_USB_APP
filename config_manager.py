"""Settings + presets loading. Every loader degrades gracefully: a missing or
corrupt file produces defaults and warnings, never an exception at startup."""
from __future__ import annotations

import copy
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

Color = Tuple[int, int, int, int]

DEFAULT_SETTINGS: Dict[str, Any] = {
    "serial": {"port": "", "baud_rate": 115200},
    "window": {"title": "J1850 VPW USB Console", "width": 1100, "height": 760},
    "theme": "dark",
    "font": {"path": "", "size": 16},
    "presets_path": "presets.json",
    "console": {"max_lines": 3000, "auto_scroll": True},
    "display": {"trim_crc": False, "show_timestamp": True, "show_command": True},
}


class ConfigError(Exception):
    pass


# =============================================================== hex helpers
def _split_tokens(text: str) -> List[str]:
    text = re.sub(r"\b0[xX]", "", text)
    return [t for t in re.split(r"[\s,;:_\-]+", text) if t]


def parse_hex(text: str) -> bytes:
    """'6C 10 40', '0x6C,0x10', '6C1040' -> bytes. Strict: 2 digits per byte."""
    out = bytearray()
    for tok in _split_tokens(text):
        if not re.fullmatch(r"[0-9A-Fa-f]+", tok):
            raise ValueError(f"Invalid hex token '{tok}'")
        if len(tok) % 2:
            raise ValueError(f"'{tok}' has an odd number of digits (use 2 digits per byte)")
        out.extend(bytes.fromhex(tok))
    return bytes(out)


def parse_pattern(text: str) -> List[Optional[int]]:
    """'6C ?? 40' -> [0x6C, None, 0x40]  (?? = any byte)."""
    pat: List[Optional[int]] = []
    for tok in _split_tokens(text):
        parts = re.findall(r"\?\?|[0-9A-Fa-f]{2}", tok)
        if "".join(parts) != tok:
            raise ValueError(f"Invalid pattern token '{tok}'")
        pat.extend(None if p == "??" else int(p, 16) for p in parts)
    if not pat:
        raise ValueError("Empty pattern")
    return pat


def hex_str(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


def parse_color(value: Any) -> Optional[Color]:
    """'#RRGGBB' / '#RRGGBBAA' / [r,g,b(,a)] -> (r,g,b,a) or None."""
    try:
        if isinstance(value, str):
            m = re.fullmatch(r"#?([0-9A-Fa-f]{6})([0-9A-Fa-f]{2})?", value.strip())
            if not m:
                return None
            h = m.group(1)
            a = int(m.group(2), 16) if m.group(2) else 255
            return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a
        if isinstance(value, (list, tuple)) and len(value) in (3, 4):
            vals = [int(v) for v in value]
            if all(0 <= v <= 255 for v in vals):
                return (*vals[:3], vals[3] if len(vals) == 4 else 255)
    except (TypeError, ValueError):
        pass
    return None


# ================================================================= settings
def resolve_path(path: str, base_dir: Path) -> Path:
    p = Path(path).expanduser()
    return p if p.is_absolute() else base_dir / p


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _merge(defaults: Dict[str, Any], loaded: Dict[str, Any], warnings: List[str], prefix="") -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, default in defaults.items():
        name = prefix + key
        if key not in loaded:
            result[key] = copy.deepcopy(default)
            continue
        value = loaded[key]
        if isinstance(default, dict):
            if isinstance(value, dict):
                result[key] = _merge(default, value, warnings, name + ".")
            else:
                warnings.append(f"settings: '{name}' should be an object; using default")
                result[key] = copy.deepcopy(default)
        elif isinstance(default, bool):
            ok = isinstance(value, bool)
            result[key] = value if ok else default
            if not ok: warnings.append(f"settings: '{name}' should be true/false; using default")
        elif _is_num(default):
            ok = _is_num(value)
            result[key] = value if ok else default
            if not ok: warnings.append(f"settings: '{name}' should be a number; using default")
        else:  # str
            ok = isinstance(value, str)
            result[key] = value if ok else default
            if not ok: warnings.append(f"settings: '{name}' should be a string; using default")
    for key, value in loaded.items():  # keep unknown keys so saving doesn't drop them
        result.setdefault(key, value)
    return result


def _sanitize(s: Dict[str, Any], warnings: List[str]) -> None:
    def clamp(d, key, lo, hi, cast=int):
        v = d[key]
        nv = cast(min(max(v, lo), hi))
        if nv != v:
            warnings.append(f"settings: value {v!r} out of range; using {nv}")
        d[key] = nv
    clamp(s["serial"], "baud_rate", 300, 4_000_000)
    clamp(s["window"], "width", 480, 7680)
    clamp(s["window"], "height", 360, 4320)
    clamp(s["font"], "size", 6, 72, float)
    clamp(s["console"], "max_lines", 100, 100_000)
    if s["theme"] not in ("dark", "light"):
        warnings.append(f"settings: unknown theme '{s['theme']}'; using 'dark'")
        s["theme"] = "dark"


def load_settings(path: Path) -> Tuple[Dict[str, Any], List[str]]:
    """Never raises. Returns (settings, warnings)."""
    warnings: List[str] = []
    defaults = copy.deepcopy(DEFAULT_SETTINGS)
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("top-level JSON value must be an object")
        settings = _merge(defaults, raw, warnings)
        _sanitize(settings, warnings)
        return settings, warnings
    except FileNotFoundError:
        warnings.append(f"settings file not found ({path}); using built-in defaults")
    except Exception as exc:  # noqa: BLE001 - corrupt/unreadable/invalid
        warnings.append(f"could not load settings ({path}): {exc}; using built-in defaults")
    return defaults, warnings


def save_settings(settings: Dict[str, Any], path: Path) -> None:
    Path(path).write_text(json.dumps(settings, indent=2), encoding="utf-8")


# ================================================================== presets
@dataclass
class Preset:
    name: str
    description: str
    data: bytes


@dataclass
class Rule:
    name: str
    label: str
    pattern: List[Optional[int]]
    color: Optional[Color] = None

    def matches(self, data: bytes) -> bool:
        if len(data) < len(self.pattern):
            return False
        return all(p is None or p == b for p, b in zip(self.pattern, data))


@dataclass
class PresetBundle:
    presets: List[Preset] = field(default_factory=list)
    rules: List[Rule] = field(default_factory=list)
    source: str = ""
    warnings: List[str] = field(default_factory=list)

    def decode(self, data: bytes) -> Optional[Rule]:
        """First matching rule wins (order in the JSON = priority)."""
        return next((r for r in self.rules if r.matches(data)), None)


def load_presets(path: Path) -> PresetBundle:
    """Raises ConfigError if the file itself is unusable; bad *entries* are
    skipped with a warning in bundle.warnings."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"presets file not found: {path}")
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"could not read presets file {path}: {exc}")
    if not isinstance(raw, dict):
        raise ConfigError("presets file: top-level JSON value must be an object")

    bundle = PresetBundle(source=str(path))
    w = bundle.warnings

    items = raw.get("presets", [])
    if not isinstance(items, list):
        w.append("'presets' must be a list; ignoring"); items = []
    for i, item in enumerate(items, 1):
        try:
            data = parse_hex(str(item["data"]))
            if not data:
                raise ValueError("empty data")
            if len(data) > 254:
                raise ValueError("frame too long (max 254 bytes)")
            bundle.presets.append(Preset(str(item.get("name") or f"Preset {i}"),
                                         str(item.get("description", "")), data))
        except Exception as exc:  # noqa: BLE001
            w.append(f"preset #{i} skipped: {exc!r}")

    rules = raw.get("rules", [])
    if not isinstance(rules, list):
        w.append("'rules' must be a list; ignoring"); rules = []
    for i, item in enumerate(rules, 1):
        try:
            color = parse_color(item["color"]) if "color" in item else None
            if "color" in item and color is None:
                w.append(f"rule #{i}: invalid color; using default color")
            bundle.rules.append(Rule(str(item.get("name", f"Rule {i}")),
                                     str(item.get("label", item.get("name", ""))),
                                     parse_pattern(str(item["match"])), color))
        except Exception as exc:  # noqa: BLE001
            w.append(f"rule #{i} skipped: {exc!r}")
    return bundle


def load_presets_safe(path: Optional[Path]) -> PresetBundle:
    """Startup variant: never raises; falls back to an empty bundle."""
    if not path or not str(path):
        return PresetBundle(warnings=["no presets file configured; starting with no presets"])
    try:
        return load_presets(path)
    except ConfigError as exc:
        return PresetBundle(warnings=[f"{exc}; starting with no presets"])
    except Exception as exc:  # noqa: BLE001
        return PresetBundle(warnings=[f"unexpected error loading presets: {exc}; starting with no presets"])
