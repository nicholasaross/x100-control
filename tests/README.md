# Tests

    pip install -e '.[test]' numpy scipy
    pytest

* `test_protocol.py` – encoder/decoder checked byte-for-byte against ViewSonic's published command list.
* `test_serial.py` – the RS-232 path, as on the Pi that does everything: the driver, the bridge (with
  its real 0.1 s pause between commands), the real `x100-bridge` process and `x100ctl probe`, all
  against the simulated projector on a pseudo-terminal.
* `test_projector.py` – driver against the simulated projector over real TCP (reconnects, line noise,
  one-way links, PJLink).
* `test_bridge.py` – bridge logic: deferral during warm-up, auto power-on, errors, one-way mode.
* `test_power_quirks.py` – the X100-4K's power reporting, as traced on a real one (its power query says
  "on" in standby too), against the simulator's quirks mode: telling standby, start-up and on apart, an
  "on" straight after an "off", start-up hiccups, an ignored "on", the remote, and `x100ctl` on it. Also
  the hangs the real one had: after an early "off", and when asked things while booting after a power
  cut. Each is reproduced in the simulator, then shown prevented: the bridge holds back power commands
  after a start, and after a power cut asks only about power and then leaves the projector alone.
* `test_local_api.py` – the bridge's HTTP API against the real core and the simulated projector: auth,
  malformed requests, deferral, errors, timeouts, the ack going out before the state read-back, the
  listener's client, and the real `x100-bridge` process in local-only mode.
* `test_listener.py` – the listener's state machine with fake engines (wake → command → dispatch,
  timeouts, cooldown, pre-roll, chimes muting the mic, level matching, the wake word resting while the
  spotter listens), mic formats (mono, 2-channel 32-bit, 48 kHz decimation, overruns), waiting for a
  missing mic, settings, the `--wav` speed report, model checksums and the Pi power check. Needs numpy
  and scipy; skipped without them.
* `test_voice_e2e.py` – real speech end to end (below), plus the real wake word's reset and thread
  settings. Skipped unless its models and voices are set up.
* `test_lambda.py` – the optional Alexa route: directives through the Lambda, and full voice sessions
  (Lambda → fake IoT → real bridge → TCP → simulated projector).
* `test_version.py` – `--version` on all three programs, and the release build ID: which files ship,
  that any change gives a new ID while your settings don't, and that the zip keeps the exec bits.
* `test_mqtt_integration.py` – the real bridge process over MQTT, including mutual TLS with a client
  certificate (skipped if `amqtt` isn't installed).

## Real speech, end to end

`test_voice_e2e.py` synthesises "Hey Jarvis, projector on" and friends with 14 Piper voices, mixes in
film-like noise, and runs them through the real openWakeWord and sherpa-onnx models, the listener, the
bridge's API and the simulated projector. It reports accuracy and latency (run with `-s` to see the
tables). It needs Python 3.11 (openWakeWord's tflite-runtime dependency stops there):

    uv venv -p 3.11 .venv311
    uv pip install -p .venv311/bin/python -e '.[listener,test]'
    MODELS_DIR=test-models .venv311/bin/x100-listener --download-models

    mkdir -p test-voices && cd test-voices
    for v in en_GB-vctk-medium en_US-libritts_r-medium en_GB-alan-medium en_US-lessac-medium; do
      curl -fsSL https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/vits-piper-$v.tar.bz2 | tar xj
    done
    cd ..

    X100_TEST_MODELS=test-models X100_TEST_VOICES=test-voices .venv311/bin/python -m pytest -s tests/test_voice_e2e.py

The voices are about 340 MB. Synthesised clips are cached in `tests/data/e2e-cache` (or
`X100_TEST_CACHE`), so reruns hear the same audio; delete the folder for a fresh draw. The pass marks
sit a little below what was measured, so they catch regressions rather than promise accuracy for a
real voice.

## Alexa schema

Lambda responses are validated against Amazon's published Smart Home message schema when it's available:

    mkdir -p tests/data
    curl -fsSL -o tests/data/alexa_smart_home_message_schema.json \
      https://raw.githubusercontent.com/alexa/alexa-smarthome/master/validation_schemas/alexa_smart_home_message_schema.json
