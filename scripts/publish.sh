#!/usr/bin/env bash
# scripts/publish.sh — push a built bundle's plugin repos + marketplace index
# to a GitHub org. Idempotent: re-running skips repos that already match.
#
# Usage:
#   ./scripts/publish.sh <bundle-dir>                       # full publish
#   ./scripts/publish.sh <bundle-dir> --limit 5             # only first 5 plugins
#   ./scripts/publish.sh <bundle-dir> --dry-run             # print actions only
#   ./scripts/publish.sh <bundle-dir> --org other-org       # publish elsewhere
#   ./scripts/publish.sh <bundle-dir> --yes                 # non-interactive publish for CI
#
# Differs from bundle-assets/import.sh in two ways:
#   1. We call `gh repo create` first (GitHub doesn't auto-create on push)
#   2. Marketplace source URLs render to the GitHub org base (not a customer URL)
#
# Idempotency: re-running after a partial failure is safe — `gh repo create`
# for an existing repo is swallowed, and plugin repos whose remote main tree
# already matches the local bundle tree are skipped before any push.

set -euo pipefail

usage() {
  cat >&2 <<EOF
Usage: $0 <bundle-dir> [options]

Required:
  <bundle-dir>           Path to a built bundle (contains repos/plugins/, marketplace.json.tmpl, manifest.json).

Options:
  --org NAME             GitHub org to publish into. Default: costrict-plugins-repo.
  --limit N              Push only the first N plugins (sorted by id) — for testing.
  --parallel N           Plugin push concurrency. Default: 4 (respects GH secondary rate limits).
  --skip-marketplace     Don't render+push marketplace.git (only push plugin bare repos).
  --skip-existing        Compatibility flag; skipping is now always content-aware.
  --local-target-dir DIR Push to local bare repos under DIR instead of GitHub (for local tests).
  --yes                  Do not prompt before publishing. Intended for CI.
  --dry-run              Print intended gh/git commands without executing.
  -h, --help             Show this message.

Examples:
  # Sanity dry-run:
  $0 build/costrict-marketplace-bundle-v0.1.0 --dry-run --limit 5

  # Publish first 5 plugins for real (smoke test):
  $0 build/costrict-marketplace-bundle-v0.1.0 --limit 5

  # Full publish (770+ repos):
  $0 build/costrict-marketplace-bundle-v0.1.0
EOF
}

BUNDLE_DIR=""
ORG="costrict-plugins-repo"
LIMIT=0
PARALLEL=4
SKIP_MP=0
SKIP_EXISTING=0
DRY_RUN=0
YES=0
LOCAL_TARGET_DIR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --org) ORG="$2"; shift 2 ;;
    --limit) LIMIT="$2"; shift 2 ;;
    --parallel) PARALLEL="$2"; shift 2 ;;
    --skip-marketplace) SKIP_MP=1; shift ;;
    --skip-existing) SKIP_EXISTING=1; shift ;;
    --local-target-dir) LOCAL_TARGET_DIR="$2"; shift 2 ;;
    --yes) YES=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --) shift; break ;;
    -*) echo "ERROR: unknown flag $1" >&2; usage; exit 2 ;;
    *)
      if [[ -z "$BUNDLE_DIR" ]]; then
        BUNDLE_DIR="$1"; shift
      else
        echo "ERROR: extra positional arg '$1'" >&2; exit 2
      fi
      ;;
  esac
done

[[ -n "$BUNDLE_DIR" ]] || { usage; exit 1; }
[[ -d "$BUNDLE_DIR/repos/plugins" ]] || { echo "ERROR: $BUNDLE_DIR/repos/plugins not found" >&2; exit 1; }
[[ -f "$BUNDLE_DIR/marketplace.json.tmpl" ]] || { echo "ERROR: $BUNDLE_DIR/marketplace.json.tmpl not found" >&2; exit 1; }
[[ -f "$BUNDLE_DIR/manifest.json" ]] || { echo "ERROR: $BUNDLE_DIR/manifest.json not found" >&2; exit 1; }

for bin in git find xargs sed; do
  command -v "$bin" >/dev/null 2>&1 || { echo "ERROR: missing tool: $bin" >&2; exit 1; }
done
if [[ -z "$LOCAL_TARGET_DIR" ]]; then
  command -v gh >/dev/null 2>&1 || { echo "ERROR: missing tool: gh" >&2; exit 1; }
else
  mkdir -p "$LOCAL_TARGET_DIR"
  LOCAL_TARGET_DIR="$(cd "$LOCAL_TARGET_DIR" && pwd)"
fi

repo_url() {
  local id="$1"
  if [[ -n "${LOCAL_TARGET_DIR:-}" ]]; then
    printf "%s/%s.git" "${LOCAL_TARGET_DIR%/}" "$id"
  else
    printf "https://github.com/%s/%s.git" "$ORG" "$id"
  fi
}

echo ">> Pre-flight"
if [[ -n "$LOCAL_TARGET_DIR" ]]; then
  echo "   local target: $LOCAL_TARGET_DIR  ->  OK"
else
  gh auth status >/dev/null 2>&1 || { echo "ERROR: gh CLI not authenticated; run 'gh auth login'" >&2; exit 1; }
  GH_USER=$(gh api user --jq .login 2>/dev/null || echo "?")
  gh api "orgs/$ORG" >/dev/null 2>&1 || { echo "ERROR: cannot access org '$ORG' (check membership / token scopes)" >&2; exit 1; }
  echo "   gh user: $GH_USER  →  org: $ORG  →  OK"
fi

mapfile -t REPOS < <(find "$BUNDLE_DIR/repos/plugins" -maxdepth 1 -name '*.git' -type d | sort)
if (( LIMIT > 0 )); then
  REPOS=("${REPOS[@]:0:LIMIT}")
fi
TOTAL=${#REPOS[@]}
echo ">> Plugin bare repos: $TOTAL  (org=$ORG  parallel=$PARALLEL$( ((DRY_RUN)) && echo "  DRY-RUN"))"

if (( ! DRY_RUN && ! YES )); then
  echo
  read -r -p "Proceed with publishing $TOTAL plugin repo(s) to https://github.com/$ORG/ ? [y/N] " confirm
  [[ "$confirm" == "y" || "$confirm" == "Y" ]] || { echo "Aborted."; exit 0; }
fi

SUCC=$(mktemp -t costrict-pub-succ.XXXXXX)
FAIL=$(mktemp -t costrict-pub-fail.XXXXXX)
SKIP=$(mktemp -t costrict-pub-skip.XXXXXX)
trap 'rm -f "$SUCC" "$FAIL" "$SKIP"' EXIT
export SUCC FAIL SKIP ORG DRY_RUN SKIP_EXISTING LOCAL_TARGET_DIR
export -f repo_url

# Phase 1 — serial repo create with throttling + secondary rate-limit backoff.
# GitHub enforces an undocumented secondary limit on rapid write operations;
# tripping it returns "too many repositories, too quickly". Empirically a
# ~1.5s gap between creates stays under the wire; on a hit we back off 60s.
echo
echo ">> Phase 1: serial repo create (throttled)"

# Build the set of IDs needing a repo.
# Optimisation: fetch the current org repo list in one paginated call instead of
# 821 sequential `git ls-remote`s. Plugins whose repo exists are assumed to have
# content (idempotent push --mirror is a no-op on identical content anyway).
echo "   indexing existing repos under $ORG..."
EXISTING=$(mktemp -t costrict-pub-exist.XXXXXX)
trap 'rm -f "$SUCC" "$FAIL" "$SKIP" "$EXISTING"' EXIT
if [[ -n "$LOCAL_TARGET_DIR" ]]; then
  find "$LOCAL_TARGET_DIR" -maxdepth 1 -name '*.git' -type d -exec basename {} .git \; | sort > "$EXISTING"
else
  gh api "orgs/$ORG/repos" --paginate --jq '.[].name' > "$EXISTING" 2>/dev/null || true
fi
EXIST_COUNT=$(wc -l < "$EXISTING" | tr -d ' ')
echo "   existing repos in org: $EXIST_COUNT"

# Index for O(1) lookup via grep -F -x -q.
#
# When --skip-existing is set, "already up-to-date" means BOTH (a) the repo
# exists in the org AND (b) the repo has at least one ref. An existing-but-
# empty repo (e.g. left over from a previous run that died between Phase 1
# create and Phase 2 push) must NOT be skipped — we still need to push to it.
# We probe emptiness in parallel via `git ls-remote` to avoid 700+ serial calls.
echo "   probing existing repos for empty-but-created (parallel ls-remote)..."
EMPTY_LIST=$(mktemp -t costrict-pub-empty.XXXXXX)
trap 'rm -f "$SUCC" "$FAIL" "$SKIP" "$EXISTING" "$EMPTY_LIST"' EXIT
xargs -P 16 -I {} bash -c '
  id="$1"; url="$(repo_url "$id")"
  if [ "$(git ls-remote "$url" 2>/dev/null | wc -l | tr -d " ")" = "0" ]; then
    echo "$id"
  fi
' _ {} < "$EXISTING" > "$EMPTY_LIST"
EMPTY_COUNT=$(wc -l < "$EMPTY_LIST" | tr -d ' ')
if (( EMPTY_COUNT > 0 )); then
  echo "   detected $EMPTY_COUNT existing-but-empty repos (will push to repair)"
fi

NEED=()
for bare in "${REPOS[@]}"; do
  id="$(basename "$bare" .git)"
  if grep -F -x -q "$id" "$EXISTING" 2>/dev/null; then
    # Repo exists in org. Treat as "needs check/push" if it's empty;
    # otherwise include it for content-aware tree comparison in Phase 2.
    if grep -F -x -q "$id" "$EMPTY_LIST" 2>/dev/null; then
      NEED+=("$bare")
    else
      NEED+=("$bare")
    fi
  else
    NEED+=("$bare")
  fi
done

CREATE_COUNT=${#NEED[@]}
K_PRE=$(wc -l < "$SKIP" | tr -d ' ')
echo "   need create/check: $CREATE_COUNT   already up-to-date: $K_PRE"

if (( ! DRY_RUN )); then
  i=0
  for bare in "${NEED[@]}"; do
    i=$((i+1))
    id="$(basename "$bare" .git)"
    # skip create if repo already exists (only push needed)
    if grep -F -x -q "$id" "$EXISTING" 2>/dev/null; then
      continue
    fi
    if [[ -n "$LOCAL_TARGET_DIR" ]]; then
      git init --bare --quiet "$(repo_url "$id")"
      git -C "$(repo_url "$id")" symbolic-ref HEAD refs/heads/main
      continue
    fi
    # gh repo create with retry-on-rate-limit
    attempts=0
    while true; do
      attempts=$((attempts+1))
      if create_out=$(gh repo create "$ORG/$id" \
            --public --disable-issues --disable-wiki \
            --description "costrict-plugins mirror of $id (auto-generated, do not edit)" 2>&1); then
        break
      fi
      if echo "$create_out" | grep -qiE "already exists|Name already exists"; then
        break  # repo exists from a prior run; that's fine
      fi
      if echo "$create_out" | grep -qiE "too quickly|secondary rate limit"; then
        if (( attempts >= 8 )); then
          echo "  CREATE_FAIL $id :: rate-limited after 8 retries" >&2
          echo "$id" >> "$FAIL"
          break
        fi
        backoff=$((120 * attempts))   # 120s, 240s, 360s, ...
        echo "   [rate-limit hit at $i/$CREATE_COUNT — sleeping ${backoff}s, attempt $((attempts+1))/8]" >&2
        sleep "$backoff"
        continue
      fi
      # other error
      echo "  CREATE_FAIL $id :: $create_out" >&2
      echo "$id" >> "$FAIL"
      break
    done
    # progress
    if (( i % 25 == 0 )); then
      echo "   processed $i/$CREATE_COUNT (org repos now: $(gh api orgs/$ORG --jq .public_repos 2>/dev/null || echo ?))"
    fi
    # inter-call gap — be conservative to avoid secondary rate limit bursts
    sleep 2
  done
fi

# Phase 2 — parallel content-aware push.
echo
echo ">> Phase 2: parallel content-aware push (main, P=$PARALLEL)"

remote_main_tree() {
  local bare="$1"
  local url="$2"
  local ref="refs/tmp/costrict-publish-remote-$$-${RANDOM:-0}"

  if git -C "$bare" fetch --quiet "$url" "+refs/heads/main:$ref" >/dev/null 2>&1; then
    git -C "$bare" rev-parse "$ref^{tree}"
    git -C "$bare" update-ref -d "$ref" >/dev/null 2>&1 || true
    return 0
  fi
  git -C "$bare" update-ref -d "$ref" >/dev/null 2>&1 || true
  return 1
}

push_main_with_retry() {
  local bare="$1"
  local url="$2"
  local id="$3"
  local attempt=0
  local push_err=""

  while true; do
    attempt=$((attempt+1))
    if push_err=$(git -C "$bare" push --force "$url" refs/heads/main:refs/heads/main 2>&1); then
      return 0
    fi
    if (( attempt >= 3 )); then
      echo "  PUSH_FAIL $id ::" >&2
      printf "%s\n" "$push_err" | sed 's/^/    /' >&2
      return 1
    fi
    echo "  PUSH_RETRY $id attempt $((attempt+1))/3" >&2
    sleep $((5 * attempt))
  done
}

push_one() {
  local bare="$1"
  local id; id=$(basename "$bare" .git)
  local url; url="$(repo_url "$id")"

  if (( DRY_RUN )); then
    echo "would: compare main tree and push changed repo: git -C $bare push --force $url refs/heads/main:refs/heads/main"
    echo "$id" >> "$SUCC"
    return 0
  fi

  local local_tree remote_tree
  local_tree=$(git -C "$bare" rev-parse "refs/heads/main^{tree}")
  if remote_tree=$(remote_main_tree "$bare" "$url") && [[ "$local_tree" == "$remote_tree" ]]; then
    echo "$id" >> "$SKIP"
    return 0
  fi

  if push_main_with_retry "$bare" "$url" "$id"; then
    echo "$id" >> "$SUCC"
  else
    echo "$id" >> "$FAIL"
    return 1
  fi
}
export -f remote_main_tree push_main_with_retry push_one

# Only check/push the ones that exist in the bundle and target repo set.
if (( ${#NEED[@]} > 0 )); then
  if ! printf "%s\n" "${NEED[@]}" \
    | xargs -P "$PARALLEL" -I {} bash -c 'push_one "$@"' _ {}; then
    true
  fi
fi

S=$(wc -l < "$SUCC" | tr -d ' ')
F=$(wc -l < "$FAIL" | tr -d ' ')
K=$(wc -l < "$SKIP" | tr -d ' ')

echo
echo ">> Plugin publish summary"
echo "   Pushed:  $S / $TOTAL"
(( K > 0 )) && echo "   Skipped: $K (already up-to-date)"
echo "   Failed:  $F"
if (( F > 0 )); then
  echo
  echo "Failed plugin IDs:" >&2
  sort -u "$FAIL" >&2 | sed 's/^/  - /' >&2
  echo "Re-run publish.sh — it's idempotent; only the failed ones will retry." >&2
fi

if (( SKIP_MP )); then
  echo
  echo "(--skip-marketplace given; not pushing marketplace.git)"
  (( F > 0 )) && exit 5
  exit 0
fi

# === marketplace index ===
echo
echo ">> Render & publish marketplace.git"

if [[ -n "$LOCAL_TARGET_DIR" ]]; then
  BASE_URL="${LOCAL_TARGET_DIR%/}"
else
  BASE_URL="https://github.com/$ORG"
fi
MP_DIR=$(mktemp -d -t costrict-mp.XXXXXX)
trap 'rm -f "$SUCC" "$FAIL" "$SKIP"; rm -rf "$MP_DIR"' EXIT
mkdir -p "$MP_DIR/.claude-plugin"
sed "s|{{BASE_URL}}|$BASE_URL|g" "$BUNDLE_DIR/marketplace.json.tmpl" > "$MP_DIR/.claude-plugin/marketplace.json"

VERSION=$(sed -n 's/.*"bundle_version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$BUNDLE_DIR/manifest.json" | head -1)
[[ -n "$VERSION" ]] || VERSION=unknown

if (( DRY_RUN )); then
  echo "would: create marketplace repo and push $BASE_URL/marketplace.git refs/heads/main"
  echo "         marketplace.json sample (first plugin source.url):"
  python3 -c "import json; d=json.load(open('$MP_DIR/.claude-plugin/marketplace.json')); print('         ', d['plugins'][0]['source']['url'])" 2>/dev/null || true
else
  if [[ -n "$LOCAL_TARGET_DIR" ]]; then
    if [[ ! -d "$BASE_URL/marketplace.git" ]]; then
      git init --bare --quiet "$BASE_URL/marketplace.git"
      git -C "$BASE_URL/marketplace.git" symbolic-ref HEAD refs/heads/main
    fi
  else
    mp_create=$(gh repo create "$ORG/marketplace" \
        --public --disable-issues --disable-wiki \
        --description "costrict-plugins marketplace index (auto-generated, do not edit)" 2>&1 || true)
    if ! echo "$mp_create" | grep -qiE "already exists|Name already exists" && [[ -n "$mp_create" ]] && [[ "$mp_create" != *"https://github.com/"* ]]; then
      echo "WARNING: marketplace create returned: $mp_create" >&2
    fi
  fi
  (
    cd "$MP_DIR"
    git init -b main --quiet
    git config user.name costrict-build
    git config user.email build@costrict.local
    git add -A
    GIT_AUTHOR_DATE="$(date -u +%FT00:00:00+00:00)" \
      GIT_COMMITTER_DATE="$(date -u +%FT00:00:00+00:00)" \
      git commit --quiet -m "costrict-plugins marketplace v$VERSION"
    git push --force "$BASE_URL/marketplace.git" refs/heads/main:refs/heads/main >/dev/null
  )
  echo "   marketplace.git pushed to $BASE_URL/marketplace.git"
fi

echo
echo "=== Publish complete ==="
echo "   Plugins:     $S/$TOTAL pushed to $BASE_URL/<plugin-id>.git"
(( K > 0 )) && echo "   (skipped $K unchanged)"
(( F > 0 )) && echo "   Failed:      $F (rerun to retry)"
echo "   Marketplace: $BASE_URL/marketplace.git"
echo
echo "Customers can now:"
echo "   csc plugin marketplace add $BASE_URL/marketplace.git"
echo "   csc plugin list"

(( F > 0 )) && exit 5
exit 0
