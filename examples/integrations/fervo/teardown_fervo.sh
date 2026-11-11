#!/usr/bin/env bash
# Stop what setup_fervo.sh started and delete its state.
#
#   teardown_fervo.sh [--keep-vm]
#
# Stops the runner and a --local server, deletes the fervo-hw8 Lima VM (unless
# --keep-vm), and removes $FERVO_WORK, tokens included. Copy any evidence out
# of $FERVO_WORK first.
set -euo pipefail

WORK=${FERVO_WORK:-${TMPDIR:-/tmp}/fervo-hw8}
WORK=${WORK%/}
LIMACTL=${LIMACTL:-limactl}
VM=fervo-hw8

for pidfile in "$WORK/runner.pid" "$WORK/server.pid"; do
  if [ -f "$pidfile" ]; then
    kill "$(cat "$pidfile")" 2>/dev/null || true
  fi
done

if [ "${1:-}" != --keep-vm ] && command -v "$LIMACTL" >/dev/null \
    && "$LIMACTL" list -q 2>/dev/null | grep -qx "$VM"; then
  echo "[teardown_fervo] deleting the $VM VM" >&2
  "$LIMACTL" delete -f "$VM"
fi

case "$WORK" in
  ""|/|"$HOME") echo "[teardown_fervo] refusing to remove $WORK" >&2; exit 1 ;;
esac
rm -rf "$WORK"
echo "[teardown_fervo] removed $WORK" >&2
