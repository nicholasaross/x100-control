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
    keyword spotter), and posts the command to the bridge. The mic is a Jabra Speak 510 (`hw:2,0`,
    16 kHz mono), whose speaker plays the chimes.
- Nick logs in as `x100-controller`; the services run as `x100`.
- `deploy/install.sh` puts the code in `/opt/x100-control`, with a Python 3.11 venv in `.venv`.
- Settings live in `/opt/x100-control/bridge.env` and `listener.env`. They hold the API token, so
  they never go in the repo, and neither do certs/, models/, clips/ or config/inputs.json.
- Logs: `journalctl -u x100-bridge -u x100-listener -f` on the Pi.
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
  - While starting from standby, only the power query answers, for 30–50 s. Polling during a normal
    start-up is safe.
- **After mains power returns, it hangs if it's asked anything except the power query.**
  - For about 25–30 s its main processor boots and only the power query answers. Then it gives odd
    values (mute on, volume 0/16, input 0x10) and flips between standby and starting.
  - Any other question in that window hangs it. Hung, it sits in standby, acknowledges "on" without
    starting, and ignores its remote and its own button. It stays that way until it has been off at
    the wall and then boots undisturbed.
  - Silent power-ups were fine (tested twice).
- **An "off" 66 s after "on", just as the picture appeared, also hung it.**
- So the bridge:
  - **After any failed reading:** asks only the power query, 2 s after the first failure and then
    every 30 s (`X100_OUTAGE_PROBE_SECONDS`).
  - **Once the projector answers again:** sends nothing at all for 300 s
    (`X100_POWER_UP_QUIET_SECONDS`), and refuses commands meanwhile with a message saying until
    when.
  - **When the Pi has been up for less than 300 s:** starts the same way, since the Pi and the
    projector may have shared the power cut.
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
  `hang_if_off_within`, `power_cut()`, `power_restored()` and `boot_seconds`. Any change to power
  handling needs a test in `tests/test_power_quirks.py` that first shows the hazard in the
  simulator, then shows it prevented.

## Listener notes

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
  tests then skip: 160 passed and 14 skipped last time.
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

## Open items (as of 2026-10-14)

- **Build 94bd0861 (tag `build-94bd0861`) still needs Nick's power-cut test on the real
  projector.**
  1. With the services running, he switches the projector off at the wall.
  2. The bridge log should say it "isn't answering".
  3. After he switches it back on, the log should say "answering again … leaving it alone until …".
  4. After that time, "Hey Jarvis, projector on" should start it.
- **If it hangs anyway,** the lone power query is itself harmful while it boots. The next step is
  `x100ctl listen` during a power-up, to see what the projector says unprompted.
