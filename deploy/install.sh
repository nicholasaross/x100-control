#!/usr/bin/env bash
# Installs x100-control as systemd services.
#
#   unzip x100-control.zip && cd x100-control
#   sudo deploy/install.sh               everything on this Pi: the listener, and the bridge on RS-232
#   sudo deploy/install.sh --listener    just the listener (the bridge runs on another machine)
#   sudo deploy/install.sh --bridge      just the bridge
#   sudo deploy/install.sh --alexa       add the optional Alexa link to the bridge (alexa/README.md)
#
# Options combine (--bridge --alexa). Safe to run again to upgrade: bridge.env,
# listener.env, config/, models and clips are kept.
#
#   /opt/x100-control/              the code (owned by root)
#   /opt/x100-control/.venv         Python 3.11 with the packages
#   /opt/x100-control/.python       the Python 3.11 that uv downloads, if the OS has none
#   /opt/x100-control/bridge.env    bridge settings: X100_SERIAL (or X100_HOST), LOCAL_API_*
#   /opt/x100-control/listener.env  listener settings: BRIDGE_URL, BRIDGE_TOKEN, MIC_DEVICE, ...
#   /opt/x100-control/config        inputs learnt with `x100ctl learn-inputs` (owned by x100)
#   /opt/x100-control/models        wake-word and command models (owned by x100)
set -euo pipefail

PREFIX=/opt/x100-control
SERVICE_USER=x100
SRC="$(cd "$(dirname "$0")/.." && pwd)"

die() { echo "error: $*" >&2; exit 1; }
step() { printf '\n== %s\n' "$*"; }

listener=0 bridge=0 alexa=0
for arg in "$@"; do
  case "$arg" in
    --listener) listener=1 ;;
    --bridge) bridge=1 ;;
    --alexa) alexa=1 ;;
    -h | --help) sed -n '2,19p' "$0"; exit 0 ;;
    *) die "unknown option $arg (see $0 --help)" ;;
  esac
done
if [ "$listener" -eq 0 ] && [ "$bridge" -eq 0 ]; then
  listener=1 bridge=1                        # the default: everything on this machine
fi
if [ "$alexa" -eq 1 ] && [ "$bridge" -eq 0 ]; then
  die "the Alexa link lives in the bridge: add --bridge"
fi
local_bridge=$((listener && bridge))         # the listener talks to a bridge on this machine

[ "$(id -u)" -eq 0 ] || die "run it with sudo: sudo $0 $*"
case "$(uname -m)" in
  aarch64 | x86_64) ;;
  armv7l | armv6l)
    if [ "$listener" -eq 1 ]; then
      die "this is a 32-bit OS. The wake-word engine needs onnxruntime, which has no 32-bit ARM build.
Reflash the SD card with Raspberry Pi OS Lite (64-bit) using Raspberry Pi Imager, then run this again."
    fi ;;
  *) die "unsupported machine type $(uname -m)" ;;
esac
if [ ! -f "$SRC/pyproject.toml" ] || [ ! -d "$SRC/x100_listener" ]; then
  die "run it from the unpacked x100-control folder"
fi

# KEY=VALUE in an env file: replaces the first KEY= line, commented or not, else appends.
set_env() {
  "$PREFIX/.venv/bin/python" - "$1" "$2" "$3" <<'PY'
import re, sys
path, key, value = sys.argv[1:4]
lines = open(path).read().splitlines()
pattern = re.compile(r"^\s*#?\s*" + re.escape(key) + r"=")
for i, line in enumerate(lines):
    if pattern.match(line):
        lines[i] = f"{key}={value}"
        break
else:
    lines.append(f"{key}={value}")
open(path, "w").write("\n".join(lines) + "\n")
PY
}
get_env() { sed -n "s/^$2=//p" "$1" 2>/dev/null | tail -1; }

step "System packages"
packages=(curl ca-certificates rsync)
[ "$listener" -eq 1 ] && packages+=(libportaudio2 alsa-utils)
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${packages[@]}" >/dev/null

step "Service user '$SERVICE_USER'"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$PREFIX" --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi
[ "$listener" -eq 1 ] && usermod -aG audio "$SERVICE_USER"     # the mic and speaker
[ "$bridge" -eq 1 ] && usermod -aG dialout "$SERVICE_USER"     # the USB serial adapter
id "$SERVICE_USER"

step "Code in $PREFIX"
mkdir -p "$PREFIX"
if [ "$SRC" != "$PREFIX" ]; then
  # Per-install state is excluded, so --delete never touches it.
  rsync -a --delete \
    --exclude '/.venv' --exclude '/.python' --exclude '/models' --exclude '/clips' \
    --exclude '/listener.env' --exclude '/bridge.env' --exclude '/config' --exclude '/certs' \
    --exclude '__pycache__' --exclude '*.egg-info' --exclude '/build' \
    "$SRC"/ "$PREFIX"/
fi
# The code belongs to root; per-install state keeps its own owner (fixed up below).
chown root:root "$PREFIX"
find "$PREFIX" -mindepth 1 -maxdepth 1 \
  ! -name models ! -name clips ! -name listener.env ! -name bridge.env ! -name config ! -name certs \
  -exec chown -R root:root {} +
chmod 755 "$PREFIX"

step "uv (Python installer)"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi
uv --version

step "Python 3.11 environment"
# openWakeWord pulls in tflite-runtime, whose ARM builds stop at Python 3.11. uv
# fetches a 3.11 if the OS doesn't have one. It goes under /opt, not root's
# home, because the services run with ProtectHome=true.
export UV_PYTHON_INSTALL_DIR="$PREFIX/.python"
uv venv --quiet --allow-existing --python 3.11 "$PREFIX/.venv"
extras=()
[ "$listener" -eq 1 ] && extras+=(listener)
[ "$alexa" -eq 1 ] && extras+=(aws)
target="$PREFIX"
if [ "${#extras[@]}" -gt 0 ]; then
  target="${PREFIX}[$(IFS=,; echo "${extras[*]}")]"
fi
rm -rf "$PREFIX/build" "$PREFIX"/*.egg-info   # a stale build/ can carry deleted files into an upgrade
uv pip install --quiet --python "$PREFIX/.venv/bin/python" --reinstall-package x100-control "$target"
rm -rf "$PREFIX/build" "$PREFIX"/*.egg-info
checks="import serial, x100_bridge"
[ "$listener" -eq 1 ] && checks="$checks, openwakeword, sherpa_onnx, sounddevice"
[ "$alexa" -eq 1 ] && checks="$checks, paho.mqtt"
"$PREFIX/.venv/bin/python" -c "$checks; print('packages ok')"

if [ "$listener" -eq 1 ]; then
  step "Models"
  mkdir -p "$PREFIX/models" "$PREFIX/clips"
  chown -R "$SERVICE_USER:$SERVICE_USER" "$PREFIX/models" "$PREFIX/clips"
  (cd "$PREFIX" && runuser -u "$SERVICE_USER" -- env MODELS_DIR="$PREFIX/models" \
    "$PREFIX/.venv/bin/x100-listener" --download-models)
fi

bridge_ready=0 serial_note=""
if [ "$bridge" -eq 1 ]; then
  step "Bridge settings"
  mkdir -p "$PREFIX/config"
  chown "$SERVICE_USER:$SERVICE_USER" "$PREFIX/config"
  env_file="$PREFIX/bridge.env"
  if [ ! -f "$env_file" ]; then
    install -m 640 -o root -g "$SERVICE_USER" "$PREFIX/deploy/bridge.env.example" "$env_file"
    # The projector link: a USB serial adapter if there is one (or the default, when this
    # machine does everything), otherwise left for you to fill in.
    mapfile -t by_id < <(ls /dev/serial/by-id/* 2>/dev/null || true)
    if [ "${#by_id[@]}" -eq 1 ]; then
      set_env "$env_file" X100_SERIAL "${by_id[0]}"
      serial_note="found the USB serial adapter: ${by_id[0]}"
    elif [ "$local_bridge" -eq 1 ] || [ -e /dev/ttyUSB0 ]; then
      set_env "$env_file" X100_SERIAL /dev/ttyUSB0
      if [ "${#by_id[@]}" -gt 1 ]; then
        serial_note="several serial adapters: pick one of /dev/serial/by-id/* for X100_SERIAL"
      elif [ ! -e /dev/ttyUSB0 ]; then
        serial_note="no USB serial adapter plugged in yet; X100_SERIAL=/dev/ttyUSB0 is a guess"
      fi
    fi
    if [ "$local_bridge" -eq 1 ]; then
      set_env "$env_file" LOCAL_API_BIND 127.0.0.1:8661   # only this machine's listener
    else
      set_env "$env_file" LOCAL_API_BIND 0.0.0.0:8661     # a listener elsewhere on the LAN
    fi
    set_env "$env_file" LOCAL_API_TOKEN "$("$PREFIX/.venv/bin/python" -c 'import secrets; print(secrets.token_hex(16))')"
    echo "created $env_file"
  fi
  # Readable by the services, not by everyone: it holds the API token.
  chown root:"$SERVICE_USER" "$env_file"
  chmod 640 "$env_file"
  serial="$(get_env "$env_file" X100_SERIAL)"
  host="$(get_env "$env_file" X100_HOST)"
  # Configured is enough: a serial adapter plugged in later is picked up on the next command.
  if [ -n "$serial" ] || [ -n "$host" ]; then
    bridge_ready=1
  fi
  echo "projector link: ${serial:-${host:-not set}}${serial_note:+ ($serial_note)}"
fi

if [ "$listener" -eq 1 ]; then
  step "Listener settings"
  env_file="$PREFIX/listener.env"
  if [ ! -f "$env_file" ]; then
    install -m 640 -o root -g "$SERVICE_USER" "$PREFIX/deploy/listener.env.example" "$env_file"
    echo "created $env_file"
  fi
  if [ "$local_bridge" -eq 1 ] && [ -z "$(get_env "$env_file" BRIDGE_TOKEN)" ]; then
    set_env "$env_file" BRIDGE_URL "http://127.0.0.1:8661"
    set_env "$env_file" BRIDGE_TOKEN "$(get_env "$PREFIX/bridge.env" LOCAL_API_TOKEN)"
    echo "pointed it at the bridge on this machine"
  fi
  chown root:"$SERVICE_USER" "$env_file"
  chmod 640 "$env_file"
fi

step "systemd services"
units=()
[ "$bridge" -eq 1 ] && units+=(x100-bridge)
[ "$listener" -eq 1 ] && units+=(x100-listener)
for unit in "${units[@]}"; do
  install -m 644 "$PREFIX/deploy/$unit.service" "/etc/systemd/system/$unit.service"
done
if [ "$alexa" -eq 1 ]; then
  # The Alexa link connects out to AWS IoT, so the bridge may use the internet.
  mkdir -p /etc/systemd/system/x100-bridge.service.d
  printf '%s\n' "# Written by install.sh --alexa: the Alexa link connects out to AWS IoT." \
    "[Service]" "IPAddressDeny=" "IPAddressAllow=" > /etc/systemd/system/x100-bridge.service.d/alexa.conf
fi
systemctl daemon-reload
systemctl enable "${units[@]}" >/dev/null

started=()
if [ "$bridge" -eq 1 ] && [ "$bridge_ready" -eq 1 ]; then
  systemctl restart x100-bridge
  started+=(x100-bridge)
fi
if [ "$listener" -eq 1 ] && grep -Eq '^BRIDGE_TOKEN=.+' "$PREFIX/listener.env"; then
  systemctl restart x100-listener
  started+=(x100-listener)
fi
if [ "$bridge" -eq 1 ] && [ -n "$(get_env "$PREFIX/bridge.env" AWS_IOT_ENDPOINT)" ] &&
  [ ! -f /etc/systemd/system/x100-bridge.service.d/alexa.conf ]; then
  echo "warning: AWS_IOT_ENDPOINT is set but the bridge is firewalled to your network; re-run with --alexa"
fi

echo
echo "Installed: $("$PREFIX/.venv/bin/x100ctl" --version)"
echo "  (check it any time with: $PREFIX/.venv/bin/x100ctl --version)"
if [ "${#started[@]}" -gt 0 ]; then
  watch=""
  for unit in "${started[@]}"; do watch+=" -u $unit"; done
  echo "Running: ${started[*]}. Watch them with: journalctl -f$watch"
fi
cat <<EOF

Next steps (from $PREFIX). Only one program can use the serial port or the mic at a
time, so stop a service before using its device by hand, and start it again after.
EOF
if [ "$bridge" -eq 1 ]; then
  cat <<EOF

  The projector (RS-232: Setting > Advanced Settings > RS-232 Control > On):
    sudo systemctl stop x100-bridge
    sudo -u $SERVICE_USER .venv/bin/x100ctl --env-file bridge.env probe
    sudo -u $SERVICE_USER .venv/bin/x100ctl --env-file bridge.env learn-inputs     # optional
    sudo systemctl start x100-bridge
EOF
  if [ "$bridge_ready" -eq 0 ]; then
    echo "    (first set X100_SERIAL or X100_HOST: sudo nano $PREFIX/bridge.env)"
  fi
  if [ "$local_bridge" -eq 0 ]; then
    echo "    The listener's BRIDGE_TOKEN is this bridge's LOCAL_API_TOKEN: sudo grep TOKEN $PREFIX/bridge.env"
  fi
fi
if [ "$listener" -eq 1 ]; then
  if [ "$local_bridge" -eq 0 ] && ! grep -Eq '^BRIDGE_TOKEN=.+' "$PREFIX/listener.env"; then
    cat <<EOF

  Point the listener at the bridge first: sudo nano $PREFIX/listener.env
    BRIDGE_URL=http://<the bridge's IP>:8661
    BRIDGE_TOKEN=<LOCAL_API_TOKEN from the bridge's bridge.env>
EOF
  fi
  cat <<EOF

  The mic:
    sudo systemctl stop x100-listener
    sudo -u $SERVICE_USER .venv/bin/x100-listener --env-file listener.env --list-devices
    sudo -u $SERVICE_USER .venv/bin/x100-listener --env-file listener.env --test-mic
    sudo -u $SERVICE_USER .venv/bin/x100-listener --env-file listener.env --check
    sudo systemctl start x100-listener
EOF
fi
