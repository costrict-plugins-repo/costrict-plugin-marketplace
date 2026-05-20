#!/usr/bin/env bash
# costrict-plugin-marketplace bundle import script.
#
# Usage: ./import.sh <git-base-url>
#   <git-base-url>: base URL of the customer's internal git server, e.g.
#                   https://git.internal.corp/costrict
#
# Pushes every bare repo under repos/plugins/*.git/ to
#   <git-base-url>/<plugin-id>.git
# (auto-create-on-push required, or pre-create with repo-list.txt),
# then renders marketplace.json from the bundled template and pushes it
# to <git-base-url>/marketplace.git.

set -euo pipefail

if (( BASH_VERSINFO[0] < 4 )); then
  echo "ERROR: this script requires bash 4.0+; you have $BASH_VERSION" >&2
  exit 2
fi

if [[ $# -ne 1 ]]; then
  echo "Usage: ./import.sh <git-base-url>" >&2
  echo "Example: ./import.sh https://git.internal.corp/costrict" >&2
  exit 1
fi

BASE="${1%/}"   # strip trailing slash
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

for bin in git find xargs sed; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "ERROR: required tool not on PATH: $bin" >&2
    exit 1
  fi
done

TOTAL=$(find "$PLUGINS_DIR" -maxdepth 1 -name '*.git' -type d | wc -l | tr -d ' ')
echo ">> Importing $TOTAL plugin repos to $BASE (parallel=8)"

FAIL_FILE=$(mktemp -t costrict-import-fail.XXXXXX)
SUCC_FILE=$(mktemp -t costrict-import-succ.XXXXXX)
trap 'rm -f "$FAIL_FILE" "$SUCC_FILE"' EXIT

export BASE FAIL_FILE SUCC_FILE

push_one() {
  local bare="$1"
  local id
  id="$(basename "$bare" .git)"
  if git -C "$bare" push --force --mirror "$BASE/$id.git" >/tmp/costrict-import-"$$"-"$id".log 2>&1; then
    echo "$id" >> "$SUCC_FILE"
    rm -f /tmp/costrict-import-"$$"-"$id".log
  else
    echo "$id" >> "$FAIL_FILE"
    echo "  FAIL $id ($(tail -1 /tmp/costrict-import-"$$"-"$id".log 2>/dev/null))" >&2
  fi
}
export -f push_one

find "$PLUGINS_DIR" -maxdepth 1 -name '*.git' -type d -print0 \
  | xargs -0 -n 1 -P 8 -I {} bash -c 'push_one "$@"' _ {}

SUCC=$(wc -l < "$SUCC_FILE" | tr -d ' ')
FAIL=$(wc -l < "$FAIL_FILE" | tr -d ' ')

echo
echo ">> Plugin push summary: $SUCC/$TOTAL succeeded, $FAIL failed"
if (( FAIL > 0 )); then
  echo "Failed plugin IDs:" >&2
  sort -u "$FAIL_FILE" >&2 | sed 's/^/  - /' >&2
fi

# Render & push marketplace index
echo
echo ">> Rendering & pushing marketplace index to $BASE/marketplace.git"
BUNDLE_VERSION="$(sed -n 's/.*"bundle_version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$MANIFEST" | head -1)"
if [[ -z "$BUNDLE_VERSION" ]]; then
  BUNDLE_VERSION="unknown"
fi

MP_DIR=$(mktemp -d -t costrict-marketplace.XXXXXX)
trap 'rm -f "$FAIL_FILE" "$SUCC_FILE"; rm -rf "$MP_DIR"' EXIT
mkdir -p "$MP_DIR/.claude-plugin"
sed "s|{{BASE_URL}}|$BASE|g" "$TMPL" > "$MP_DIR/.claude-plugin/marketplace.json"

(
  cd "$MP_DIR"
  git init -b main --quiet
  git config user.name costrict-import
  git config user.email import@costrict.local
  git add -A
  GIT_AUTHOR_DATE="$(date -u +%FT00:00:00+00:00)" \
    GIT_COMMITTER_DATE="$(date -u +%FT00:00:00+00:00)" \
    git commit --quiet -m "costrict-plugins marketplace v$BUNDLE_VERSION"
  git push --force --mirror "$BASE/marketplace.git" >/dev/null
)

echo
echo "=== Import complete ==="
echo "Successfully imported $SUCC/$TOTAL plugins."
if (( FAIL > 0 )); then
  echo "Failed: $FAIL plugins (see list above)."
fi
echo "Marketplace ready at $BASE/marketplace.git"
echo
echo "Next steps:"
echo "  1. Verify marketplace: csc plugin marketplace add $BASE/marketplace.git"
echo "  2. List available plugins: csc plugin list"
echo "  3. Install a plugin:       csc plugin install <plugin-name>@costrict-plugins"

if (( FAIL > 0 )); then
  exit 5
fi
