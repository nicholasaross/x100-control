"""What the projector's draw says it did, read from its plug (plug.py), for the log.

It decides nothing the bridge acts on, and sends nothing to the projector or the plug. It turns the
plug's readings into events: a start, and whether the bridge's "on" caused it; an "off", and whether
the bridge's "off" did; a press that reached the projector without starting it; an "on" it ignored;
and how long it took to boot when its power came back.

What the draw looked like through Nick's plug, from 8 to 17 November 2026:

- standby: 11.5-15.4 W (its fans run now and then); on: about 150 W.
- a start, by a serial "on" or its remote: a dip to 5.6-10.3 W, 2-3 s after a serial "on", then
  25-42 W for about 11 s, then over 100 W about 17 s after the "on".
- a press that reached it in a hang without starting it (16 November): the dip, then 12-17.5 W for
  over a minute, like a boot.
- an "on" it ignored (a hang): no change at all.
- an "off", by the bridge or its remote: below 25 W within 1-2 s.
- a step in standby, minutes after an "off" (after 4 of 8 remote "off"s): a dip from about 11.3 W to
  9.1-9.8 W, then about 10.5 W for a while, or back to 12-13 W. Nothing presses anything: something
  inside it changes. It isn't a sign of a hang (it never came in the 3 that hung, but it didn't come in
  4 of the 8 that didn't either). Told apart from a press by what follows: a press that reaches it
  draws over 16 W within 20 s.
- a boot when its power came back: 11-19.6 W, back under 14.5 W within about 45 s.
- unplugged: 0 W with the plug's power on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional

DIP_WATTS = 10.5          # a reading below this, and DIP_DROP below the one before: the projector's draw dipped
DIP_DROP = 1.5            # (the dips as it starts were 2.4-5.4 W below; a stepped-down standby wavers by 0.3 W)
REACTED_WATTS = 16.0      # after a dip, more than this within REACTED_SECONDS: something reached it (a press)
REACTED_SECONDS = 20.0    # (16.2 W 12 s after the press of 16 November; a step stays under 13.5 W)
SETTLED_WATTS = 14.5      # a boot has settled once its draw stays at or below this...
SETTLED_SECONDS = 30.0    # ...for this long
BOOT_LONGEST = 180.0      # or this long after its power came back, whatever it's drawing
BOOT_GRACE = 5.0          # readings this soon after its power comes back can dip (the inrush)
START_SECONDS = 60.0      # from its dip, a start reaches plug_on_watts within this (17 s, 11 to 17 November)
CAUSE_SECONDS = 10.0      # a dip or a fall this soon after the bridge's "on" or "off" came from it (2-3 s)
FALLING_LONGEST = 90.0    # below on but above standby for this long: not on its way off after all
FRESH_SECONDS = 150.0     # the plug reports any change at once, and about once a minute anyway


@dataclass
class PlugEvent:
    kind: str                         # "start", "off", "press", "step", "rise", "no_reaction", "boot", ...
    text: str                         # for the log, after "plug: "
    by_bridge: Optional[bool] = None  # whether the bridge's own "on" or "off" caused it, where that applies
    at: float = 0.0                   # when it happened (time.monotonic): the dip, the fall, the power back


class PlugWatch:
    """Fed every reading from the plug (``reading``), the bridge's own "on" and "off" (``sent``), and the
    time now and then (``tick``), it returns the events those add up to. ``stamp`` turns a time.monotonic()
    time into a clock time for the log."""

    def __init__(self, unplugged_watts: float, standby_watts: float, on_watts: float,
                 stamp: Callable[[float], str], cause_seconds: float = CAUSE_SECONDS):
        self.unplugged_watts = unplugged_watts
        self.standby_watts = standby_watts
        self.on_watts = on_watts
        self.stamp = stamp
        self.cause_seconds = cause_seconds  # see CAUSE_SECONDS: shorter only in tests
        self.phase = ""                       # "no power", "booting", "standby", "starting", "on", "falling",
        self.watts: Optional[float] = None    # "between"; "" before the first reading
        self.last_at: Optional[float] = None  # when the last reading came
        self._on_level = 0.0                  # the most it has drawn since it last came on
        self._start_at: Optional[float] = None    # a start under way: since its dip (or rise),
        self._start_dip: Optional[float] = None   # how low it dipped, the most it has drawn since,
        self._start_peak = 0.0
        self._start_by_bridge = False             # and whether the bridge's "on" did it
        self._fall_at: Optional[float] = None     # an "off" under way: since its draw fell below on,
        self._fall_by_bridge = False              # and whether the bridge's "off" did it
        self._boot_at: Optional[float] = None     # a boot: since its power came back,
        self._boot_peak = 0.0                     # its highest draw,
        self._boot_peak_at = 0.0
        self._boot_high_at: Optional[float] = None  # its last reading above SETTLED_WATTS,
        self._boot_low_at: Optional[float] = None   # and the first at or below it after that
        self._on_at: Optional[float] = None       # the bridge's last "on", and whether what came of it is told
        self._on_told = True
        self._on_from = 0.0                       # what it drew when the bridge sent it
        self._off_at: Optional[float] = None      # the same for its "off"
        self._off_told = True

    # ------------------------------------------------------------------ what the bridge did
    def sent(self, what: str, at: float) -> None:
        """The bridge has just sent "on" or "off" to the projector, and had it acknowledged."""
        if what == "on":
            self._on_at, self._on_told, self._on_from = at, False, self.watts or 0.0
        elif what == "off":
            self._off_at, self._off_told = at, False

    def fresh(self, now: float) -> bool:
        return self.last_at is not None and now - self.last_at < FRESH_SECONDS

    @property
    def starting_since(self) -> Optional[float]:
        """A start under way (or something like one): since its dip, or its rise above standby."""
        return self._start_at if self.phase == "starting" else None

    # ------------------------------------------------------------------ readings
    def reading(self, at: float, output: bool, watts: Optional[float]) -> List[PlugEvent]:
        if watts is None:
            return []
        w = watts if output else 0.0
        before, self.watts, self.last_at = self.watts, w, at
        dip = w < DIP_WATTS and before is not None and before - w >= DIP_DROP
        events: List[PlugEvent] = []
        if w <= self.unplugged_watts:  # no power: the plug's own off, or unplugged (the bridge says which)
            if self.phase == "booting":
                events.append(self._boot_event(at, "and then lost its power again"))
            self._start_at = self._fall_at = None
            self.phase = "no power"
            return events
        if self.phase == "no power":
            self.phase = "booting"
            self._boot_at, self._boot_peak, self._boot_peak_at = at, w, at
            self._boot_high_at = at if w > SETTLED_WATTS else None
            self._boot_low_at = None if w > SETTLED_WATTS else at
            return events
        if self.phase == "booting":
            assert self._boot_at is not None
            if at - self._boot_at >= BOOT_GRACE and (dip or w > self.standby_watts):
                events.append(self._boot_event(at, "when something started it"))
                self.phase = "standby"  # and on to the start below
            else:
                self._boot_follow(at, w)
                return events
        if self.phase == "":
            self.phase = "on" if w >= self.on_watts else "standby" if w <= self.standby_watts else "between"
            if self.phase == "on":
                self._on_level = w
            return events
        if self.phase == "standby":
            if dip or w > self.standby_watts:
                self._start_at, self._start_dip, self._start_peak = at, (w if dip else None), w
                self._start_by_bridge = self._caused(self._on_at, at)
                if self._start_by_bridge:
                    self._on_told = True  # told by the start, or the press, it turns into
                self.phase = "starting"
                if w >= self.on_watts:
                    events.append(self._started(at, w))
            return events
        if self.phase == "starting":
            self._start_peak = max(self._start_peak, w)
            if self._start_dip is not None and w < self._start_dip:
                self._start_dip = w
            if w >= self.on_watts:
                events.append(self._started(at, w))
            return events
        if self.phase == "on":
            if w >= self.on_watts:
                self._on_level = max(self._on_level, w)
                return events
            self._fall_at, self._fall_by_bridge = at, self._caused(self._off_at, at)
            if self._fall_by_bridge:
                self._off_told = True
            self.phase = "falling"
        if self.phase == "falling":
            if w <= self.standby_watts:
                events.append(self._went_off(at, w))
            elif w >= self.on_watts:
                self.phase, self._fall_at = "on", None  # back up: a dark scene, perhaps
            return events
        if self.phase == "between":
            if w >= self.on_watts:
                self.phase, self._on_level = "on", w
            elif w <= self.standby_watts:
                self.phase = "standby"
        return events

    def tick(self, now: float) -> List[PlugEvent]:
        """What time alone tells: a boot that has settled, a start that didn't come, an "on" ignored."""
        events: List[PlugEvent] = []
        if self.phase == "booting" and self._boot_at is not None:
            busy_until = self._boot_high_at if self._boot_high_at is not None else self._boot_at
            settled = self._boot_low_at is not None and now - busy_until >= SETTLED_SECONDS
            if settled or now - self._boot_at >= BOOT_LONGEST:
                events.append(self._boot_event(now, "" if settled else "and hadn't settled"))
                self.phase = "standby"
        if (self.phase == "starting" and self._start_at is not None and self._start_dip is not None
                and not self._start_by_bridge and now - self._start_at >= REACTED_SECONDS
                and self._start_peak <= REACTED_WATTS):
            events.append(self._step(now))
        if self.phase == "starting" and self._start_at is not None and now - self._start_at >= START_SECONDS:
            events.append(self._no_start(now))
        if self.phase == "falling" and self._fall_at is not None and now - self._fall_at >= FALLING_LONGEST:
            events.append(PlugEvent("dim", f"its draw fell from {self._on_level:.0f} W at {self.stamp(self._fall_at)}, "
                                    f"but only to {self.watts:.0f} W, and has stayed there: a dim picture, perhaps",
                                    self._fall_by_bridge, self._fall_at))
            self.phase, self._fall_at = "between", None
        if not self._on_told and self._on_at is not None and now - self._on_at >= self.cause_seconds:
            self._on_told = True
            if self.fresh(now):
                events.append(PlugEvent("no_reaction", f"no change in its draw in the {now - self._on_at:.0f} s after "
                                        f"the bridge's 'on' at {self.stamp(self._on_at)} ({self.watts:.1f} W): the "
                                        "projector didn't react to it at all", True, self._on_at))
            else:
                events.append(PlugEvent("no_readings", "no recent readings from the plug, so its draw can't show what "
                                        "the bridge's 'on' did", True, self._on_at))
        if not self._off_told and self._off_at is not None and now - self._off_at >= self.cause_seconds:
            self._off_told = True
            if self.fresh(now) and self.phase == "on":
                events.append(PlugEvent("no_off", f"still drawing {self.watts:.0f} W {now - self._off_at:.0f} s after "
                                        f"the bridge's 'off' at {self.stamp(self._off_at)}: it didn't go off", True,
                                        self._off_at))
        return events

    # ------------------------------------------------------------------ helpers
    def _caused(self, sent_at: Optional[float], at: float) -> bool:
        return sent_at is not None and 0.0 <= at - sent_at <= self.cause_seconds

    def _who(self, by_bridge: bool, sent_at: Optional[float], at: float, what: str) -> str:
        if by_bridge and sent_at is not None:
            return f"{at - sent_at:.0f} s after the bridge's '{what}'"
        if what == "on":
            return "not by the bridge: its remote, or its own button"
        return "not by the bridge: its remote, or the projector itself"

    def _started(self, at: float, w: float) -> PlugEvent:
        assert self._start_at is not None
        began = (f"a dip to {self._start_dip:.1f} W at {self.stamp(self._start_at)}" if self._start_dip is not None
                 else f"its draw rising at {self.stamp(self._start_at)}")
        text = (f"the projector came on: {began}, then {w:.0f} W {at - self._start_at:.0f} s later; "
                + self._who(self._start_by_bridge, self._on_at, self._start_at, "on"))
        event = PlugEvent("start", text, self._start_by_bridge, self._start_at)
        self.phase, self._on_level, self._start_at = "on", w, None
        return event

    def _no_start(self, now: float) -> PlugEvent:
        assert self._start_at is not None
        who = self._who(self._start_by_bridge, self._on_at, self._start_at, "on")
        if self._start_dip is not None:
            event = PlugEvent("press", f"something reached the projector at {self.stamp(self._start_at)} (a dip to "
                              f"{self._start_dip:.1f} W), but it didn't start: {self.watts:.1f} W "
                              f"{now - self._start_at:.0f} s later; {who}", self._start_by_bridge, self._start_at)
        else:
            event = PlugEvent("rise", f"its draw rose above standby at {self.stamp(self._start_at)}, but not as far "
                              f"as on: {self.watts:.1f} W {now - self._start_at:.0f} s later", self._start_by_bridge,
                              self._start_at)
        self.phase = "standby" if (self.watts or 0.0) <= self.standby_watts else "between"
        self._start_at = None
        return event

    def _step(self, now: float) -> PlugEvent:
        assert self._start_at is not None and self._start_dip is not None
        event = PlugEvent("step", f"its standby draw dipped to {self._start_dip:.1f} W at {self.stamp(self._start_at)}, "
                          f"then {self.watts:.1f} W: something changed inside it, but nothing started it",
                          False, self._start_at)
        self.phase, self._start_at = "standby", None
        return event

    def _went_off(self, at: float, w: float) -> PlugEvent:
        assert self._fall_at is not None
        text = (f"the projector went off: from {self._on_level:.0f} W to {w:.0f} W at {self.stamp(self._fall_at)}; "
                + self._who(self._fall_by_bridge, self._off_at, self._fall_at, "off"))
        event = PlugEvent("off", text, self._fall_by_bridge, self._fall_at)
        self.phase, self._fall_at = "standby", None
        return event

    def _boot_follow(self, at: float, w: float) -> None:
        if w > self._boot_peak:
            self._boot_peak, self._boot_peak_at = w, at
        if w > SETTLED_WATTS:
            self._boot_high_at, self._boot_low_at = at, None
        elif self._boot_low_at is None:
            self._boot_low_at = at

    def _boot_event(self, at: float, ending: str) -> PlugEvent:
        assert self._boot_at is not None
        start = self._boot_at
        text = (f"its power came back at {self.stamp(start)}, and it booted: its draw peaked at {self._boot_peak:.1f} W "
                f"at +{self._boot_peak_at - start:.0f} s")
        if self._boot_low_at is not None and not ending:
            text += f", and was back under {SETTLED_WATTS:g} W from +{self._boot_low_at - start:.0f} s"
        elif ending:
            text += f", {ending} at +{at - start:.0f} s"
        self._boot_at = None
        return PlugEvent("boot", text, None, start)
