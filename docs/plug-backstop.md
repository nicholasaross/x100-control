# The plug backstop: a design

**Status:** agreed with Nick on 22 November 2026 (his answers are at the end), and built, in the
simulator, with tests. It ships switched off (`X100_PLUG_BACKSTOP`), to be turned on after the current
hang test. [As built](#as-built), at the end, says where it differs from the design.

## What it's for

Now and then the projector hangs in standby. "On" is acknowledged, but it doesn't start, and the
bridge says so: "It didn't start. It might need switching off at the wall." Someone then has to
power-cycle it, with the plug's button or at the wall, or sometimes the remote gets it out.

The projector's mains already goes through a smart plug, which the bridge reads. The backstop would
let the bridge switch that plug too, so that "Hey Jarvis, projector on" still ends with the
projector on, a few minutes later, without anyone touching anything. The bridge stays primarily a
projector controller: it would use the plug only when the projector needs rebooting.

## What we know

From the bridge's log and the plug's readings, October to November 2026:

- **A hang can be told apart.** In all 5 hung "on"s the plug recorded (9 to 17 November), volume
  still answered 2 s after the "on", which is the bridge's check today. And the projector's draw
  didn't change at all in the 10 s after it. A real start always dips below 10 W within 2-3 s of a
  serial "on" (5.9-9.4 W). So far, every "on" the bridge sent and called a start really started.
- **A power-cycle cleared every hang it was tried on:**
  - 7 November: the plug off for about 30 s;
  - 8 and 9 November: the plug off for 2 minutes, until its `auto_on` timer;
  - 17 November: the projector unplugged for about 9 s.
- **After the power comes back,** the boot shows in the draw for 43-53 s. A remote "on" worked
  46 s after the power returned (17 November), and a serial "on" 1.5 to 5 minutes after (7 November).
  The bridge already waits before asking anything: 60 s on Nick's Pi (`X100_POWER_UP_QUIET_SECONDS`,
  300 s by default).
- **The hazards it must respect** (all in CLAUDE.md):
  - questions in the first 30-34 s of a boot hang it;
  - an "off" soon after a start hung it once;
  - after some power-ups its standby answers as if it were on (1 of 2 on 15 October), and whether
    a serial "on" is safe from there isn't known;
  - cutting the power while it's on is the ordinary wall-switch risk, and best avoided.

## What it would do

1. You say "projector on". The bridge sends "on", as now.
2. It checks, as now, that volume has stopped answering 2 s later. If volume still answers, it
   also waits for the plug: 10 s after the "on", no change in the draw. Both together mean a hang.
3. It answers you at once: "It's hung, so I'm restarting it at the wall. It'll come on in two or
   three minutes." If the draw then shows it reacted after all, it doesn't cut.
4. It switches the plug off, and back on 30 s later. If the bridge fails in between, the plug's own
   `auto_on` brings the power back after 2 minutes anyway.
5. When the power is back, the bridge does what it does after any cut: it waits 60 s, then takes
   one reading.
6. If the reading shows an ordinary standby, it sends "on" again, and checks the projector starts:
   volume stops answering, then the draw rises.
7. If it starts, that's the end. If it doesn't, the bridge stops there. No second power-cycle: it
   tells you at your next command.

```mermaid
flowchart TD
    A["'on' acknowledged"] --> B{"volume still answers at +2 s,<br/>and no change in draw by +10 s?"}
    B -- no --> OK["it started: done, as now"]
    B -- yes --> C{"all the rules allow a cycle?"}
    C -- no --> OLD["today's reply: switch it off at the wall"]
    C -- yes --> D["reply 'restarting'; switch the plug off"]
    D --> E["the bridge switches it back on 30 s later<br/>(the plug's auto_on after 2 min, if not)"]
    E --> F["the usual 60 s wait, then one reading"]
    F --> G{"an ordinary standby?"}
    G -- no --> H["no 'on': the remote, this once"]
    G -- yes --> I["send 'on'; check it starts"]
    I -- starts --> DONE["done"]
    I -- hangs again --> STOP["stop; say so at the next command"]
```

## The rules

It never breaks these:

1. **Automatically, only after both signs of a hang, and only after the bridge's own "on".** A hang
   the remote finds can't be seen: the draw doesn't change when a press doesn't reach it. Or when
   you ask: "Hey Jarvis, restart the projector" (or "reboot the projector"). Either way the other
   rules apply.
2. **Never while the projector draws more than standby** (over 25 W), or has in the last 30 s. If
   the draw changes at any point before the cut (a start, a press of the remote), it doesn't cut.
3. **Never within 120 s of a start or an "off" that the draw showed.** A hung "on" isn't a start:
   the draw shows no reaction.
4. **Automatically, at most once per hang.** If the "on" after a power-cycle hangs too, it stops,
   and doesn't cycle by itself again until the projector has next started. Asking still works.
5. **No daily limit** (Nick's choice): "once per hang" stops it repeating.
6. **The power always comes back.** The bridge switches the plug back on after 30 s
   (`X100_BACKSTOP_OFF_SECONDS`), and if it can't, the plug's own `auto_on` does after 2 minutes.
7. **After the power returns, the same wait and reading as after any cut.** "On" goes out only from
   an ordinary standby. From the standby that answers as if it were on, no "on": the remote, this
   once, as today.
8. **No fresh readings from the plug, or no way to switch it: no cycle,** and today's reply.
9. **Off unless `X100_PLUG_BACKSTOP=true`.** It ships switched off.

## What you'd hear

| When | Reply |
|---|---|
| A hang, and it's restarting | "It's hung, so I'm restarting it at the wall. It'll come on in two or three minutes." |
| "Restart the projector", in standby | "Righto. Restarting it at the wall. It'll come on in two or three minutes." |
| "Restart the projector", while it's on | "It's on, so I won't cut its power. Switch it off first." |
| "Restart the projector", just after a start or an "off" | "It's only just been switched. Try again in a couple of minutes." |
| "Restart the projector", when it can't (no plug control, say) | "I can't restart it from here. Switch it off at the wall." |
| A command while it restarts | "It's restarting. Give it a couple of minutes." |
| At the next command, after a restart that didn't help | "I restarted it, but it still wouldn't start. It needs switching off at the wall." |
| A hang when it may not cycle by itself (the last one didn't help, say) | "It's hung again. Switch it off at the wall this time." |
| It worked | Nothing: the picture comes on. |

The listener hears from the bridge only in answer to a command. So it can't say "it's on now" when
the restart works, unless a later version adds that.

## What the log would show

```
the projector acknowledged power on but hasn't started. It had been in standby for 2.1 h, ...
backstop: volume still answered 2 s after the bridge's 'on'. If its draw shows no change by 10 s after the 'on', it's hung, and the bridge will restart it at the wall
plug: no change in its draw in the 10 s after the bridge's 'on' at 19:30:05 (12.1 W): the projector didn't react to it at all
backstop: the projector is hung (volume still answered 2 s after the bridge's 'on', and its draw showed no change by 10 s): switching its plug off for 30 s
the projector's plug has switched its power off (MQTT): sending the projector nothing until it's back
backstop: switching its plug back on
the projector's plug has switched its power back on (MQTT): leaving the projector alone until 19:31:46, as questions while it boots can hang it
plug: its power came back at 19:30:46, and it booted: its draw peaked at 18.9 W at +35 s, and was back under 14.5 W from +43 s
serial (backstop): power? on (10 ms) · input? no reply (621 ms) · volume? 6 (25 ms) · mute? off (27 ms) · 'on' ok (13 ms)
serial (backstop): volume? no reply (602 ms)
backstop: done: the projector has started (2 min after it began)
plug: the projector came on: a dip to 8.1 W at 19:31:49, then 136 W 15 s later; 2 s after the bridge's 'on'
```

(What the plug reports as `source` when the bridge switches it isn't known yet: "MQTT" here is a
guess.) The hang ends the standby period it hung in, as now. The cut begins a new one, "(the
bridge's backstop switched its plug off and on)". The daily line counts the restarts, and how many
worked: `…, 1 hangs, 1 restarts at the wall (1 worked); …`.

## Switching the plug

Shelly's Gen2 documentation gives two ways to switch the plug over MQTT, through the broker on the
Pi. Both need a setting changed on the plug.

- **MQTT control** (`enable_control`). The plug takes `on`, `off` and `toggle` on
  `<prefix>/command/switch:0`. The documentation also shows a timer added (its example is "on,10"),
  though only on another topic. It can switch the plug, and nothing else.
- **RPC over MQTT** (`enable_rpc`). `Switch.Set` with `toggle_after` (a flip-back timer, in
  seconds) does it in one call, and replies (`was_on`). But the same channel takes any call:
  settings, reboots, firmware. So a broker login that can use it can change anything on the plug.

**Chosen: MQTT control.** It only switches. The bridge sends `off`, then `on` 30 s later: 30 s off
cleared the hang on 7 November. The plug's own `auto_on` brings the power back after 2 minutes, if
the bridge can't. No timer form is needed.

### What would change, all done by Nick (nobody else handles the passwords)

1. **On the plug:** turn on MQTT control, on its web page, with its admin password. RPC over MQTT
   stays off.
2. **In the broker:** a login for the bridge on the Pi's own listener (port 1884). It may publish
   to `x100-plug/command/switch:0` and nothing else. Anonymous clients there still only read.
   That's a change to `/etc/mosquitto/conf.d/x100.conf`, its access list and password file, and to
   `~/x100-mqtt-setup.sh`, which sets them up.
3. **In bridge.env:** the bridge's broker login, and `X100_PLUG_BACKSTOP=true`.

### To check on the plug first

Do these with the bridge stopped and the projector in standby. Each one is a power cut, so the
projector boots each time: leave it 5 minutes afterwards.

- `off` on `x100-plug/command/switch:0`: the plug's status shows its output off. Then `on`: back on.
  Note what `source` it reports each time.
- `off` alone: its `auto_on` brings it back after 2 minutes.
- That a client without the bridge's login can't switch it.

## Building it

- **In the simulator:** a fake plug that cuts the simulated projector's power when told, and
  brings it back after its timer, reporting each change as the real one does.
- **Tests:** in `tests/test_power_quirks.py`, each hazard first shown, then shown prevented:
  - cutting the power while it's on, or just after a start;
  - questions or an "on" during the boot after the cycle;
  - a second cycle in the same hang;
  - the standby that answers as if it were on, after the cycle;
  - the bridge restarting mid-cycle;
  - no fresh readings from the plug;
  - a plug that doesn't switch off when told.
- **Its own log lines and replies,** as above, and their spoken clips.
- **Released switched off,** and turned on only after the current hang test ends, so the test isn't
  muddied by power-cycles.

## Not in this design

- **Telling you when it's back on.**
- **Acting on a hang found any other way.**

## Decided with Nick, 22 November 2026

1. **Automatic, and on request:** both.
2. **MQTT control,** not RPC over MQTT.
3. **Off for 30 s.**
4. **Send "on" again** after the power-cycle.
5. **No daily limit.**
6. **When to turn it on:** not decided. It ships switched off, and the plan is to turn it on after
   the current hang test.

## As built

Built on 22 November 2026, as above, with these differences and details:

- **"It came on"** means volume stopped answering 2 s after the "on", as for any "on". The plug's
  line for the start follows in the log.
- **"About two minutes"** became "two or three minutes": 10 s to see the hang, 30 s off, 60 s for
  the boot, then 30-70 s for the picture.
- **A command while it restarts** gets "It's restarting". "Off" before the cut calls the restart off.
  "Off" after the cut lets the power come back as usual, but no "on" follows. "On" after that puts it
  back.
- **"Restart the projector" just after a start or an "off"** gets its own reply: "It's only just been
  switched. Try again in a couple of minutes." So does a draw that is changing (a remote press, say).
- **A plug that doesn't switch** when told (within 10 s) counts like a restart that didn't help: no
  more by itself until the projector has started, and the next command hears "I can't restart it
  from here".
- **The news** of a restart that didn't help comes with the next command, whatever it is, once. Later
  hangs hear "It's hung again". A start, or 50 W or more from the plug, ends it.
- **A bridge restarted in the middle of one** finds a note in `/var/lib/x100-bridge` (systemd's
  `StateDirectory=`). It leaves the projector alone for the usual quiet, switches the plug back on if
  it's still off, and finishes the restart, "on" included.
- **Found on the way, and fixed:** the 2-minute hold after a start ran from the bridge's "on", even when
  that "on" didn't take and the remote started it later. An "off" could then go out too soon after the
  real start. It now runs from the start the draw shows.
- **Not done yet:** the broker's login for the bridge, and the plug's MQTT control. Both are Nick's
  to do, with the checks above, before it's turned on.
