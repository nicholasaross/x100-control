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
- Logs: `journalctl -u x100-bridge -u x100-listener -f` on the Pi.
  - Since 18 October they survive reboots, through `/etc/systemd/journald.conf.d/50-persistent.conf`
    (`Storage=persistent`, `SystemMaxUse=200M`). Raspberry Pi OS otherwise keeps them in memory only,
    which is how the logs of a crash between 16 and 18 October were lost.
  - The Pi has had its official power supply since 18 October. Before that it logged undervoltage
    many times an hour.
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
  acknowledged without starting, and a wall power-cycle clears it.
  - On 14 and 15 October that followed "off"s 17 and 6 minutes after a start, which looked like a
    pattern.
  - But from 18 to 27 October, with clean power and every change logged, it hung in 4 of 9 standby
    periods. The "off"s before them were from the bridge after 91 minutes on, and from the remote
    (or the projector itself) after 2 hours on. Other standby periods, after similar "off"s, were fine.
  - So it's either the projector by itself, or something the bridge does in standby: build
    e776ca5b and later ask "power?" every 2 s, about 43,000 times a day. The open items have the
    week of watching that tells them apart.
- **It misses a reply to the power query now and then:** about once a day at one check every 2 s,
  from 21 October. On 24 October it stayed on throughout one, so it wasn't a power cut.
- So the bridge:
  - **After any failed reading:** asks only the power query, 2 s after the first failure and then
    every 30 s (`X100_OUTAGE_PROBE_SECONDS`).
  - **Once the projector answers again:** sends nothing at all for 300 s
    (`X100_POWER_UP_QUIET_SECONDS`), and refuses commands meanwhile with a message saying until
    when. Then it takes one full reading, rather than assuming standby: missed replies can look like
    a cut while it stays on.
  - **When the Pi has been up for less than 300 s:** starts the same way, since the Pi and the
    projector may have shared the power cut.
  - **In standby:** asks only the power query, every 2 s (`X100_POWER_CHECK_SECONDS`), and asks the
    rest only when a command comes. When it's on, the same check runs between the 30 s polls. Each
    check waits 2 s for its answer (`power_check_wait`), and silence counts as a cut only after two
    unanswered tries in a row (`power_check_tries`), so one missed reply isn't taken for one. A cut of
    more than a few seconds is seen, and gets the 300 s quiet.
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
  - **Says what came before a hang.** "Acknowledged power on but hasn't started" now adds how long it
    had been in standby, how it got there (the bridge, its remote or itself, or a cut), and how many
    power checks, unanswered ones and full readings the bridge made meanwhile.
  - **Once a day (`summary_seconds`), logs what it asked and missed:** `in the last 24 h: … power
    checks (… unanswered), … full readings, … commands, … outages; OFF … h, ON … h`.
  - **For 120 s after a start (`X100_START_HOLD_SECONDS`):** sends no power command. An "off"
    waits, and a repeated "on" isn't sent.
  - **Never resends "on" by itself.** 2 s after "on" it checks whether volume still answers. If it
    does, the projector is still in standby, and the bridge reports that it hasn't started.
- **To recover a hung projector,** switch it off at the wall for 2 minutes, then back on. Leave it
  3 minutes before using its remote. The bridge stays quiet meanwhile.
- **For investigating, with the bridge stopped:**
  - `x100ctl listen` sends nothing and prints whatever the projector says unprompted.
  - `x100ctl trace --power-only` asks only the power query.
  - Plain `trace` asks everything, which hangs it while it boots.
- **`x100/mock.py` reproduces all of this in quirks mode,** through `hang_if_asked_while_booting`,
  `hang_if_off_within`, `cold_standby_after_power_up`, `power_cut()`, `power_restored()` and
  `boot_seconds`. Any change to power
  handling needs a test in `tests/test_power_quirks.py` that first shows the hazard in the
  simulator, then shows it prevented.

## Listener notes

- **Chimes:** two rising notes for ok, two level notes for queued (accepted, but waiting until the
  projector is ready, such as an "off" in the 2 minutes after a start), two falling notes for fail,
  and one low note when no command followed "Hey Jarvis".
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

The code supports Python 3.9 and up, and the Pi runs 3.11. Before a release, run the suite under
3.9, 3.11 and 3.13. tests/README.md has the details, including the real-speech tests.

    uv venv -p 3.11 .venv311                     # the Pi's Python; uv downloads it if needed
    uv pip install -p .venv311/bin/python -e '.[test]' numpy scipy
    .venv311/bin/python -m pytest -q

- **The `test` extra needs Python 3.10 or later,** because amqtt does. A Mac's built-in `python3`
  may be 3.9.
- **To test on 3.9,** install the package with `pytest` instead of the extra. The MQTT and listener
  tests then skip: 178 passed and 14 skipped last time.
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

  - `install.sh` keeps bridge.env, listener.env, config/, models and clips.
  - It ends by printing the installed build ID. Check that it matches the one make_release printed.
  - Older zips from before the move may still be in Nick's home folder. Never send those.
- **Install over SSH:** if Nick has put his SSH key on the Pi, these steps can run as
  `ssh x100-controller@x100-pi.local '...'`. That works only if `sudo -n true` succeeds there.
  Otherwise, ask him to run `install.sh` himself.

## Working with Nick

- Nick runs commands on the Pi from his Mac's Terminal. For each step, give one command, the state
  things should be in first, and the output to expect.
- He tests on the real projector, so say what he should see and hear (the projector, the chimes)
  at each step.
- Ask him before anything that sends commands to the projector, restarts the services or installs
  a build. Reading logs and `--version` is fine.
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

## Open items (as of 2026-10-30)

- **A week of watching, from when the Pi is next switched on.** The build installed on 30 October
  runs with `X100_POWER_CHECK_SECONDS=3600` in bridge.env, so in standby the bridge asks "power?"
  about once an hour instead of every 2 s. Nick powered the Pi down straight after installing it,
  so the week starts when it's back on.
  - Compare with 18 to 27 October, at one check every 2 s, when it hung in 4 of 9 standby periods.
  - Each hang's log line now says how long it had been in standby, how it got there and what was
    asked meanwhile. The daily summary line gives the totals.
  - If it still hangs as often, the projector does it by itself: look next at its firmware and its
    standby settings. If it stops, the 2 s checks are implicated.
  - Meanwhile, a cut of a few seconds between hourly checks goes unseen, so a command within 5
    minutes of one could hang it: the hazard the 2 s checks were for.
- **A crash between 16 and 18 October** happened before the journal was persistent. Nick doesn't
  remember the details, so it can't be placed.
- **Untested: a serial "on" from the standby that looks on** after a power cut. Until it's known to
  be safe, the bridge refuses "on" there and asks for the remote. To test it, stop the bridge, cut
  the power at the wall for 10 s, and after 8 minutes, if `x100ctl trace` shows "on" with the
  projector in standby, run `trace on`. That state came after only one of two power-ups.
- **Not yet shown on the real projector:** that the power query every 2 s is harmless while it boots.
  So far only a single power query during a boot is known to be fine. `x100ctl trace --power-only
  --interval 2` across a power-up would show it.
