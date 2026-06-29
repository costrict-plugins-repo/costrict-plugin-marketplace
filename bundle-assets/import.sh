#!/usr/bin/env bash
# costrict-plugin-marketplace bundle import script.
#
# Usage: ./import.sh <git-base-url> [--limit N]
#   <git-base-url>: base URL of the customer's internal git server, e.g.
#                   https://git.internal.corp/costrict
#                   (may embed credentials, e.g. https://user:token@host/owner —
#                    they are used for pushing but stripped from the published
#                    marketplace.json so they never leak to clients).
#   --limit N     : push only the first N plugin bare repos (sorted) — for
#                   smoke tests. Omit for a full import.
#
# Pushes every bare repo under repos/plugins/*.git/ to
#   <git-base-url>/<plugin-id>.git
# (auto-create-on-push required, or pre-create with repo-list.txt),
# then renders marketplace.json from the bundled template and pushes it
# to <git-base-url>/marketplace.git.
#
# Robustness: every push runs with HTTP speed guards (lowSpeedLimit/Time) plus a
# large postBuffer so a flaky reverse proxy stalls the transfer instead of
# hanging forever; repos that fail the P=8 parallel pass are retried once
# serially (P=1) before being declared failed (exit 5).

set -euo pipefail

usage() {
  echo "Usage: ./import.sh <git-base-url> [--limit N]" >&2
  echo "Example: ./import.sh https://git.internal.corp/costrict" >&2
  echo "Example: ./import.sh https://git.internal.corp/costrict --limit 20" >&2
}

if (( BASH_VERSINFO[0] < 4 )); then
  echo "ERROR: this script requires bash 4.0+; you have $BASH_VERSION" >&2
  exit 2
fi

BASE=""
LIMIT=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --limit)
      [[ $# -ge 2 ]] || { echo "ERROR: --limit requires a value" >&2; exit 1; }
      LIMIT="$2"; shift 2 ;;
    --limit=*) LIMIT="${1#*=}"; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "ERROR: unknown flag $1" >&2; usage; exit 1 ;;
    *)
      if [[ -z "$BASE" ]]; then
        BASE="${1%/}"; shift           # strip trailing slash
      else
        echo "ERROR: extra positional arg '$1'" >&2; usage; exit 1
      fi
      ;;
  esac
done

if [[ -z "$BASE" ]]; then
  usage; exit 1
fi
if ! [[ "$LIMIT" =~ ^[0-9]+$ ]]; then
  echo "ERROR: --limit must be a non-negative integer (got '$LIMIT')" >&2
  exit 1
fi

# $BASE may embed push credentials (https://user:token@host/owner). They are
# needed for the actual git pushes but must NEVER reach logs or the published
# marketplace.json. Derive a credential-stripped variant up front and use it for
# every human-facing string; keep $BASE strictly for `git push` argv.
BASE_PUBLIC="$(printf '%s' "$BASE" | sed -E 's#^(https?://)[^/@]*@#\1#')"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGINS_DIR="$SCRIPT_DIR/repos/plugins"
TMPL="$SCRIPT_DIR/marketplace.json.tmpl"
MANIFEST="$SCRIPT_DIR/manifest.json"

if [[ ! -d "$PLUGINS_DIR" ]]; then
  echo "ERROR: $PLUGINS_DIR not found (run from extracted bundle root)" >&2
  exit 1
fi
if [[ ! -f "$TMPL" ]]; then
  echo "ERROR: $TMPL not found" >&2
  exit 1
fi

for bin in git find xargs sed sort; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "ERROR: required tool not on PATH: $bin" >&2
    exit 1
  fi
done

# Per-push HTTP guards: bail on a transfer that drops below 1000 B/s for 60s
# (instead of hanging on a wedged reverse proxy), and give git room to buffer a
# large pack before erroring. These `-c` overrides are additive — any auth the
# caller provides via an inherited GIT_CONFIG_GLOBAL (e.g. an http.extraHeader
# carrying a token) still applies on top of these guards.
GIT_GUARDS=(-c http.lowSpeedLimit=1000 -c http.lowSpeedTime=60 -c http.postBuffer=524288000)

# Select the plugin bare repos to push (sorted; honoring --limit).
# LC_ALL=C keeps the sort byte-stable so the --limit prefix matches any caller
# (e.g. mirror-to-gitea.sh) that pre-creates repos using the same ordering.
mapfile -t REPOS < <(find "$PLUGINS_DIR" -maxdepth 1 -name '*.git' -type d | LC_ALL=C sort)
if (( LIMIT > 0 )); then
  REPOS=("${REPOS[@]:0:LIMIT}")
fi
TOTAL=${#REPOS[@]}

if (( LIMIT > 0 )); then
  echo ">> Importing $TOTAL plugin repos to $BASE_PUBLIC (parallel=8, --limit $LIMIT)"
else
  echo ">> Importing $TOTAL plugin repos to $BASE_PUBLIC (parallel=8)"
fi

FAIL_FILE=$(mktemp -t costrict-import-fail.XXXXXX)
SUCC_FILE=$(mktemp -t costrict-import-succ.XXXXXX)
trap 'rm -f "$FAIL_FILE" "$SUCC_FILE"' EXIT

export BASE FAIL_FILE SUCC_FILE
export GIT_GUARDS_STR="${GIT_GUARDS[*]}"

push_one() {
  local bare="$1"
  local id
  id="$(basename "$bare" .git)"
  # shellcheck disable=SC2086 -- GIT_GUARDS_STR is a fixed, space-safe option list
  if git $GIT_GUARDS_STR -C "$bare" push --force --mirror "$BASE/$id.git" \
       >/tmp/costrict-import-"$$"-"$id".log 2>&1; then
    echo "$id" >> "$SUCC_FILE"
    rm -f /tmp/costrict-import-"$$"-"$id".log
  else
    echo "$id" >> "$FAIL_FILE"
    echo "  FAIL $id ($(tail -1 /tmp/costrict-import-"$$"-"$id".log 2>/dev/null))" >&2
  fi
}
export -f push_one

if (( TOTAL > 0 )); then
  printf '%s\0' "${REPOS[@]}" \
    | xargs -0 -P 8 -I {} bash -c 'push_one "$@"' _ {}
fi

SUCC=$(wc -l < "$SUCC_FILE" | tr -d ' ')
FAIL=$(wc -l < "$FAIL_FILE" | tr -d ' ')

# Serial retry pass — flaky proxies (504 / stalled connection) often clear on a
# single non-parallel retry. Re-run only the failures, serially, same guards.
if (( FAIL > 0 )); then
  mapfile -t RETRY_IDS < <(LC_ALL=C sort -u "$FAIL_FILE")
  echo
  echo ">> Retrying ${#RETRY_IDS[@]} failed repo(s) serially (P=1)"
  : > "$FAIL_FILE"
  for id in "${RETRY_IDS[@]}"; do
    push_one "$PLUGINS_DIR/$id.git"
  done
  SUCC=$(wc -l < "$SUCC_FILE" | tr -d ' ')
  FAIL=$(wc -l < "$FAIL_FILE" | tr -d ' ')
fi

echo
echo ">> Plugin push summary: $SUCC/$TOTAL succeeded, $FAIL failed"
if (( FAIL > 0 )); then
  echo "Failed plugin IDs:" >&2
  LC_ALL=C sort -u "$FAIL_FILE" | sed 's/^/  - /' >&2
fi

# Render & push marketplace index
echo
echo ">> Rendering & pushing marketplace index to $BASE_PUBLIC/marketplace.git"
BUNDLE_VERSION="$(sed -n 's/.*"bundle_version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$MANIFEST" | head -1)"
if [[ -z "$BUNDLE_VERSION" ]]; then
  BUNDLE_VERSION="unknown"
fi

# The rendered marketplace.json is world-readable, so it must NEVER carry the
# push credentials — BASE_PUBLIC (computed up front) has any `user:token@`
# userinfo stripped and is what gets substituted into client-facing clone URLs.
MP_DIR=$(mktemp -d -t costrict-marketplace.XXXXXX)
trap 'rm -f "$FAIL_FILE" "$SUCC_FILE"; rm -rf "$MP_DIR"' EXIT
mkdir -p "$MP_DIR/.claude-plugin"
sed "s|{{BASE_URL}}|$BASE_PUBLIC|g" "$TMPL" > "$MP_DIR/.claude-plugin/marketplace.json"

(
  cd "$MP_DIR"
  git init -b main --quiet
  git config user.name costrict-import
  git config user.email import@costrict.local
  git add -A
  GIT_AUTHOR_DATE="$(date -u +%FT00:00:00+00:00)" \
    GIT_COMMITTER_DATE="$(date -u +%FT00:00:00+00:00)" \
    git commit --quiet -m "costrict-plugins marketplace v$BUNDLE_VERSION"
  git "${GIT_GUARDS[@]}" push --force --mirror "$BASE/marketplace.git" >/dev/null
)

echo
echo "=== Import complete ==="
echo "Successfully imported $SUCC/$TOTAL plugins."
if (( FAIL > 0 )); then
  echo "Failed: $FAIL plugins (see list above)."
fi
echo "Marketplace ready at $BASE_PUBLIC/marketplace.git"
echo
echo "Next steps:"
echo "  1. Verify marketplace: csc plugin marketplace add $BASE_PUBLIC/marketplace.git"
echo "  2. List available plugins: csc plugin list"
echo "  3. Install a plugin:       csc plugin install <plugin-name>@costrict-plugins"

if (( FAIL > 0 )); then
  exit 5
fi
