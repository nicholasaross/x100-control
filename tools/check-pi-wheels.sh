#!/usr/bin/env bash
# Checks, from any machine, that every package the listener needs exists as a ready-built
# wheel for the Raspberry Pi: 64-bit Raspberry Pi OS (Bookworm or Trixie), Python 3.11.
# Then shows why the 32-bit OS can't work. Needs uv, pip and internet access.
#
#   tools/check-pi-wheels.sh
set -euo pipefail
cd "$(dirname "$0")/.."
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

echo "== 64-bit Raspberry Pi OS (aarch64, glibc 2.36+, Python 3.11): wheels only"
uv pip compile pyproject.toml --extra listener --python-version 3.11 \
  --python-platform aarch64-manylinux_2_36 --only-binary :all: --quiet --no-header --no-annotate \
  --output-file "$work/pi64.txt"
cat "$work/pi64.txt"
echo "-> all $(grep -c '==' "$work/pi64.txt") packages have 64-bit ARM wheels; nothing compiles on the Pi."

echo
echo "== Python 3.11 for the Pi (the current Pi OS ships 3.13; install.sh has uv fetch 3.11)"
uv python install --quiet cpython-3.11-linux-aarch64-gnu --install-dir "$work/python"
echo "-> uv can download $(basename "$(ls -d "$work"/python/cpython-3.11.*-linux-aarch64-gnu)")."

echo
echo "== 32-bit Raspberry Pi OS (armv7l): onnxruntime, which openWakeWord needs"
if python3 -m pip download --quiet --no-deps --only-binary=:all: --dest "$work/armv7" \
    --implementation cp --python-version 3.11 --abi cp311 \
    --platform manylinux2014_armv7l --platform manylinux_2_31_armv7l --platform linux_armv7l \
    onnxruntime >"$work/armv7.log" 2>&1; then
  echo "-> unexpected: onnxruntime now has a 32-bit ARM wheel; the 64-bit requirement could be revisited."
else
  grep -m1 -i "no matching distribution" "$work/armv7.log" || tail -1 "$work/armv7.log"
  echo "-> as expected, no 32-bit ARM build: the Pi needs the 64-bit OS."
fi
