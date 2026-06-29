#!/usr/bin/env bash
# scripts/mirror-to-gitea.sh — mirror a built bundle's plugin repos +
# marketplace index to the self-hosted Gitea (gitea.costrict.ai).
#
# Usage:
#   GITEA_TOKEN=... ./scripts/mirror-to-gitea.sh <bundle-dir>
#   GITEA_TOKEN=... ./scripts/mirror-to-gitea.sh <bundle-dir> --limit 30  # smoke
#
# Env:
#   GITEA_TOKEN   (required) Gitea PAT with write:repository. Empty => no-op skip.
#   GITEA_HOST    (optional) default gitea.costrict.ai
#   GITEA_OWNER   (optional) repo owner/namespace, default costrict-plugins-repo
#   GITEA_USER    (optional) accepted but unused — auth is a token header, not
#                            Basic-Auth, so no username is required.
#
# Why this exists (vs publish.sh which targets GitHub):
#   - Gitea has push-to-create DISABLED for user accounts, so every repo the
#     bundle will push must be API pre-created first. We diff the bundle's repo
#     set against Gitea's existing repos and POST /user/repos for the missing.
#   - The actual mirror push is delegated to the bundle's own import.sh, which
#     carries the HTTP speed guards + serial-retry hardening and renders the
#     marketplace index.
#
# Credentials never appear in logs/URLs: no `set -x`; the PAT is only ever sent
# as an HTTP Authorization header — to the Gitea API via `curl -H`, and to git
# via a host-scoped `http.extraHeader` written into an isolated temporary
# GIT_CONFIG_GLOBAL that import.sh's child git processes inherit. No URL, argv,
# or git push-failure stderr ever carries the token.

set -euo pipefail

usage() {
  echo "Usage: $0 <bundle-dir> [--limit N]" >&2
}

BUNDLE_DIR=""
LIMIT=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --limit)
      [[ $# -ge 2 ]] || { echo "ERROR: --limit requires a value" >&2; exit 1; }
      LIMIT="$2"; shift 2 ;;
    --limit=*) LIMIT="${1#*=}"; shift ;;
    -h|--help) usage; exit 0 ;;
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
if ! [[ "$LIMIT" =~ ^[0-9]+$ ]]; then
  echo "ERROR: --limit must be a non-negative integer (got '$LIMIT')" >&2
  exit 1
fi

# --- credential gate -------------------------------------------------------
if [[ -z "${GITEA_TOKEN:-}" ]]; then
  echo ">> GITEA_TOKEN not set — skipping Gitea mirror (no-op)."
  exit 0
fi

GITEA_HOST="${GITEA_HOST:-gitea.costrict.ai}"
GITEA_OWNER="${GITEA_OWNER:-costrict-plugins-repo}"

# Authenticate to git WITHOUT ever putting the PAT in a URL. Inject a
# host-scoped HTTP auth header via an isolated temporary git config pointed at
# by GIT_CONFIG_GLOBAL, which the git processes spawned by import.sh inherit.
# This keeps the token out of every URL, argv, log line and git push-failure
# stderr, and leaves the user's real ~/.gitconfig untouched. The temp file
# holding the token is shredded by the EXIT trap (so we do NOT `exec`).
GITCONFIG_TMP="$(mktemp -t gitea-gitconfig.XXXXXX)"
trap 'rm -f "${GITCONFIG_TMP:-}" "${EXISTING:-}"' EXIT
git config -f "$GITCONFIG_TMP" "http.https://$GITEA_HOST/.extraHeader" "AUTHORIZATION: token $GITEA_TOKEN"
export GIT_CONFIG_GLOBAL="$GITCONFIG_TMP"

# --- preconditions ---------------------------------------------------------
PLUGINS_DIR="$BUNDLE_DIR/repos/plugins"
[[ -d "$PLUGINS_DIR" ]] || { echo "ERROR: $PLUGINS_DIR not found" >&2; exit 1; }
[[ -f "$BUNDLE_DIR/repo-list.txt" ]] || { echo "ERROR: $BUNDLE_DIR/repo-list.txt not found" >&2; exit 1; }
[[ -f "$BUNDLE_DIR/import.sh" ]] || { echo "ERROR: $BUNDLE_DIR/import.sh not found" >&2; exit 1; }

for bin in curl python3 git find sort sed; do
  command -v "$bin" >/dev/null 2>&1 || { echo "ERROR: missing tool: $bin" >&2; exit 1; }
done

API="https://$GITEA_HOST/api/v1"

# --- collect the repos we intend to ensure exist ---------------------------
# Derived from the SAME `find repos/plugins/*.git | sort [| head -N]` that
# import.sh uses to choose what to push, so the pre-create set is exactly the
# push set. (`marketplace` is always pushed by import.sh's index step, so it is
# always included.) Selecting straight from the bundle's bare repos — rather
# than slicing repo-list.txt — guarantees no off-by-one / ordering drift under
# --limit. repo-list.txt presence is still required (validated above).
mapfile -t WANT < <(
  echo "marketplace"
  find "$PLUGINS_DIR" -maxdepth 1 -name '*.git' -type d | LC_ALL=C sort \
    | sed -e 's#.*/##' -e 's#\.git$##' \
    | { if (( LIMIT > 0 )); then head -n "$LIMIT"; else cat; fi; }
)
echo ">> bundle target repos to ensure on Gitea: ${#WANT[@]} (incl. marketplace)$( ((LIMIT>0)) && echo " [--limit $LIMIT]")"

# --- index existing Gitea repos (paginated) --------------------------------
fetch_existing_repos() {
  local page=1 per=50 max_pages=500 resp names
  while (( page <= max_pages )); do
    resp="$(curl -fsS --max-time 30 \
      -H "Authorization: token $GITEA_TOKEN" \
      "$API/repos/search?owner=$GITEA_OWNER&limit=$per&page=$page")" || {
        echo "ERROR: Gitea repo listing failed (page $page)" >&2
        return 1
      }
    names="$(printf '%s' "$resp" | python3 -c \
      'import sys,json; d=json.load(sys.stdin); print("\n".join(r["name"] for r in (d.get("data") or [])))')"
    [[ -z "$names" ]] && break
    printf '%s\n' "$names"
    [[ "$(printf '%s\n' "$names" | grep -c .)" -lt "$per" ]] && break
    page=$((page + 1))
  done
}

EXISTING=$(mktemp -t gitea-existing.XXXXXX)   # cleaned by the EXIT trap set above
fetch_existing_repos | LC_ALL=C sort -u > "$EXISTING"
echo ">> Gitea currently has $(grep -c . "$EXISTING" || true) repos under $GITEA_OWNER"

# --- pre-create missing repos ----------------------------------------------
created=0
present=0
failed_ids=()
for id in "${WANT[@]}"; do
  if grep -Fxq -- "$id" "$EXISTING"; then
    present=$((present + 1))
    continue
  fi
  http=""
  http="$(curl -sS --max-time 30 -o /dev/null -w '%{http_code}' \
    -X POST \
    -H "Authorization: token $GITEA_TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"$id\",\"private\":false,\"auto_init\":false}" \
    "$API/user/repos")" || http="000"
  case "$http" in
    201) created=$((created + 1)) ;;
    409) present=$((present + 1)) ;;   # already exists (race / list lag)
    *)
      echo "::error::Gitea pre-create failed for '$id' (HTTP $http)" >&2
      failed_ids+=("$id")
      ;;
  esac
done

echo ">> pre-create: created=$created, already-present=$present, failed=${#failed_ids[@]}"
if (( ${#failed_ids[@]} > 0 )); then
  echo "ERROR: pre-create failed for: ${failed_ids[*]}" >&2
  exit 1
fi

# --- delegate the actual mirror push to the bundle's import.sh -------------
# import.sh carries the HTTP guards + serial retry and renders/pushes the
# marketplace index. The base URL is CREDENTIAL-FREE — auth rides on the
# host-scoped extraHeader in the GIT_CONFIG_GLOBAL we exported above, which
# import.sh's child git processes inherit. We do NOT `exec`: running it as a
# child lets the EXIT trap shred the token-bearing temp gitconfig afterward.
# Use THIS REPO's import.sh (with --limit + HTTP guards + serial retry), not the
# snapshot baked into a possibly-older released bundle: a downloaded v0.x bundle
# can carry a stale import.sh that lacks --limit (older usage → exits "Usage:").
# import.sh resolves repos/plugins relative to its own dir, so overwrite the
# bundle copy with the repo's current one before running it.
REPO_IMPORT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/bundle-assets/import.sh"
# Hard-fail rather than silently fall back to the bundle's (possibly stale,
# pre---limit) import.sh — a missing repo copy means a broken checkout.
[[ -f "$REPO_IMPORT" ]] || { echo "ERROR: repo import.sh not found at $REPO_IMPORT" >&2; exit 1; }
cp "$REPO_IMPORT" "$BUNDLE_DIR/import.sh"
BASE_URL="https://$GITEA_HOST/$GITEA_OWNER"
echo ">> handing off to import.sh to mirror to $BASE_URL"
rc=0
if (( LIMIT > 0 )); then
  bash "$BUNDLE_DIR/import.sh" "$BASE_URL" --limit "$LIMIT" || rc=$?
else
  bash "$BUNDLE_DIR/import.sh" "$BASE_URL" || rc=$?
fi
exit "$rc"
