#!/usr/bin/env bash
# Start a fresh Fervo server, runner and agent clone for the Town HW8 check.
#
#   setup_fervo.sh [--lima | --local]
#
# --lima (default): the server (bare repo + SQLite) runs in a throwaway Lima
#   VM named fervo-hw8, defined by lima/fervo-host.yaml. Git pushes and API
#   calls cross the VM boundary through 127.0.0.1:$FERVO_PORT.
# --local: the same steps with the server as a process on this machine, for a
#   reviewer without Lima.
#
# Every run starts from an empty database: an existing server, runner and clone
# under $FERVO_WORK are stopped and removed first. The Fervo runner and the
# contributor clone always run on this machine.
#
# Inputs (environment):
#   FERVO_TGZ         Fervo build archive: package.json, package-lock.json and
#                     dist/src from the pinned commit (see README). Required for
#                     --lima, and for --local unless FERVO_CLI is given.
#   FERVO_TGZ_SHA256  expected sha256 of FERVO_TGZ; checked when set.
#   FERVO_REF         the Fervo commit the archive was built from, for the
#                     version record (default: unrecorded).
#   FERVO_CLI         host command that runs the Fervo CLI. Default: the CLI
#                     installed from FERVO_TGZ under $FERVO_WORK/fervo-app.
#   FERVO_WORK        work directory (default: $TMPDIR/fervo-hw8).
#   FERVO_PORT        server port (default 8775; --lima must match the yaml).
#   LIMACTL           limactl command (default: limactl).
#
# Outputs under $FERVO_WORK: env.sh (no secrets; source it before running
# Town), tokens/ (mode 700, never commit), clone/, versions.txt, *.log.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
MODE=lima
case "${1:-}" in
  ""|--lima) MODE=lima ;;
  --local) MODE=local ;;
  *) echo "usage: $0 [--lima | --local]" >&2; exit 2 ;;
esac

WORK=${FERVO_WORK:-${TMPDIR:-/tmp}/fervo-hw8}
WORK=${WORK%/}
PORT=${FERVO_PORT:-8775}
VM=fervo-hw8
LIMACTL=${LIMACTL:-limactl}
NODE_VERSION=v24.22.1
# sha256 of node-$NODE_VERSION-linux-arm64.tar.xz from nodejs.org; other
# architectures are checked against the release's SHASUMS256.txt.
NODE_ARM64_SHA256=dbabf7a1cbf947e6059ffad2c27b80041db97dea1e553b62afdee7f13fca336e
URL=http://127.0.0.1:$PORT
# The fixture commit is made with fixed metadata, so n_0 has the same commit
# id on every machine; versions.txt records it for comparison.
FIXED_DATE=2026-01-01T00:00:00Z

say() { printf '[setup_fervo] %s\n' "$*" >&2; }
die() { say "error: $*"; exit 1; }
json_field() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(eval("d"+sys.argv[2]))' "$1" "$2"; }

stop_pidfile() {
  if [ -f "$1" ]; then
    kill "$(cat "$1")" 2>/dev/null || true
    rm -f "$1"
  fi
}

# -- reset ---------------------------------------------------------------
if [ -d "$WORK" ]; then
  say "stopping the previous runner and server under $WORK"
  stop_pidfile "$WORK/runner.pid"
  stop_pidfile "$WORK/server.pid"
  rm -rf "$WORK/clone" "$WORK/ops" "$WORK/tokens" "$WORK/root" \
         "$WORK/fixture-src" "$WORK"/*.log "$WORK/env.sh" "$WORK/versions.txt"
fi
mkdir -p "$WORK"
(umask 077 && mkdir -p "$WORK/tokens")
chmod 700 "$WORK/tokens"

if [ -n "${FERVO_TGZ:-}" ]; then
  [ -f "$FERVO_TGZ" ] || die "FERVO_TGZ=$FERVO_TGZ does not exist"
  TGZ_SHA=$(shasum -a 256 "$FERVO_TGZ" | cut -d' ' -f1)
  if [ -n "${FERVO_TGZ_SHA256:-}" ] && [ "$TGZ_SHA" != "$FERVO_TGZ_SHA256" ]; then
    die "FERVO_TGZ sha256 is $TGZ_SHA, expected $FERVO_TGZ_SHA256"
  fi
else
  TGZ_SHA=none
fi

# -- host CLI -------------------------------------------------------------
if [ -z "${FERVO_CLI:-}" ]; then
  [ -n "${FERVO_TGZ:-}" ] || die "set FERVO_TGZ (or FERVO_CLI for --local)"
  say "installing the Fervo CLI from $FERVO_TGZ into $WORK/fervo-app"
  rm -rf "$WORK/fervo-app" && mkdir -p "$WORK/fervo-app"
  tar -xzf "$FERVO_TGZ" -C "$WORK/fervo-app"
  (cd "$WORK/fervo-app" && npm ci --omit=dev --no-audit --no-fund >"$WORK/npm-host.log" 2>&1) \
    || die "npm ci failed on the host; see $WORK/npm-host.log"
  FERVO_CLI="node $WORK/fervo-app/dist/src/cli/main.js"
fi
# shellcheck disable=SC2086
fervo() { $FERVO_CLI "$@"; }
node --version >/dev/null 2>&1 || die "node is not installed on this machine"

# -- the fixture repository -------------------------------------------------
make_fixture() {  # $1: directory to create the fixture repository in
  mkdir -p "$1"
  cp "$HERE/fixture/fervo.toml" "$HERE/fixture/score.txt" "$1/"
  (cd "$1" && git init -q -b main && git add fervo.toml score.txt &&
   GIT_AUTHOR_DATE=$FIXED_DATE GIT_COMMITTER_DATE=$FIXED_DATE \
   git -c user.name=fixture -c user.email=fixture@example.invalid \
       -c commit.gpgsign=false commit -q -m "Fervo HW8 fixture")
}

wait_for_server() {
  for _ in $(seq 1 100); do
    code=$(curl -s -o /dev/null -w '%{http_code}' "$URL/whoami" || true)
    [ "$code" = 401 ] && return 0
    sleep 0.2
  done
  die "the Fervo server did not answer on $URL"
}

if curl -s -o /dev/null "$URL/whoami" 2>/dev/null; then
  if [ "$MODE" = local ] || ! "$LIMACTL" list -q 2>/dev/null | grep -qx "$VM"; then
    die "something already listens on $URL; stop it (is another Lima VM forwarding $PORT?)"
  fi
fi

# -- server ------------------------------------------------------------------
if [ "$MODE" = local ]; then
  say "starting a local Fervo server on $URL"
  make_fixture "$WORK/fixture-src"
  (cd "$WORK" && fervo init --source "$WORK/fixture-src" --root "$WORK/root" \
     --actor operator) > "$WORK/tokens/operator.json"
  nohup $FERVO_CLI serve --root "$WORK/root" --host 127.0.0.1 --port "$PORT" \
    >"$WORK/server.log" 2>&1 &
  echo $! > "$WORK/server.pid"
  GUEST_NODE=n/a; GUEST_OS=n/a; LIMA_VERSION=n/a; IMAGE=n/a
else
  command -v "$LIMACTL" >/dev/null || die "limactl not found; install Lima or use --local"
  [ -n "${FERVO_TGZ:-}" ] || die "--lima needs FERVO_TGZ to install Fervo in the guest"
  if ! "$LIMACTL" list -q 2>/dev/null | grep -qx "$VM"; then
    say "creating the $VM VM from lima/fervo-host.yaml"
    "$LIMACTL" create --tty=false --name "$VM" "$HERE/lima/fervo-host.yaml"
  fi
  "$LIMACTL" start --tty=false "$VM" >/dev/null 2>&1 || true
  guest() { "$LIMACTL" shell "$VM" -- bash -lc "$1"; }

  say "installing Node $NODE_VERSION and Fervo in the guest"
  "$LIMACTL" copy "$FERVO_TGZ" "$VM:/tmp/fervo.tgz"
  "$LIMACTL" copy "$HERE/fixture/fervo.toml" "$VM:/tmp/fervo.toml"
  "$LIMACTL" copy "$HERE/fixture/score.txt" "$VM:/tmp/score.txt"
  guest "set -euo pipefail
    arch=\$(uname -m); case \$arch in aarch64) na=arm64;; x86_64) na=x64;; *) echo unsupported \$arch; exit 1;; esac
    node_dir=\$HOME/opt/node-$NODE_VERSION-linux-\$na
    if [ ! -x \$node_dir/bin/node ]; then
      mkdir -p \$HOME/opt && cd \$HOME/opt
      curl -fsSLO https://nodejs.org/dist/$NODE_VERSION/node-$NODE_VERSION-linux-\$na.tar.xz
      if [ \$na = arm64 ]; then expected=$NODE_ARM64_SHA256
      else expected=\$(curl -fsSL https://nodejs.org/dist/$NODE_VERSION/SHASUMS256.txt | awk -v f=node-$NODE_VERSION-linux-\$na.tar.xz '\$2==f{print \$1}'); fi
      echo \"\$expected  node-$NODE_VERSION-linux-\$na.tar.xz\" | sha256sum -c -
      tar -xJf node-$NODE_VERSION-linux-\$na.tar.xz && rm node-$NODE_VERSION-linux-\$na.tar.xz
    fi
    ln -sfn \$node_dir \$HOME/opt/node
    export PATH=\$HOME/opt/node/bin:\$PATH
    pkill -f '[f]ervo-app/dist/src/cli/main.js serve' || true  # [f]: never match this shell
    rm -rf \$HOME/fervo-app \$HOME/hw8-root \$HOME/hw8-fixture
    mkdir -p \$HOME/fervo-app && tar -xzf /tmp/fervo.tgz -C \$HOME/fervo-app
    cd \$HOME/fervo-app
    if ! npm ci --omit=dev --no-audit --no-fund >/tmp/npm-guest.log 2>&1; then
      sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq build-essential python3 >/dev/null
      npm ci --omit=dev --no-audit --no-fund >/tmp/npm-guest.log 2>&1
    fi" >&2

  say "initializing the managed repository in the guest"
  guest "set -euo pipefail
    mkdir -p \$HOME/hw8-fixture && cp /tmp/fervo.toml /tmp/score.txt \$HOME/hw8-fixture/
    cd \$HOME/hw8-fixture && git init -q -b main && git add fervo.toml score.txt
    GIT_AUTHOR_DATE=$FIXED_DATE GIT_COMMITTER_DATE=$FIXED_DATE git -c user.name=fixture -c user.email=fixture@example.invalid -c commit.gpgsign=false commit -q -m 'Fervo HW8 fixture'
    cd \$HOME && \$HOME/opt/node/bin/node \$HOME/fervo-app/dist/src/cli/main.js init --source \$HOME/hw8-fixture --root \$HOME/hw8-root --actor operator" \
    > "$WORK/tokens/operator.json"
  # setsid -f detaches the server from this shell, and the host side closes
  # its own stdio, so `limactl shell` returns instead of waiting on it.
  guest "cd \$HOME && setsid -f \$HOME/opt/node/bin/node \$HOME/fervo-app/dist/src/cli/main.js serve --root \$HOME/hw8-root --host 127.0.0.1 --port $PORT >\$HOME/hw8-serve.log 2>&1 < /dev/null" \
    </dev/null >/dev/null 2>&1
  GUEST_NODE=$(guest "\$HOME/opt/node/bin/node --version")
  GUEST_OS=$(guest ". /etc/os-release && echo \"\$PRETTY_NAME \$(uname -m)\"")
  LIMA_VERSION=$("$LIMACTL" --version)
  IMAGE=$(grep -m1 'digest:' "$HERE/lima/fervo-host.yaml" | tr -d ' "' | cut -d: -f2-)
fi
chmod 600 "$WORK/tokens/operator.json"
wait_for_server

# -- actors, runner, clone --------------------------------------------------------
# Operator commands read the server URL and authorization from a clone's Git
# config. A clone made with the human token would enroll an extra actor, so
# the operator directory is configured directly instead.
OP_TOKEN=$(json_field "$WORK/tokens/operator.json" "['token']")
GIT_URL=$URL/git/repo.git
mkdir -p "$WORK/ops"
git -C "$WORK/ops" init -q
git -C "$WORK/ops" remote add origin "$GIT_URL"
git -C "$WORK/ops" config fervo.url "$URL"
git -C "$WORK/ops" config "http.$GIT_URL.extraHeader" "Authorization: Bearer $OP_TOKEN"

say "adding actors town-seller (agent) and town-runner (runner)"
(cd "$WORK/ops" && FERVO_TOKEN=$OP_TOKEN fervo actor add town-seller --kind agent \
   --provider none --model none --harness nandatown-track --key setup-town-seller) \
  > "$WORK/tokens/town-seller.json"
(cd "$WORK/ops" && FERVO_TOKEN=$OP_TOKEN fervo actor add town-runner --kind runner \
   --key setup-town-runner) > "$WORK/tokens/town-runner.json"
chmod 600 "$WORK/tokens"/*.json

say "starting the Fervo runner on this machine"
# The fixture policy is tests + frontier_gain only, so no model is called;
# the runner still requires every model flag.
FERVO_TOKEN=$(json_field "$WORK/tokens/town-runner.json" "['token']") \
  nohup $FERVO_CLI runner --remote "$URL" --judge-template /dev/null \
    --review-template /dev/null --anthropic-model none --openai-model none \
    --review-provider anthropic --review-model none \
  >"$WORK/runner.log" 2>&1 &
echo $! > "$WORK/runner.pid"

say "cloning as town-seller into $WORK/clone"
SELLER_TOKEN=$(json_field "$WORK/tokens/town-seller.json" "['token']")
(cd "$WORK" && FERVO_TOKEN= fervo clone "$URL" "$WORK/clone" --token "$SELLER_TOKEN") >/dev/null

# The runner's first job is the baseline of main; wait for it, so the first
# contribution is measured against a scored parent.
for _ in $(seq 1 50); do
  (cd "$WORK/ops" && FERVO_TOKEN=$OP_TOKEN fervo events) | grep -q '"type":"job_done"' && break
  sleep 0.2
done

# -- records ------------------------------------------------------------------------
N0=$(json_field "$WORK/tokens/operator.json" "['main']['commit']")
if [ -n "${FERVO_TGZ:-}" ]; then
  PKG_VERSION=$(tar -xzOf "$FERVO_TGZ" package.json |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])')
else
  PKG_VERSION="unrecorded (FERVO_CLI=$FERVO_CLI)"
fi
{
  echo "mode=$MODE"
  echo "fervo_ref=${FERVO_REF:-unrecorded}"
  echo "fervo_tgz_sha256=$TGZ_SHA"
  echo "fervo_package=$PKG_VERSION"
  echo "fixture_main_commit=$N0"
  echo "host_os=$(uname -sm) $(sw_vers -productVersion 2>/dev/null || true)"
  echo "host_node=$(node --version)"
  echo "host_git=$(git --version | cut -d' ' -f3)"
  echo "guest_node=$GUEST_NODE"
  echo "guest_os=$GUEST_OS"
  echo "lima=$LIMA_VERSION"
  echo "image_digest_aarch64=$IMAGE"
} > "$WORK/versions.txt"

cat > "$WORK/env.sh" <<EOF
# Source this before running Town. It holds no tokens: the seller's token is
# in the clone's Git config, the operator's in tokens/operator.json.
export FERVO_WORK='$WORK'
export FERVO_URL='$URL'
export FERVO_CLONE='$WORK/clone'
export FERVO_CLI='$FERVO_CLI'
export FERVO_MODE='$MODE'
EOF

say "ready: $URL ($MODE); main is n_0 at $N0"
say "next: source $WORK/env.sh"
