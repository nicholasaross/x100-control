# CLAUDE.md

Local voice control for Nick's ViewSonic X100-4K projector: "Hey Jarvis, projector on" (or "off"),
with nothing leaving the house. README.md is Nick's guide to setting it up and using it, and
tests/README.md covers the tests. This file holds what a new session needs to know before
changing anything.

## The setup in Nick's house

- A Raspberry Pi 3 (`x100-pi.local`, 64-bit Raspberry Pi OS Lite) runs both services:
  - **`x100-bridge`** is the only thing that talks to the projector. It uses RS-232 through an FTDI
    USB adapter (`/dev/serial/by-id/usb-FTDI_USB_Serial_Converter_FTF4MRDZ-if00-port0`), and
    serves the local HTTP API on 127.0.0.1:8661.
  - **`x100-listener`** hears "Hey Jarvis" (openWakeWord), then "projector on/off" (a sherpa-onnx
    keyword spotter), and posts the command to the bridge. The mic is a Jabra Speak 510 (16 kHz
    mono), whose speaker plays the chimes. The listener finds it by name: it was `hw:2,0`, and
    `hw:1,0` after a reboot on 18 October.
- Nick logs in as `x100-controller`; the services run as `x100`.
- `deploy/install.sh` puts the code in `/opt/x100-control`, with a Python 3.11 venv in `.venv`.
- Settings live in `/opt/x100-control/bridge.env` and `listener.env`. They hold the API token, so
  they never go in the repo, and neither do certs/, models/, clips/ or config/inputs.json.
- Logs: `journalctl -u x100-bridge -u x100-listener -f` on the Pi. Every reading from the plug is in
  `journalctl -u x100-plug-recorder` (below).
  - Since 18 October they survive reboots, through `/etc/systemd/journald.conf.d/50-persistent.conf`
    (`Storage=persistent`, `SystemMaxUse=200M`). Raspberry Pi OS otherwise keeps them in memory only,
    which is how the logs of a crash between 16 and 18 October were lost.
  - The Pi has had its official power supply since 18 October. Before that it logged undervoltage
    many times an hour.
  - The Pi has no clock battery. After a boot its log carries the time it shut down until the
    network corrects the clock, so the first lines of a boot can be days out.
- **The projector's mains goes through a Shelly Plus Plug UK** (Gen2, stable firmware 2.0.3), since
  31 October. The bridge has read it (`X100_PLUG_MQTT`, below) since 17 November.
  - It's set to switch its output on at start-up (`initial_state`), and on again 2 minutes after
    any off (`auto_on`). Its cloud link and its own setup network are off, and an admin password
    is set.
  - It restores the projector's power after a power cut and after an ordinary restart, as tested
    on 2 November. But after its own firmware updates the output stayed off, so don't update it
    without a reason, and check the output after one.
  - Any off or restart of the plug is a power cut for the projector: leave it alone for 5 minutes
    afterwards.
  - It publishes its status over MQTT to Mosquitto on the Pi, under `x100-plug/`. Its MQTT control
    is off. On the network, Mosquitto takes only the plug, signed in. On the Pi itself, anyone can
    read, and nobody can write. So reading the plug needs no password:
    `mosquitto_sub -h 127.0.0.1 -p 1884 -t x100-plug/status/switch:0 -C 1 -W 90` on the Pi. It
    sends about once a minute, and on any change: `output`, `apower` (watts), `source` (what last
    switched it), `counts.switch_on` (how many times it has been switched on) and more.
  - Now and then it drops off the broker and back within a few seconds, without restarting: 4 times
    from 8 to 16 November. Its `source` and `counts.switch_on` then stay as they were. After a
    restart, its `source` is `init`.
- **On its HDMI:** one Roku stick, powered from the projector's USB port, with "1-touch play" (CEC)
  off. A Fire TV stick was removed on 2 November. The projector's "Auto Power On (CEC)" is off.
  The household switches the projector with its own remote.
- Only one program can have the serial port at a time. To use `x100ctl` on the Pi, first run
  `sudo systemctl stop x100-bridge`, then from `/opt/x100-control`:
  `sudo -u x100 .venv/bin/x100ctl --env-file bridge.env <command>`.
- The optional Alexa route (`alexa/`) is off and unused. Its tests still run.

## Code map

- `x100/`: the projector library.
  - `protocol.py`: frames.
  - `transport.py`: serial and TCP.
  - `projector.py`: the driver.
  - `mock.py`: the simulator, including the real projector's quirks.
  - `cli.py`: `x100ctl`.
- `x100_bridge/`:
  - `core.py`: `BridgeCore`, which holds state, deferral, polling and all the power rules below.
  - `local_api.py`: the HTTP API.
  - `plug.py`: reads the plug's MQTT status, and passes it to `BridgeCore`. It never publishes. Also the
    plug recorder (`x100-bridge --record-plug`).
  - `plugwatch.py`: what the projector's draw says it did, for the log only: starts, offs, presses,
    boots, and what came of the bridge's own "on" and "off".
  - `main.py`.
- `x100_listener/`:
  - `listener.py`: a pure state machine.
  - `wake.py`, `commands.py`: the engines.
  - `audio.py`, `chimes.py`, `client.py`.
  - `models.py`: model downloads and checksums.
- `deploy/`: `install.sh`, systemd units and `*.env.example`.
- `tools/make_release.py`: builds the release zip.

## The X100-4K's quirks

These were learnt the hard way, from traces on Nick's projector. They are why the power logic in
`x100_bridge/core.py` looks the way it does. Don't relax any of them without a new trace showing
it's safe, and agree it with Nick first: one mistake hangs the projector until it's switched off at
the wall.

- **Its power query always answers "on", even in standby.** The bridge tells standby, start-up and
  on apart from the other answers:
  - In standby, volume and mute answer but input doesn't.
  - While starting from standby, only the power query answers, for 30–70 s (67 s on 15 October).
    Just before it comes on, it can answer mute on, then input 0x01, for a few seconds. Polling during
    a normal start-up is safe.
- **After mains power returns, it hangs if it's asked anything except the power query.**
  - For about 25–30 s its main processor boots and only the power query answers. Then it gives odd
    values (mute on, volume 0/16, input 0x10) and flips between standby and starting.
  - Any other question in that window hangs it. Hung, it sits in standby, acknowledges "on" without
    starting, and ignores its remote and its own button. It stays that way until it has been off at
    the wall and then boots undisturbed.
  - Silent power-ups were fine (twice before, and twice more on 15 October). It says nothing
    unprompted as it powers up: `x100ctl listen` heard nothing across a 10 s cut on 15 October.
  - Afterwards, its standby sometimes answers everything as if it were on (input HDMI 1, volume 9,
    mute off) until it's next started. Nothing the bridge can ask tells that standby from on. Seen 8
    and 13 minutes after one power-up on 15 October, but not 9 minutes after the next. Its remote
    starts it from there. Whether a serial "on" does is untested.
  - A cut short enough to fall between two polls looks like nothing happened: it answers again by the
    next poll, and booting looks just like starting up. In the simulator, that poll's question about
    the input hangs it. It was first blamed for the 14 October hang, but none has been seen since
    (below).
- **An "off" 66 s after "on", just as the picture appeared, also hung it.**
- **It also hangs in standby, whatever switched it off, and why isn't known yet.** The next "on" is
  acknowledged without starting, and doesn't change its draw at all. A wall power-cycle clears it,
  and once so far its remote did (below).
  - On 14 and 15 October that followed "off"s 17 and 6 minutes after a start, which looked like a
    pattern.
  - But from 18 to 27 October, with clean power and every change logged, it hung in 4 of 9 standby
    periods. The "off"s before them were from the bridge after 91 minutes on, and from the remote
    (or the projector itself) after 2 hours on. Other standby periods, after similar "off"s, were fine.
  - So it was either the projector by itself, or something the bridge does in standby: build
    e776ca5b and later ask "power?" every 2 s, about 43,000 times a day.
  - From 2 to 16 November, with that check once an hour and the CEC traffic cut, it still hung in 4
    of 16 standby periods (7, 8, 9 and 16 November). One came after only 3 checks in 2.6 h. So the
    standby checks aren't the cause. There may have been more standby periods from 2 to 8 November,
    as the bridge doesn't see a remote start, and the plug wasn't being recorded yet.
  - Who sent the "off" may matter. From 18 October to 16 November, a standby that began with the
    bridge's own "off" hung 3 times in 5. One that began any other way (the remote, the projector
    itself, or a cut the bridge thought it saw) hung 5 times in 21. That could still be chance,
    about 1 in 7. After its own "off", the bridge asks everything 7 s later, as it cools down.
  - Until 14 November every standby period had this, hung or not: the bridge asks everything in the
    first minute after the "off", in the poll that sees the standby and the check 15 s later. On 14
    November the bridge missed two remote starts, so it asked nothing after the "off"s that ended
    them, and neither standby hung. Whether that matters isn't known yet. The plug's watts can tell
    standby without asking (below).
  - In standby it runs its fans for about 10 minutes every 35–80 minutes, at 13–15 W. The hangs
    don't follow them. Of the two hung "on"s the plug recorded, one came during a fan run, and one
    33 minutes after one. A hung projector keeps running them.
  - On 16 November, an hour into a hang, its remote got it out without a power-cycle. The first press
    didn't start it, but its draw looked like a boot: a dip to 7 W, then 12–17.5 W. It was still at
    15.5 W 65 s later, and the second press, at 72 s, started it as usual. On 9 and 17 November
    the remote got no reaction at all, in the 2 and 5 minutes after the hang was found.
- **It misses a reply to the power query now and then:** about once a day at one check every 2 s,
  from 21 October. On 24 October it stayed on throughout one, so it wasn't a power cut.
- **It draws about 150 W when on, and about 12 W in standby,** whichever kind of standby (measured
  through the plug on 2 November). Its digital audio port glows red in standby. So the plug's
  reading tells on from standby, but not one standby from another.
  - A start, by a serial "on" or its remote (9 recorded from 11 to 16 November): a dip to 6–9 W,
    then 25–42 W for about 11 s, then about 150 W some 17 s after the "on".
  - An "off" falls below 25 W within a second (once with one reading of 47 W on the way), then to
    about 12 W.
  - From 8 to 16 November standby stayed between 11.5 and 15.4 W, fan runs included.
- **While on, it can be slow to answer about its input:** on 2 November, 31 s after its picture
  appeared, it didn't answer within 0.6 s but did answer about volume and mute. That looks just like
  standby. Perhaps its menu was open, or an input was being unplugged.
- So the bridge:
  - **After any failed reading:** asks only the power query, 2 s after the first failure and then
    every 30 s (`X100_OUTAGE_PROBE_SECONDS`).
  - **Once the projector answers again:** sends nothing at all for 300 s
    (`X100_POWER_UP_QUIET_SECONDS`), and refuses commands meanwhile with a message saying until
    when. Then it takes one full reading, rather than assuming standby: missed replies can look like
    a cut while it stays on.
  - **When the Pi has been up for less than 300 s:** starts the same way, since the Pi and the
    projector may have shared the power cut.
  - **In standby:** asks only the power query, every 2 s (`X100_POWER_CHECK_SECONDS`; Nick's
    bridge.env sets 3600; with `X100_PLUG_STATE` and fresh readings from the plug, there are none),
    and asks the rest only when a command comes.
    When it's on, the same check runs between the 30 s polls. Each check waits 2 s for its answer
    (`power_check_wait`), and silence counts as a cut only after two unanswered tries in a row
    (`power_check_tries`), so one missed reply isn't taken for one. A cut of more than a few
    seconds is seen, and gets the 300 s quiet.
    - The price: a remote start from standby goes unseen until the next command, so an "off" then
      waits the full 120 s, and "on" takes about a second longer, as the bridge reads first.
    - Still at risk: a command within 300 s of a cut too short to see.
  - **Is told it's an X100-4K** by `X100_QUIRKS=true` in bridge.env. Without it, the bridge learns
    that from the first reading that shows a standby or a start. After a power cut that can take until
    the projector is next started, and none of these rules apply until then. install.sh keeps an
    existing bridge.env, so Nick's needs the line adding by hand.
  - **After a power cut, or whenever the bridge starts:** until a reading shows a standby or a start,
    a reading of "on" isn't believed for "on" or an input. They get NOT_IN_OPERATION, saying since
    when and to start it with its remote this once, and nothing is sent. "Off" goes out as usual.
    - Any start counts, not just the Pi's: on 18 October a restart for a new build found that
      standby, hours after a wall power-cycle, and took it for on.
    - The price: after a restart while it really is on, "on" gets that answer instead of "ok".
  - **Counts a start seen from what looked like on** (that standby), so an "off" after it is held as
    usual. For the 120 s after any start it has seen, it keeps reading everything, not just the power
    query, because its answers dip like standby as it starts.
  - **Logs a start it didn't ask for.** When only the power query answers, that line can't tell a
    remote start from a boot after a blip, but it shows what the projector looked like before a hang.
  - **Logs every change in the power state it acts on,** as `projector: OFF -> WARMING (why)`. The
    reason is a command it sent, a reading with what the projector answered, or a cut. The first
    reading after starting has its own line instead ("projector reachable").
  - **Checks a standby reading again when it was on,** unless the bridge has just switched it off.
    It believes the reading only once a second reading agrees: 15 s later (`standby_confirm_seconds`),
    waiting 2 s for the answer about its input (`confirm_input_wait`). A command in between counts as
    that second reading. Otherwise one slow answer would stick, as standby means power checks only.
  - **Says what came before a hang.** "Acknowledged power on but hasn't started" now adds how long it
    had been in standby, how it got there (the bridge, its remote or itself, or a cut), and how many
    power checks, unanswered ones and full readings the bridge made meanwhile. An "on" that doesn't
    start it leaves it in the same standby, so a second try says the same.
  - **Logs the serial traffic in words, unless it's simply on:** `serial (power check): power? on
    (35 ms)`, `serial (command power=OFF): power? on (31 ms) · input? 0x03 (40 ms) · 'off' ok
    (45 ms)`. One line per poll, check or command, just before any change it leads to. Each time runs
    from when the command went out: the 0.1 s pause kept between commands (`X100_MIN_GAP`) isn't in it. Not the polls
    while it's on and nothing's happening, nor standby checks less than 60 s apart
    (`SERIAL_CHECKS_LOGGED_FROM`), which would be thousands a day. Bytes it sends unasked are logged
    too (`the projector sent … bytes nobody was waiting for`), as is a retry on a reopened link.
  - **Logs what the projector's draw says it did,** with a plug (`plugwatch.py`), as `plug: …` lines:
    each start and "off", and whether the bridge caused it (within 10 s of its "on" or "off"); a press
    that reached it without starting it (the dip with no start, as on 16 November); an "on" or "off"
    it ignored; and each boot when its power came back, and how long it took. It acts on none of it.
    - Also steps in its standby draw: minutes after 4 of 8 remote "off"s (8 to 17 November), it dipped
      from about 11.3 W to 9-10 W, then sat at about 10.5 W (or went back to 12-13 W). Nothing pressed
      anything. A press draws over 16 W within 20 s of its dip; a step doesn't. Not a sign of a hang:
      none came in the 3 standbys that hung, but none came in 4 of the 8 that didn't either.
    - Replayed over the plug's readings from 8 to 17 November, it found the 11 starts and 12 "off"s
      with the right cause, the 5 "on"s a hang ignored, the one press, 5 steps and 3 boots.
  - **Logs each standby period as it ends:** `standby period: 2.4 h from 10:53:56 on 17 Nov (the
    bridge switched it off; its draw fell at 10:53:58); meanwhile 3 power checks (0 unanswered), 2 full
    readings (at +7 s, +2.4 h), 1 commands; ended by the bridge's 'on': it was acknowledged, but the
    projector didn't start`. With the plug, its own start and "off" times are used, so remote ones count too.
  - **Once a day (`summary_seconds`), logs what it asked and missed:** `in the last 24 h: … power
    checks (… unanswered), … full readings, … commands, … outages, … hangs; the plug saw … starts (…
    by the bridge), … offs (… by the bridge), …; OFF … h, ON … h`.
  - **Reads the plug, if `X100_PLUG_MQTT` and `X100_PLUG_PREFIX` are set.** It subscribes to
    `<prefix>/status/switch:0` and `<prefix>/online`, and only reads.
    - While the plug's output is off, it sends nothing, and refuses commands saying so. When the
      output comes back on, it gives the same 300 s quiet as after any cut.
    - The same while the plug's output is on but the projector draws 3 W or less
      (`X100_PLUG_UNPLUGGED_WATTS`): it has no power, unplugged from the plug, say. The quiet then
      starts once it draws power again. On 17 November it was unplugged for about 1 s, which the
      bridge didn't see, and 34 s into the boot a command made it ask everything. Standby has never
      read below 11.5 W, and the dip as it starts never below 5.6 W. The plug's first readings
      after its own power comes back can be 0 W too, for about a second.
    - A plug that reconnects to the broker (a live `online true`, not the retained one) may have
      restarted, which cuts the power. It sends nothing until the plug's next reading, for up to
      10 s (`plug_check_seconds`). Then it takes it as a cut, unless that reading shows the plug kept
      running: last switched by something other than `init`, and switched on no more times than
      before. Without a reading, or with either fact missing, it's a cut.
    - A reading at any time with more switch-ons than the last one means the plug switched off and
      on unseen (its button, say, while it was out of touch): that's a cut too.
    - Either way, if the projector is drawing 50 W or more (`plug_on_watts`), it's on and past
      booting, so it isn't treated as a cut.
    - If the broker can't be reached, or the settings are wrong, it logs that once and carries on
      as without a plug.
  - **Can tell on from standby by the plug's watts, with `X100_PLUG_STATE=true`.** It's an
    experiment, off by default. While the plug's readings are fresh, the bridge asks the projector
    nothing except what a command needs: no polls, no power checks, and no reading before "on".
    - The bands: standby at 25 W or less (`X100_PLUG_STANDBY_WATTS`), on at 50 W or more
      (`X100_PLUG_ON_WATTS`). In between, it's starting up or going off, whichever way it was
      heading. In between for longer than 90 s (`plug_follow_seconds`), it's on (a dim picture, say),
      unless the bridge switched it off. The real draw while starting and stopping isn't known yet.
    - After "on", it still checks once that volume has stopped answering, so a hang gets an answer
      at once.
    - It still takes one full reading when it starts and after the quiet that follows a cut. The
      watts can't tell the standby that answers like on from an ordinary one, and whether a serial
      "on" is safe from that one isn't known. If the reading shows it, "on" is refused with
      `looks_on` until the projector has been started with its remote.
    - A start by its remote is seen from the watts, and starts the 120 s hold.
    - With no reading for 180 s (`plug_stale_seconds`; the plug sends one about once a minute), it
      asks the projector as without the plug, and logs that. It logs again when the readings are back.
    - The daily line and the hang line count the readings from the plug.
  - **For 120 s after a start (`X100_START_HOLD_SECONDS`):** sends no power command. An "off"
    waits, and a repeated "on" isn't sent.
  - **Never resends "on" by itself.** 2 s after "on" it checks whether volume still answers. If it
    does, the projector is still in standby, and the bridge reports that it hasn't started.
- **To recover a hung projector,** first try its remote: press once, wait about 70 s, and press
  again. That worked on 16 November, but not on 9 November. If it still doesn't start, switch it off
  at the wall for 2 minutes, then back on. Leave it 3 minutes before using its remote. The bridge
  stays quiet meanwhile.
- **For investigating, with the bridge stopped:**
  - `x100ctl listen` sends nothing and prints whatever the projector says unprompted.
  - `x100ctl trace --power-only` asks only the power query.
  - Plain `trace` asks everything, which hangs it while it boots.
- **`x100/mock.py` reproduces all of this in quirks mode,** through `hang_if_asked_while_booting`,
  `hang_if_off_within`, `cold_standby_after_power_up`, `power_cut()`, `power_restored()` and
  `boot_seconds`. `hang_if_asked_after_off` is a guess, not from any trace: the idea the watts are
  there to test. Any change to power
  handling needs a test in `tests/test_power_quirks.py` that first shows the hazard in the
  simulator, then shows it prevented.

## Listener notes

- **Chimes:** two rising notes for ok, two level notes for queued (accepted, but waiting until the
  projector is ready, such as an "off" in the 2 minutes after a start), two falling notes for fail,
  and one low note when no command followed "Hey Jarvis".
  - The low note is off on Nick's Pi since 22 November (`UNCLEAR_CHIME=false` in listener.env; the old
    file is kept beside it). From 2 to 22 November "Hey Jarvis" fired 1,022 times, and only 35 were
    followed by a command: about 50 false wakes a day, from voices in the room, as often with the
    projector off as on. The note helped only 4 times, when a real command wasn't caught. Without it,
    a missing "ok" chime says "say it again".
  - Raising `WAKE_THRESHOLD` (0.4) would miss Nick: 8 of his 35 real wakes scored under 0.5.
- **Spoken replies (`replies.py`) stand in for the level and falling notes.** Nick chose the voice on
  7 November 2026: Piper's semaine voice, speaker 2 ("Obadiah"), 3.5 semitones lower at the same
  pace, with a little growl and gravel ("gruff"; the "sillier" 6.5 semitones was too much).
  - The bridge gives each refusal a `reason` ("plug_off", "booting", …: `BridgeError`), and the
    listener picks the line from that, or from the code when there's no reason.
  - While it's left alone after a power cut, the reason is "booting" ("Give it a few minutes"), or with
    90 s of the wait left or less, "booting_minute" ("Give it a minute"): all of Nick's 60 s wait.
  - Its log says what it played after each command: `said "It didn't start. …"`, or the chime.
  - Clips are made once by `x100-listener --make-replies`, which install.sh runs. It fetches the
    voice first (80 MB, checksummed, unpacked only inside its own folder). They're named by a hash
    of the line, voice and effect, so changing any of them makes fresh clips at the next install.
  - A reply with no clip plays its chime. `REPLIES=chimes` turns the voice off.
  - Speech peaks at twice `CHIME_VOLUME`, as it sounds quieter than a tone at the same peak. Not yet
    heard on the Jabra.
- **On the Pi 3**, the wake word takes about 26 ms per 80 ms frame and the command spotter about
  24 ms.
- **Keep ONNX intra-op spinning off** (`allow_spinning=0` in `wake.py`). With it on, the listener
  used 236% CPU.
- **Thread defaults:** `WAKE_THREADS` and `KWS_THREADS` default to 2 on machines with 4 or more
  cores.
- **Wake word and spotter:** the wake word rests while the spotter listens. openWakeWord's raw
  buffer is capped at 16000 samples, which gives identical scores.
- **A missing mic:** the listener waits for it, and loads its models once the mic appears.

## Tests and checks

The code runs on Python 3.11 only, as the Pi does: `requires-python` in pyproject.toml says so,
and `.python-version` points uv at it. The listener needs it, as tflite-runtime has no Pi builds for
anything newer. Nick decided on 7 November 2026 to stay on 3.11 for the foreseeable future, so test
on 3.11 alone, and write code for it. tests/README.md has the details, including the real-speech tests.

    uv venv -p 3.11 .venv311                     # the Pi's Python; uv downloads it if needed
    uv pip install -p .venv311/bin/python -e '.[test]' numpy scipy
    .venv311/bin/python -m pytest -q

- **pip refuses other Pythons, but uv doesn't:** uv ignores the upper bound in `requires-python`.
  install.sh asks uv for 3.11 by name, and here `.python-version` makes it uv's default.
- **Two exceptions run elsewhere:** `tools/make_release.py` runs under the Mac's own `python3`
  (3.13 now), using only the standard library. And the unused Alexa Lambda
  (`alexa/infra/template.yaml`) runs on AWS's Python 3.13.
- **Other skips need setting up first.** The Alexa schema tests and the real-speech tests skip until
  that's done, as tests/README.md describes.

    ruff check --select E4,E7,E9,F .                       # clean
    shellcheck deploy/install.sh tools/check-pi-wheels.sh  # clean

## Releasing to the Pi

- **Build:** `python3 tools/make_release.py` writes `x100-control.zip` in the folder above the repo.
  On Nick's Mac the repo is `~/Projects/x100-control`, so the zip is `~/Projects/x100-control.zip`.
  - The zip is stamped with a build ID, a hash of the released files. `x100ctl --version` and the
    service logs show it.
  - Build from a clean, committed tree.
  - Once Nick installs a build, tag its commit `build-<id>`.
- **Install,** from the repo folder on Nick's Mac:

      scp ../x100-control.zip x100-controller@x100-pi.local:
      ssh x100-controller@x100-pi.local
      rm -rf x100-control && unzip -q x100-control.zip && cd x100-control && sudo deploy/install.sh

  - Builds up to 3dc308bf left a root-owned cache file in the unpacked folder: their package check
    ran Python as root from it, which also imported the unpacked copy, not the installed one. So the
    first install after them needs `sudo rm -rf x100-control`; the Pi's folder has that file now.
    install.sh now runs its Python with `-P` and clears any root-owned cache it finds there, so a
    plain `rm` works from then on.

  - `install.sh` keeps bridge.env, listener.env, config/, models and clips.
  - It ends by printing the installed build ID. Check that it matches the one make_release printed.
  - Older zips from before the move may still be in Nick's home folder. Never send those.
- **Install over SSH:** Nick's SSH key is on the Pi, so `scp` and read-only `ssh` commands work from
  his Mac. sudo there needs his password (`sudo -n true` fails). So run the install in a tab of his
  Terminal panel, as `ssh -t x100-controller@x100-pi.local 'rm -rf x100-control && unzip -q
  x100-control.zip && cd x100-control && sudo deploy/install.sh'` (`sudo rm` the first time, above),
  and he types the password there.
  It ends with `Installed: … (build <id>, …)` and `Connection to x100-pi.local closed.`
- **Tags so far:** `build-94bd0861`, `build-e776ca5b`, `build-df034892`, `build-91c5549f`,
  `build-bbc37ab4`, `build-2d0dd24e`, and `build-3dc308bf`, the build installed since 17:34 on
  17 November.

## Working with Nick

- Nick runs commands on the Pi from his Mac's Terminal. For each step, give one command, the state
  things should be in first, and the output to expect.
- He tests on the real projector, so say what he should see and hear (the projector, the chimes)
  at each step.
- Ask him before anything that sends commands to the projector, restarts the services or installs
  a build. Reading logs and `--version` is fine.
- Never handle his passwords: not sudo's, and not the plug's admin password. When one is needed,
  run the command in a tab of his Terminal panel so he types it there, then read the result from
  the tab. Don't store a password anywhere to use later.
- Docs and messages use British spelling and plain, short sentences.
- Work on a branch and open a pull request. `main` is what gets released.

## The repo is public

- It's `nicholasaross/x100-control` on GitHub, under the MIT licence. Anyone can read every file,
  commit message and pull request, so write them all for that.
- Never commit tokens, keys or `*.env` files. Add no more about Nick's house or network than is
  already here.
- Commits use Nick's GitHub noreply address, `67790949+nicholasaross@users.noreply.github.com`.
  His Mac's git settings already do. Before pushing, check with `git log -1 --format=%ae`.
- The old private repo, `nicholasaross/x100-control-private`, is archived. Don't push to it. The
  merge messages for "pull request #1" and "#2" in `main`'s early history refer to PRs there.

## Open items (as of 2026-11-17)

- **Watching the standby hangs.** From 17:06 on 2 November to 17 November it hung in 5 of 18
  standby periods (7, 8, 9, 16 and 17 November), with build bbc37ab4, hourly checks
  (`X100_POWER_CHECK_SECONDS=3600`), and the CEC traffic cut (the Fire TV stick removed, the
  Roku's "1-touch play" off). From 18 to 27 October, at one check every 2 s, it was 4 of 9. So
  neither change stopped it.
  - **The next phase, since 16:53 on 17 November:** build 2d0dd24e (3dc308bf since 17:34: the
    "Give it a minute" reply, and serial times without the pause between commands), with `X100_PLUG_MQTT=127.0.0.1:1884`,
    `X100_PLUG_PREFIX=x100-plug`, `X100_PLUG_STATE=true` and `X100_POWER_UP_QUIET_SECONDS=60` added
    to bridge.env (the old one is kept beside it, as `bridge.env.bak-20261117-164935`). While the
    plug's readings are fresh, the bridge asks the projector nothing in standby, and nothing after an
    "off". It still sends its own serial "off", which since 18 October has been followed by a hang
    4 times in 6, against 5 times in 22 for other "off"s. If hangs after the bridge's "off" carry on,
    the serial "off" itself is the suspect; if they stop, the questions after it were. If the other
    "off"s still hang too, the projector does it by itself: look next at its firmware and its
    standby settings. RS-232 control has to stay on.
  - At the next hang, read the plug before anyone power-cycles it (MQTT, above), then try the
    remote twice, about 70 s apart.
  - The 60 s wait after the power returns, in place of 300 s: the longest boot after a power-up
    seen so far (53 s) and a few seconds more. When the wait ends the bridge asks everything at
    once, so this is when the first questions arrive. The evidence, in seconds after the power
    returns:
    - 0–30: only the power query answers, and questions every second from the start hung it twice.
      On 17 November it was still answering only the power query at 34 s.
    - 0–53: the boot shows in the plug's watts. On 9 November: a peak of 19.6 W at 33 s, then a
      steady 12 W from 53 s. On 17 November: a peak of 18.9 W at 36 s, and 12.8 W by 43 s.
    - 46: on 17 November its remote started it as usual 46 s after the power returned.
    - 80 to 300: on 7 November a full reading and "on" came somewhere in this range after a plug
      power-cycle, and were fine. That's the earliest question known to be safe.
    - 480 and more: full readings 8, 9 and 13 minutes after power-ups on 15 October were fine.
    - The 300 s came with the first build, with no reason recorded.
    - Not a power-up: on 16 November, after a remote press out of a hang, a boot-like draw was
      still going at 65 s, when the second press came at 72 s.

    The wait never starts before the power returns: it counts from the plug's report, the
    projector's first answer to a check, or the Pi's own start-up. To show 60 s safe directly: stop
    the bridge, press the plug's button (it comes back on after 2 minutes), run `x100ctl status`
    60 s after it does, then start the projector with its remote.

  To count them:
  - `journalctl -u x100-bridge | grep "standby period:"`: one line per standby period, saying how
    and when it began, what was asked meanwhile, and how it ended (started, hung, or a cut).
  - The `plug:` lines show every start and "off", by the bridge or not, and each "on" a hang ignored.
  - The daily `in the last 24 h:` lines give the totals, hangs included.
- **Recordings of the plug, on the Pi.** The `x100-plug-recorder` service has recorded every message
  from the plug since 16:53 on 17 November: `journalctl -u x100-plug-recorder`, one line each, and
  it survives restarts. It shows every start and "off", however made, each fan run and each boot.
  - Before it, from 20:11 on 8 November to 16:59 on 17 November, a temporary `mosquitto_sub`
    recorded the same to `~/plug-capture.log` (11.6 MB), the source of most of November's findings.
    It was stopped with Nick's OK. Keep the log: delete it only with his OK.
- **The bridge has read the plug since 17 November.** It sees the plug's off and on, its restarts,
  and the projector being unplugged from it, so a cut between hourly checks is no longer missed.
  - Why: on 7 November it hung in standby, and Nick power-cycled it with the plug's button, off for
    about 30 s. With hourly checks the bridge saw nothing, so an "on" while it booted would have gone
    straight to it. His "on" came 1.5 to 5 minutes later, and worked. On 2 November a restart of the
    plug for a settings change went unseen the same way.
  - Still possible later, with Nick's agreement and tests: power-cycling a hang. The design for it,
    for Nick's review, is in `docs/plug-backstop.md`: its rules, what you'd hear, the plug and broker
    changes it needs, and its open questions. None of it is built.
- **Telling on from standby by the plug's watts (`X100_PLUG_STATE`), on since 17 November.** It tests
  the idea that the questions just after an "off" hang it (above).
  - The 25 W and 50 W bands hold against the starts and "off"s recorded from 8 to 16 November (its
    draw, above).
- **A late answer in standby, 17 November.** Twice, at 16:53:54 and 17:34:45, the first reading after
  a restart for a new build got 9 bytes just after its question about the input had timed out
  (600 ms): a reply worth 6 (`05 14 00 03 00 00 00 06 1d`). So its standby seems to answer "input?"
  slowly, with 0x06, rather than not at all. If more appear (`nobody was
  waiting for` in the log), a long wait for that answer could read standby as on: the standby recheck
  waits 2 s (`confirm_input_wait`). With `X100_PLUG_STATE` and fresh readings there are no rechecks.
- **A crash between 16 and 18 October** happened before the journal was persistent. Nick doesn't
  remember the details, so it can't be placed.
- **Untested: a serial "on" from the standby that looks on** after a power cut. Until it's known to
  be safe, the bridge refuses "on" there and asks for the remote. To test it, stop the bridge, cut
  the power at the wall for 10 s, and after 8 minutes, if `x100ctl trace` shows "on" with the
  projector in standby, run `trace on`. That state came after only one of two power-ups.
- **Not yet shown on the real projector:** that the power query every 2 s is harmless while it boots.
  So far only a single power query during a boot is known to be fine. `x100ctl trace --power-only
  --interval 2` across a power-up would show it.
