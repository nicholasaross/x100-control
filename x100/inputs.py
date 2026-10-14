"""Input map: Alexa input names <-> ViewSonic source codes.

Stored as JSON so ``x100ctl learn-inputs`` can write it and the bridge can
advertise exactly the inputs it knows how to select. Only inputs with a known
``code`` are offered to Alexa.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

# Valid values for Alexa.InputController "input" (Amazon developer docs).
ALEXA_INPUT_NAMES = (
    [f"AUX {i}" for i in range(1, 8)]
    + ["BLURAY", "CABLE", "CD", "COAX 1", "COAX 2", "COMPOSITE 1", "DVD", "GAME", "HD RADIO"]
    + [f"HDMI {i}" for i in range(1, 11)]
    + ["HDMI ARC"]
    + [f"INPUT {i}" for i in range(1, 11)]
    + ["IPOD"]
    + [f"LINE {i}" for i in range(1, 8)]
    + ["MEDIA PLAYER", "OPTICAL 1", "OPTICAL 2", "PHONO", "PLAYSTATION", "PLAYSTATION 3",
       "PLAYSTATION 4", "SATELLITE", "SMARTCAST", "TUNER", "TV", "USB DAC", "VIDEO 1", "VIDEO 2",
       "VIDEO 3", "XBOX"]
)


@dataclass
class InputDef:
    alexa: str
    code: Optional[int] = None
    label: str = ""
    friendly_names: List[str] = field(default_factory=list)

    def to_json(self) -> dict:
        d: dict = {"alexa": self.alexa, "code": self.code}
        if self.label:
            d["label"] = self.label
        if self.friendly_names:
            d["friendly_names"] = list(self.friendly_names)
        return d


def default_inputs() -> List[InputDef]:
    """HDMI 1/2 codes are ViewSonic-documented; the rest are learned on your unit."""
    return [
        InputDef("HDMI 1", 0x03, "HDMI 1"),
        InputDef("HDMI 2", 0x07, "HDMI 2"),
        InputDef("HDMI 3", None, "HDMI 3"),
        InputDef("HDMI 4", None, "HDMI 4"),
        InputDef("INPUT 1", None, "Computer In (VGA)", ["Computer", "PC"]),
        InputDef("MEDIA PLAYER", None, "Home screen / apps", ["Home", "Smart TV"]),
    ]


def _norm(s: str) -> str:
    return " ".join(s.upper().replace("_", " ").replace("-", " ").split())


class InputMap:
    def __init__(self, defs: Iterable[InputDef]):
        self.defs: List[InputDef] = list(defs)
        self.validate()

    # -- persistence ------------------------------------------------------------
    @classmethod
    def load(cls, path: Optional[str]) -> "InputMap":
        if not path or not os.path.exists(path):
            return cls(default_inputs())
        with open(path) as fh:
            raw = json.load(fh)
        items = raw["inputs"] if isinstance(raw, dict) else raw
        defs = [
            InputDef(
                alexa=_norm(item["alexa"]),
                code=item.get("code"),
                label=item.get("label", ""),
                friendly_names=list(item.get("friendly_names", [])),
            )
            for item in items
        ]
        return cls(defs)

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"inputs": [d.to_json() for d in self.defs]}, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)

    # -- validation ---------------------------------------------------------------
    def validate(self) -> None:
        seen_names, seen_codes, seen_friendly = set(), set(), set()
        for d in self.defs:
            if d.alexa not in ALEXA_INPUT_NAMES:
                raise ValueError(f"{d.alexa!r} is not an Alexa input name; pick one of: "
                                 + ", ".join(ALEXA_INPUT_NAMES))
            if d.alexa in seen_names:
                raise ValueError(f"duplicate input {d.alexa!r}")
            seen_names.add(d.alexa)
            if d.code is not None:
                if not isinstance(d.code, int) or not 0 <= d.code <= 0xFF:
                    raise ValueError(f"{d.alexa}: code must be 0-255, got {d.code!r}")
                if d.code in seen_codes:
                    raise ValueError(f"source code 0x{d.code:02X} used twice")
                seen_codes.add(d.code)
            for f in d.friendly_names:
                key = _norm(f)
                if key in seen_friendly or key in seen_names:
                    raise ValueError(f"friendly name {f!r} is not unique")
                seen_friendly.add(key)

    # -- lookups ------------------------------------------------------------------
    def advertised(self) -> List[InputDef]:
        return [d for d in self.defs if d.code is not None]

    def find(self, name: str) -> Optional[InputDef]:
        """Match an Alexa input name, a friendly name or a label (case-insensitive)."""
        key = _norm(name)
        for d in self.defs:
            if key == d.alexa or key == _norm(d.label) or key in (_norm(f) for f in d.friendly_names):
                return d
        return None

    def name_for_code(self, code: Optional[int]) -> Optional[str]:
        for d in self.defs:
            if d.code is not None and d.code == code:
                return d.alexa
        return None

    def set_code(self, alexa_name: str, code: Optional[int]) -> None:
        d = self.find(alexa_name)
        if d is None:
            raise KeyError(alexa_name)
        for other in self.defs:
            if other is not d and code is not None and other.code == code:
                other.code = None
        d.code = code
        self.validate()

    def discovery_inputs(self) -> List[Dict[str, object]]:
        out: List[Dict[str, object]] = []
        for d in self.advertised():
            entry: Dict[str, object] = {"name": d.alexa}
            if d.friendly_names:
                entry["friendlyNames"] = list(d.friendly_names)
            out.append(entry)
        return out
