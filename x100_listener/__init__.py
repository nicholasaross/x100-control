"""x100-listener: always-on wake word plus command spotting for the X100-4K bridge.

Runs on a Raspberry Pi (or any Linux box) with a USB mic. Hears
"Hey Jarvis, projector on" and sends the command to x100-bridge's local API.
Audio never leaves the machine.
"""

import os as _os

# The official onnxruntime builds (openWakeWord's engine) upload usage telemetry to Microsoft
# by default; with it on, a listener looked up mobile.events.data.microsoft.com within seconds.
# This must be set before onnxruntime loads, so it lives here, at package import.
_os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")
