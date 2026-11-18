#!/usr/bin/env bash
# Repeat a HW9 cold-start run.  Usage (from examples/loom): bash hw9/reproduce.sh coldstart-4 run4
# 1. starts the portal with the access log and an owner token, 2. starts the owner stand-in,
# 3. waits while you start four fresh agent sessions with hw9/PROMPTS.md, 4. scores the run.
set -euo pipefail
NS=${1:-coldstart-4}; RUN=${2:-run4}
HERE=$(cd "$(dirname "$0")/.." && pwd)
DIR=/home/claude/hw9-coldstart/$RUN; mkdir -p "$DIR/out"
cp "$HERE/AGENT_INTERFACE.md" /home/claude/hw9-coldstart/AGENT_INTERFACE.md
export LOOM_OWNER_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(16))')
loom-portal --port 8787 --access-log "$DIR/access.jsonl" & P1=$!
sleep 1
python3 -u "$HERE/hw9/owner_bot.py" http://127.0.0.1:8787 "$NS" > "$DIR/owner_bot.jsonl" & P2=$!
trap 'kill $P1 $P2 2>/dev/null' EXIT
echo "Portal and owner stand-in running. Start the four agents from hw9/PROMPTS.md"
echo "with namespace $NS and output dir $DIR/out, then press Enter."
read -r
python3 "$HERE/hw9/check.py" "$DIR" --run2
