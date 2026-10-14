# x100-control: local voice control for the ViewSonic X100-4K

ViewSonic retired the vRemote cloud service in April 2026, which took Alexa and Google voice control
with it. This replaces it with one small box by the projector and nothing in the cloud:

> **"Hey Jarvis, projector on."** … two rising notes, and the projector starts.

```
Projector room: everything on one Raspberry Pi 3

 Jabra Speak 510 ─USB─► x100-listener ──127.0.0.1:8661──► x100-bridge ─USB-RS232─► X100-4K
 (mic and chimes)        1 "Hey Jarvis"         openWakeWord       queues commands      RS-232 port
                         2 "projector on/off"   sherpa-onnx        during warm-up,
                         3 chime                                   polls the state
```

* **Fast.** About half a second from the end of "projector on" to the projector receiving the command,
  measured (see [How well it works](#how-well-it-works)), against an estimated 1.5–3 s through Alexa.
* **Private.** Voice control needs no network at all: mic, Pi and projector are wired together. Audio
  never leaves the Pi, both services are firewalled to your local network, and the wake-word engine's
  Microsoft telemetry is switched off (see [Privacy](#privacy)). The Pi uses Wi-Fi only for setting up,
  updates and SSH.
* **Hard to set off by accident.** It needs the wake word *and* a command straight after it. Speech
  without "Hey Jarvis", chatter after it, a minute of film-like noise: none of it acted in testing.
* **Room to grow.** The bridge can also take commands from your network (a phone shortcut, say), and
  the optional Alexa route runs on the same Pi without moving anything ([Other layouts](#other-layouts)).

## What you can say

Say "Hey Jarvis", then the command, in one breath or with a pause of up to 3.5 seconds.

| Say | Does |
|---|---|
| "Hey Jarvis, projector on" / "…turn on the projector" / "…switch on the projector" | Power on, including from standby |
| "Hey Jarvis, projector off" / "…turn off the projector" / "…turn the projector off" | Power off |

What the chimes mean:

| You hear | Meaning |
|---|---|
| two rising notes | done: the projector accepted it |
| two falling notes | the bridge or the projector said no, or couldn't be reached (the log says which) |
| one low note | heard "Hey Jarvis" but no command after it |

More phrases (inputs, mute, volume) are one line of settings each: see [More commands](#more-commands).

## What you need

* A **Raspberry Pi 3 B or B+** (1 GB; any newer Pi works too), a good **5 V 2.5 A** supply (the Jabra
  and the serial adapter draw power from the Pi) and an 8 GB+ SD card.
* Your **Jabra Speak 510**, plugged in by USB. It's a standard USB sound card, and its speaker plays the
  chimes. If `--test-mic` shows it struggling while a film plays, the step up is the **ReSpeaker
  XVF3800 USB 4-Mic Array** (the plain USB version: the kit with a XIAO ESP32 board ships with
  different firmware), with a small powered speaker on its 3.5 mm jack for the chimes.
* A **USB to RS-232 adapter** with a DB9 plug. It must be true RS-232 (FTDI or Prolific chip), not a
  3.3 V "TTL" serial cable, which is a different thing and can't talk to the projector.
* A **DB9 null-modem cable**, from the adapter to the projector's RS-232 port (DB9). Both ends are
  usually male, so the cable is female to female; check the projector's port before buying. A
  null-modem adapter on a straight cable does the same job.
* Perhaps a **USB extension** for the Jabra: see [placement](#4-placement-and-tuning).
* About an hour.

---

## 1. Projector settings

On the projector: **Setting → Advanced Settings → RS-232 Control → On**. That's all; the projector
doesn't need to be on your network.

## 2. Wire it up

Pi → USB → RS-232 adapter → null-modem cable → the projector's **RS-232** port. Jabra → USB → Pi. The
serial link runs at 115200 baud, 8N1; only pins 2, 3 and 5 are used.

## 3. The Raspberry Pi

**Flash the SD card** with [Raspberry Pi Imager](https://www.raspberrypi.com/software/): device
*Raspberry Pi 3*, OS *Raspberry Pi OS (other) → **Raspberry Pi OS Lite (64-bit)***. It must be the
**64-bit** image: the wake-word engine needs onnxruntime, which has no 32-bit ARM build. In Imager's
settings set the hostname `x100-pi`, the user `x100-controller`, your Wi-Fi, and turn on SSH.

**Install** from your Mac or PC, in the folder with the zip, with the adapter and the Jabra plugged
into the Pi:

```bash
scp x100-control.zip x100-controller@x100-pi.local:
ssh x100-controller@x100-pi.local
sudo apt update && sudo apt install -y unzip    # if the image doesn't have it
unzip x100-control.zip && cd x100-control
sudo deploy/install.sh
```

That installs everything in `/opt/x100-control` and needs no editing afterwards:

* the system packages, a Python 3.11 environment (fetched by `uv` if the OS has a newer Python), and
  the models (about 10 MB, checksum-verified);
* `bridge.env`, with the serial adapter found automatically (its `/dev/serial/by-id/…` name, which
  survives reboots) and the bridge's API kept to the Pi itself;
* `listener.env`, pointed at that bridge, with a freshly generated token that the two share;
* two systemd services, `x100-bridge` and `x100-listener`, running as an unprivileged `x100` user.

Re-run it to upgrade; it keeps your settings and restarts both services, so the new code is running
when it finishes. It ends by printing the release it installed, and you can check that any time:

```bash
/opt/x100-control/.venv/bin/x100ctl --version     # e.g. x100-control 2.0.1 (build 3f9c2a1b, 2026-10-12)
```

Every zip has its own build, a fingerprint of all its files, so the same build means the same code.
The services log the same line when they start (`journalctl -u x100-bridge -u x100-listener`), and
the listener's `--check` says whether the bridge is on the same build.

**Check the projector answers.** Only one program can use the serial port at a time, so stop the
bridge while you do:

```bash
cd /opt/x100-control
sudo systemctl stop x100-bridge
sudo -u x100 .venv/bin/x100ctl --env-file bridge.env probe       # should say "two-way OK"
sudo -u x100 .venv/bin/x100ctl --env-file bridge.env on --wait   # the projector should start
sudo -u x100 .venv/bin/x100ctl --env-file bridge.env learn-inputs
sudo systemctl start x100-bridge
```

`learn-inputs` asks you to select each input with the remote and records the code the projector
reports. ViewSonic only documents HDMI 1 and 2, and you'll want the others for input commands.

**Check the mic.** Likewise, stop the listener first:

```bash
sudo systemctl stop x100-listener
sudo -u x100 .venv/bin/x100-listener --env-file listener.env --list-devices   # the Jabra should be listed
sudo -u x100 .venv/bin/x100-listener --env-file listener.env --test-mic       # live scores, sends nothing
sudo -u x100 .venv/bin/x100-listener --env-file listener.env --check          # models, mic, bridge, power
sudo systemctl start x100-listener
journalctl -f -u x100-listener -u x100-bridge                                   # watch it work
```

## 4. Placement and tuning

**Where the Jabra sits matters more than anything else.** The Pi can stay by the projector, but put
the Jabra on a side table **by the seats, facing you**, and away from whatever plays the film's sound.
If that's the projector's own speakers, a USB extension cable gets the Jabra over to you: up to 5 m
passive, or an active one beyond. The Jabra's echo cancellation only removes what *it* plays; it
can't remove the film.

**`--test-mic`** shows the wake score live, the input level, how long each 80 ms frame takes and
whether the Pi is keeping up:

```
wake 0.97 [###################] level  -31.2 dBFS  frame  21.4 ms  cpu  34%  overruns 0  listening
  12.40 s  wake word (score 0.97)
  13.36 s  heard "projector on" 0.9 s after the wake word -> power=ON
```

1. Say "Hey Jarvis" a few times from your seat. Your scores should land well above `WAKE_THRESHOLD`
   (0.4). If they hover around it, lower it to 0.3.
2. Play a film at your usual volume and watch for a while. The score should stay near zero. If the
   film pushes it over the threshold, raise it (0.5–0.6), move the Jabra, or turn the "didn't catch
   that" note off with `UNCLEAR_CHIME=false`.
3. Say the full commands, with and without the film. If "Hey Jarvis" works but commands are missed,
   try `KWS_BOOST=2.5` or `KWS_THRESHOLD=0.15`.
4. Check the Pi keeps up: frame times well under 80 ms and `overruns 0`. If not: run `--check` (it
   reports under-voltage), compare thread settings (see [Speed on the Pi 3](#speed-on-the-pi-3)), or
   move the SD card to a Pi 4 (same image).
5. Once everything works, try `X100_MIN_GAP=0.03` in `bridge.env` (then
   `sudo systemctl restart x100-bridge`). It shortens the pause the bridge keeps between commands to
   the projector, taking most of 0.1 s off every command. Go back to 0.1 if the projector ever
   ignores one.

Every setting is described in `deploy/listener.env.example` and `deploy/bridge.env.example`.
`SAVE_CLIPS_DIR=clips` keeps a WAV of everything that triggers the listener; replay one with different
settings using `x100-listener --env-file listener.env --wav clips/<file>.wav` (nothing is sent).

## More commands

Add phrases in `listener.env` with `EXTRA_PHRASES`, numbers written as words. The ops are the
bridge's: `power`, `input` (an input name, after `learn-inputs`), `volume` (0–50), `volume_delta`,
`mute`, `key` (a remote key code):

```
EXTRA_PHRASES={"projector HDMI one": [{"op": "input", "value": "HDMI 1"}], "projector HDMI two": [{"op": "input", "value": "HDMI 2"}], "projector mute": [{"op": "mute", "value": true}], "projector unmute": [{"op": "mute", "value": false}], "projector louder": [{"op": "volume_delta", "value": 3}], "projector quieter": [{"op": "volume_delta", "value": -3}]}
```

"Hey Jarvis, projector HDMI two" from standby turns the projector on, waits for it to warm up and
then switches input. Extra phrases weren't part of the accuracy tests below, so try each with
`--test-mic`; phrases that sound alike ("one" and "on") are best avoided.

---

## How well it works

Measured here (`tests/test_voice_e2e.py`). The real wake-word and command models, the real listener,
the real bridge and its API were driven by synthesised speech from 14 voices (British and American,
male and female), with a simulated projector. Two independent sets of clips were drawn with the final
settings, and the ranges below cover both. Since then, power control has been checked on your X100-4K
over RS-232 (see the last point), and seven of the clips gave the same results on your Pi 3 as here
(see [Speed on the Pi 3](#speed-on-the-pi-3)). Not yet tested: a real voice, or the Jabra.

| Condition | Acted correctly |
|---|---|
| Clean speech, five phrasings | 60–64 of 70 (86–91%) |
| Quiet speech (−30 dBFS, a distant talker) | 11–12 of 14 |
| Under film-like noise and chatter, 20 dB below the voice | 12 of 14 |
| …15 dB below | 12–13 of 14 (at most 1 heard as the other command) |
| …10 dB below | 7–10 of 14 (1–2 heard as the other command) |
| Things that aren't commands: speech without "Hey Jarvis", "Hey Jarvis, what time is it?", a minute of film-like noise, a minute of silence | none acted (0 of 58, both times) |

* **Latency:** median **0.52 s** from the end of the phrase to the projector receiving the command,
  90th percentile 0.78 s. That's recognition (0.37 s median) plus the bridge (about 0.1 s), measured
  before the change below. Now "on" from standby goes out at once, and "off" asks two questions
  first, so the bridge's share is a few milliseconds for "on" and about 0.2 s for "off". That 0.2 s is
  the cautious pause kept between commands to the projector, which `X100_MIN_GAP` shortens once you've
  tested your projector. The projector's own response
  time comes on top and isn't known yet; the RS-232 link itself adds well under 20 ms (USB serial
  adapters hold short replies back for a few milliseconds). Frames took at most about 25 ms to process
  here; a Pi 3 is much slower, so it adds a little (see below).
* **Most misses are the wake word**, which on some of the synthetic *British* voices scored as low as
  0.04. The pre-trained "Hey Jarvis" model is the weak link, and your own voice is what counts: check
  your scores with `--test-mic` and set `WAKE_THRESHOLD` from them.
* **Tuning on this data more than doubled quiet-speech success.** Changes: a wider search in the
  command spotter, a wake threshold of 0.4 instead of 0.5, and matching the command's volume to how
  loudly "Hey Jarvis" was said. On the same clips the original settings managed 51 of 70 clean and 5
  of 14 quiet. The cost was about 25 ms of latency.
* **"On" and "off" can be confused under heavy noise** (the "f" gets masked). In practice that does
  nothing: you only say "on" when it's off, so a misheard "off" has nothing to turn off. If the
  projector doesn't react after the two rising notes, say it again.
* **Rhymes:** "Hey *Travis*, projector on" also worked for 5–7 of the 14 voices. It still needs the
  full command straight after, so it won't happen by accident.
* **Your X100-4K's power reporting**, from traces on it (`x100ctl trace`): asked, it says "on" even in
  standby, so the bridge tells standby, starting up and on apart by which questions it answers
  (`x100ctl status` says which). "On" takes effect at once: the fans start, the ViewSonic logo shows
  after about 20 s and the picture after about a minute. An "on" straight after an "off" is held for
  5 s, which worked on yours; commands that need the picture (an input change, say) wait until it has
  been answering for 15 s.
* **Your X100-4K hangs if it's asked things while it boots after a power cut.** After you switch it
  on at the wall it answers only the power question for about 30 s, while its main processor boots.
  Asked anything else then (the bridge polling it, or `x100ctl trace`), it hung every time, whether it
  had been on or in standby before the cut. Hung, it sits in standby and won't start, not even from
  its own remote, until it has been switched off at the wall again. Left alone, it started fine. It
  also hung after an "off" 66 s after "on", just after the picture appeared. So the bridge:
  * asks only the power question once the projector stops answering, every 30 s, and when it answers
    again sends nothing at all for 5 minutes. Commands in that time get the two falling notes, and
    the log says until when; the remote works once the projector has started up;
  * leaves the projector alone for the first 5 minutes after the Pi itself starts, since a mains cut
    takes out both;
  * sends no power command for 2 minutes after the projector starts (by voice, remote or its button):
    an "off" waits, and a repeated "on" isn't sent;
  * never sends "on" by itself, and says "ok" to an "on" only once the projector has visibly started
    (from standby it stops answering about its volume at once), about 3 s after the command.

  Best avoided all the same: switching it off at the wall. If it ever hangs, switch it off at the wall
  for 2 minutes and back on, then leave it 3 minutes before using its remote. `X100_START_HOLD_SECONDS`,
  `X100_POWER_UP_QUIET_SECONDS`, `X100_OUTAGE_PROBE_SECONDS` and the start-up timings
  (`X100_COOLDOWN_SECONDS`, `X100_WARMUP_SECONDS`, `X100_SETTLE_SECONDS`) are in `bridge.env`.

## Speed on the Pi 3

The listener has 80 ms to deal with each 80 ms of audio. Measured on your Pi 3 with the test clips,
milliseconds per frame:

| Cores (`WAKE_THREADS`, `KWS_THREADS`) | 1 | 2 | 4 |
|---|---|---|---|
| Listening for the wake word (a minute of film-like noise) | 48 | 36 | 75 |
| …keeping 1 s of raw audio instead of openWakeWord's 10 s | | **26** | |
| Listening for a command ("Hey Jarvis, projector on") | 29 | **23** | – |

So both engines use 2 cores on a machine with 4 or more (and 1 on smaller ones, where 2 was slower),
and they take turns: the wake word rests while the spotter listens for a command, then starts again
from a quiet room. openWakeWord keeps 10 s of raw audio and copies all of it for every frame, though
it only reads the last 0.11 s; keeping 1 s gives identical scores and took the wake word from 36 to
26 ms. Its extra thread now sleeps between frames: openWakeWord leaves ONNX Runtime's spin-waiting
on, which kept the Pi 3 at 236% of a core for that 26 ms of work. The listener's log shows its CPU use
every 10 minutes (`stats:`). Starting up takes about 17 s. While the mic is missing, the listener waits for it rather than
exiting and restarting, which reloaded the models every few seconds.

To measure it yourself, or to try other settings, run a recording through with `--wav` (nothing is
sent to the bridge). Stop the service first so it isn't competing for the processor:

```bash
sudo systemctl stop x100-listener
cd /opt/x100-control
sudo -u x100 env WAKE_THREADS=1 .venv/bin/x100-listener --env-file listener.env --wav <recording.wav> --seconds 20
```

It ends with how long each kind of frame took, and a verdict: under 80 ms keeps up with a live mic. To
keep a setting, put it in `listener.env`, then `sudo systemctl start x100-listener`. "The frame that
woke it" is always slower, as it replays the last 0.5 s into the spotter; the next frames catch up.

## Troubleshooting

| Symptom | Check |
|---|---|
| `probe`: "No RS-232 answer" | **RS-232 Control** is on (step 1). The cable: null-modem first; if that's what you have, try a straight one, as projectors differ. The adapter is real RS-232, not TTL. `ls /dev/serial/by-id/` shows it, and `X100_SERIAL` in `bridge.env` matches. Then `probe --scan-baud`. |
| `probe` or the bridge: permission denied on the serial port | Run it as `sudo -u x100 …` (the installer put `x100` in the `dialout` group). |
| `probe` answers strangely, or the bridge logs junk | Something else has the port open: `sudo systemctl stop x100-bridge` before using `x100ctl`. |
| Power commands behave oddly (ignored, or "on" straight after "off" does nothing) | With the bridge stopped, `x100ctl --env-file bridge.env trace off` (or `trace on`) sends the command and logs what the projector reports every second until it settles, with a summary at the end. |
| No reaction, no chime | Watch `journalctl -u x100-listener -f` while you speak. No "wake word" line: check scores with `--test-mic`, `WAKE_THRESHOLD`, the Jabra's mute button (red light) and `MIC_DEVICE`. |
| One low note after "Hey Jarvis" | It heard the wake word but not the command. Say the command within 3.5 s, a little closer; or `KWS_BOOST=2.5`. |
| Two falling notes | The log line says why. `BRIDGE_UNREACHABLE`: `x100-bridge` isn't running (`systemctl status x100-bridge`). `UNAUTHORIZED`: `BRIDGE_TOKEN` doesn't match `LOCAL_API_TOKEN`. `ENDPOINT_UNREACHABLE`: the bridge can't reach the projector (cable, adapter, RS-232 Control). |
| Two rising notes but nothing happened | It probably heard the other command (on/off) and there was nothing to do. Say it again. |
| Two falling notes after "on", and the log says the projector "hasn't started" | It acknowledged "on" but stayed in standby: it has probably hung. If its remote can't start it either, switch it off at the wall for 2 minutes, back on, and leave it 3 minutes before using the remote (the bridge stays quiet meanwhile). |
| Two falling notes, and the log says the projector "may still be booting" | It stopped answering for a while (a power cut, say), and questions while it boots can hang it, so the bridge leaves it alone for 5 minutes after it answers again (the log says until when). Its remote works once it has started up. |
| Wondering what the projector does as it powers up | With the bridge stopped: `x100ctl --env-file bridge.env listen` sends nothing and prints anything it says by itself; `trace --power-only` asks only the power question, once a second. Plain `trace` asks everything, which hangs it if it's booting. |
| The low note during films | The soundtrack set off the wake word: raise `WAKE_THRESHOLD`, move the Jabra away from the speakers, or `UNCLEAR_CHIME=false`. |
| `--test-mic` shows overruns, or frames near 80 ms | The Pi is falling behind: `--check` for under-voltage (use the 2.5 A supply), compare `WAKE_THREADS` and `KWS_THREADS` (see [Speed on the Pi 3](#speed-on-the-pi-3)), or a Pi 4. |
| The listener's log says "Waiting for the mic" | It can't find the mic: plug it in (it carries on by itself within 5 s), or check `MIC_DEVICE` against `--list-devices`. |
| `--test-mic` says the device is busy | The service has the mic: `sudo systemctl stop x100-listener` first. |
| Anything else | `LOG_LEVEL=DEBUG` in either settings file. The bridge then logs every byte to and from the projector, and `x100ctl -v raw "…"` sends any frame by hand. |

## Rehearse without the projector

`python -m x100.mock --pty` runs a simulated X100-4K (warm-up, cool-down, refusals in standby) on a
serial device of its own and prints its name. Point `X100_SERIAL` at it and the whole chain, live voice
included, works against the simulator. (`--port 4661` instead simulates the LAN control port.)

## How it behaves

* **Listening:** openWakeWord scores each 80 ms frame for "Hey Jarvis". When it fires, the last
  0.5 s of audio plus the next 3.5 s go to sherpa-onnx's keyword spotter, so a command said in the same
  breath isn't clipped; the wake word rests meanwhile, so a Pi 3 only ever runs one of them. After a
  command there's a 2-second pause before it listens again, and the mic is ignored while a chime plays
  so it can't hear itself.
* **The bridge** answers as soon as the projector accepts the write, except that an "on" is answered
  once the projector has visibly started (see above). During warm-up and cool-down the projector
  refuses most commands, so the bridge queues them and runs them in order once it's ready.
  Turning off cancels anything queued. State is polled every 30 s (every 2 s around changes), so it
  stays right when you use the remote.
* **Audio** stays in the Pi's memory and is dropped as it's processed. `SAVE_CLIPS_DIR` is the only
  thing that writes audio to disk, and it keeps the newest 500 clips.

## Privacy

Nothing leaves the house, and that's enforced rather than just intended:

* **Telemetry off.** openWakeWord runs on onnxruntime, and its official builds upload usage telemetry
  to Microsoft by default. In testing, a process that loaded the wake-word model looked up
  `mobile.events.data.microsoft.com` within 15 seconds. The listener sets `ORT_DISABLE_TELEMETRY=1`
  before onnxruntime loads, and with that set the same test made no network calls at all. The other
  engine (sherpa-onnx) bundles an onnxruntime with no telemetry in it.
* **Firewalled.** Both services may only reach the Pi itself and private LAN addresses
  (`IPAddressAllow` in their unit files). The models are downloaded once by the installer, outside the
  services. If the kernel couldn't enforce this, systemd says so when a service starts:
  `journalctl -b -u x100-listener -u x100-bridge | grep -i firewall` should print nothing.
* **No cloud accounts.** The Alexa route is the one exception, and it's off unless you set it up.

## Security notes

* The bridge's API listens on the Pi only (`127.0.0.1`), and every call except `/healthz` needs the
  token anyway. Opening it to your network (`LOCAL_API_BIND=0.0.0.0:8661`) is for a phone shortcut or
  a listener elsewhere; it's plain HTTP, so keep it to the home network, not a guest one.
* Both services run as the unprivileged `x100` user, with the filesystem read-only apart from their
  own folders (`config/`, `clips/`).

---

## Other layouts

**A phone shortcut or another device controlling the projector.** Set
`LOCAL_API_BIND=0.0.0.0:8661` in `bridge.env` and restart the bridge; the token is
`LOCAL_API_TOKEN` there. For example, an iPhone Shortcut ("Get contents of URL") or:

```bash
curl -H "Authorization: Bearer <token>" -d '{"ops":[{"op":"power","value":"ON"}]}' http://x100-pi.local:8661/command
```

**Alexa as well, later.** No new hardware and no change of layout: the Alexa route is an outbound
connection the Pi's bridge makes to your own AWS account. Follow [alexa/README.md](alexa/README.md),
which ends with `sudo deploy/install.sh --alexa` on the Pi.

**The projector on your network, with the bridge elsewhere.** If the projector's CONTROL port is ever
cabled to the network, the bridge can run on any always-on machine (the Jetson, say) and talk to it
over the LAN:

* **Projector:** Setting → Network Settings → **Wired LAN Control** (Configure IP: Automatic, with a
  DHCP reservation) and **Standby LAN Control → On**. Open `http://<its IP>/` once and set a password
  if asked (the default is `0000`): some LAN modules only start answering after this.
* **Bridge machine:** `sudo deploy/install.sh --bridge`, or Docker:
  `cp deploy/bridge.env.example bridge.env`; set `X100_HOST`, `LOCAL_API_BIND=0.0.0.0:8661` and a
  token (`openssl rand -hex 16`); then `docker compose up -d`. `x100ctl --host <IP> probe` checks the
  projector first; if port 4661 accepts connections but never answers, it explains `X100_ONE_WAY`
  and PJLink.
* **Pi:** `sudo deploy/install.sh --listener`, then set `BRIDGE_URL=http://<bridge>:8661` and
  `BRIDGE_TOKEN` in `listener.env`.

**An ESP32 board instead of the Pi.** Not built yet, but planned for. An ESPHome board (ReSpeaker Lite
with a XIAO ESP32S3, or Home Assistant's Voice Preview Edition) would stream audio over the LAN to the
listener running on a Linux machine. The listener only sees an `AudioSource` and a `ChimePlayer`
(`x100_listener/audio.py`, `chimes.py`), so the board needs those two plus its ESPHome configuration.

## Project layout

```
x100/             projector library: protocol, serial/TCP transports, PJLink, simulator, x100ctl
x100_bridge/      the bridge: command handling and state (core.py), local HTTP API (local_api.py)
x100_listener/    the listener: audio in, wake word, command spotter, state machine, chimes, CLI
deploy/           install.sh, systemd units, settings examples
tools/            check-pi-wheels.sh: checks every listener package has a 64-bit ARM build;
                  make_release.py: builds the zip and stamps its build ID
alexa/            the optional Alexa route: Lambda, CloudFormation, deploy and teardown scripts
tests/            see tests/README.md
Dockerfile, docker-compose.yml    the bridge in Docker, for the LAN layout
```

## Tests

```bash
pip install -e '.[test]' numpy scipy     # Python 3.10 or later (see tests/README.md for 3.9)
pytest
```

This covers:
- the protocol bytes, checked against ViewSonic's published list
- the driver and the bridge over RS-232, against the simulated projector on a pseudo-terminal: the real
  bridge process in the Pi's layout, and `x100ctl probe`
- the same over TCP, for the LAN layout
- the local API
- the listener's state machine with fake engines, and its mic formats (the Jabra's mono, the XVF3800's
  2-channel 32-bit, and 48 kHz)
- the optional Alexa route
- `--version` and the build ID: the same files always give the same ID, and any change a new one

The real-speech test needs a Python 3.11 environment with `.[listener]`, plus downloaded models and
voices: see [tests/README.md](tests/README.md). `tools/check-pi-wheels.sh` checks that every listener
package installs on the Pi without compiling.
