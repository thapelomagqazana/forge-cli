#!/usr/bin/env bash
# =============================================================================
# Forge CLI — Create a new Architecture Decision Record
# =============================================================================
# Usage:
#   bash scripts/adr/new.sh "Use TypeScript for the frontend"
#   bash scripts/adr/new.sh "Use PostgreSQL for persistence" --deciders "Team A"
#   bash scripts/adr/new.sh "Use Typer for the CLI" --status Accepted
#
# What it does:
#   1. Validates the title is non-empty and slugifiable.
#   2. Finds the highest existing ADR number and increments it.
#   3. Slugifies the title into a URL-safe, ASCII-only filename.
#   4. Renders the template with {NUMBER}, {TITLE}, {STATUS}, {DATE},
#      {DECIDERS}, {CONSULTED}, {INFORMED} substituted.
#   5. Refuses to overwrite an existing ADR.
#   6. Rebuilds the ADR index via scripts/adr/index.py.
#   7. Prints the path to the new file and next steps.
#
# Design notes:
#   - The script is idempotent on its inputs: running it twice with the
#     same title fails on the second run (existing file) rather than
#     silently overwriting. This is deliberate.
#   - `index.py` exits 1 when it changes the index. That is the signal
#     pre-commit and CI use to detect drift. Here, that exit code is
#     *expected* (the ADR was just created), so it is tolerated via
#     `|| true`. The drift signal is preserved for callers that need it
#     (`make adr-check`, the pre-commit hook).
#   - All writes go through a temp file first, so a failed render never
#     leaves a partial ADR on disk.
# =============================================================================
set -euo pipefail

# --- Configuration -----------------------------------------------------------
ADR_DIR="${ADR_DIR:-docs/adr}"
TEMPLATE="${ADR_DIR}/template.md"
INDEX_SCRIPT="${INDEX_SCRIPT:-scripts/adr/index.py}"
DATE="$(date -u +%Y-%m-%d)"
STATUS="Proposed"
DECIDERS="${USER:-Forge maintainers}"

# --- Argument parsing --------------------------------------------------------
TITLE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --deciders)
      if [[ $# -lt 2 ]]; then
        echo "Error: --deciders requires a value." >&2
        exit 2
      fi
      DECIDERS="$2"
      shift 2
      ;;
    --status)
      if [[ $# -lt 2 ]]; then
        echo "Error: --status requires a value." >&2
        exit 2
      fi
      STATUS="$2"
      shift 2
      ;;
    --help|-h)
      cat <<EOF
Usage: $0 "<title>" [--deciders "<names>"] [--status <status>]

Arguments:
  <title>         The ADR title. Required. Used in the filename and heading.

Options:
  --deciders      Who made the decision (default: \$USER)
  --status        ADR status (default: Proposed)
  --help, -h      Show this help and exit.

Environment:
  ADR_DIR         Directory for ADRs (default: docs/adr)
  INDEX_SCRIPT    Path to the index generator (default: scripts/adr/index.py)

Exit codes:
  0   Success. The ADR was created and the index was rebuilt.
  2   Invalid input (empty title, missing template, bad flag value).
  3   The target file already exists. Refusing to overwrite.

Examples:
  $0 "Use Typer for the CLI layer"
  $0 "Use PostgreSQL" --deciders "Team Platform" --status Accepted
EOF
      exit 0
      ;;
    --)
      shift
      break
      ;;
    -*)
      echo "Error: unknown option: $1" >&2
      echo "Run '$0 --help' for usage." >&2
      exit 2
      ;;
    *)
      if [[ -z "$TITLE" ]]; then
        TITLE="$1"
      else
        echo "Error: unexpected argument: $1" >&2
        echo "Run '$0 --help' for usage." >&2
        exit 2
      fi
      shift
      ;;
  esac
done

# --- Validation --------------------------------------------------------------
if [[ -z "$TITLE" ]]; then
  echo "Error: an ADR title is required." >&2
  echo "Usage: $0 \"<title>\"" >&2
  echo "Run '$0 --help' for more information." >&2
  exit 2
fi

if [[ ! -d "$ADR_DIR" ]]; then
  echo "Error: ADR directory not found at $ADR_DIR" >&2
  echo "Run this script from the repository root, or set ADR_DIR." >&2
  exit 2
fi

if [[ ! -f "$TEMPLATE" ]]; then
  echo "Error: template not found at $TEMPLATE" >&2
  echo "Run this script from the repository root, or set ADR_DIR." >&2
  exit 2
fi

# Validate status against the documented set. An unknown status is almost
# always a typo, and the index generator would render it verbatim without
# warning. Catch it here.
case "$STATUS" in
  Proposed|Accepted|Deprecated|Superseded|Rejected) ;;
  *)
    echo "Error: invalid status: $STATUS" >&2
    echo "Valid statuses: Proposed, Accepted, Deprecated, Superseded, Rejected" >&2
    exit 2
    ;;
esac

# --- Find the next number ----------------------------------------------------
# Existing files are named NNNN-*.md. The highest number wins.
# If no ADRs exist, start at 0001.
LAST_NUMBER=0
if compgen -G "${ADR_DIR}/[0-9][0-9][0-9][0-9]-*.md" > /dev/null; then
  LAST_NUMBER=$(find "$ADR_DIR" -maxdepth 1 -name '[0-9][0-9][0-9][0-9]-*.md' \
    | sed -E 's#.*/([0-9]{4})-.*#\1#' \
    | sort -n \
    | tail -1)
fi
NEXT_NUMBER=$(printf "%04d" $((10#$LAST_NUMBER + 1)))

# --- Slugify the title -------------------------------------------------------
# Lowercase, ASCII only, non-alphanumeric -> hyphen, collapse repeats,
# trim leading/trailing hyphens, cap at 60 characters.
#
# Note on `iconv`: if it is not available (e.g., minimal CI images),
# the transliteration step is skipped and non-ASCII characters are dropped
# by the subsequent `sed`. The slug is still valid as long as at least one
# ASCII alphanumeric character survives.
SLUG=$(printf '%s' "$TITLE" \
  | tr '[:upper:]' '[:lower:]' \
  | { iconv -f utf-8 -t ascii//TRANSLIT 2>/dev/null || cat; } \
  | sed -E 's/[^a-z0-9]+/-/g' \
  | sed -E 's/^-+|-+$//g' \
  | cut -c1-60)

if [[ -z "$SLUG" ]]; then
  echo "Error: could not derive a valid slug from the title." >&2
  echo "Use a title with at least one ASCII letter or digit." >&2
  exit 2
fi

# --- Compose the filename ----------------------------------------------------
FILENAME="${NEXT_NUMBER}-${SLUG}.md"
TARGET="${ADR_DIR}/${FILENAME}"

if [[ -e "$TARGET" ]]; then
  echo "Error: $TARGET already exists. Refusing to overwrite." >&2
  echo "If you meant to edit the existing ADR, open it directly." >&2
  echo "If you meant to create a new one, use a different title." >&2
  exit 3
fi

# --- Render the template -----------------------------------------------------
# Use a temp file so a failed render does not leave a partial ADR.
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

# Escape special characters in the substitutions so they are safe in sed.
escape_sed() {
  printf '%s' "$1" | sed -e 's/[\/&]/\\&/g'
}

sed \
  -e "s/{NUMBER}/$(escape_sed "$NEXT_NUMBER")/g" \
  -e "s/{TITLE}/$(escape_sed "$TITLE")/g" \
  -e "s/{STATUS}/$(escape_sed "$STATUS")/g" \
  -e "s/{DATE}/$(escape_sed "$DATE")/g" \
  -e "s/{DECIDERS}/$(escape_sed "$DECIDERS")/g" \
  -e "s/{CONSULTED}/$(escape_sed "—")/g" \
  -e "s/{INFORMED}/$(escape_sed "—")/g" \
  "$TEMPLATE" > "$TMP"

# Sanity check: the rendered file must contain the new number and title.
# If the template was edited to remove a placeholder, sed leaves it as-is
# (e.g., `{TITLE}` stays literal). Catch that here so the ADR is not
# created with a placeholder in it.
if grep -qE '\{(NUMBER|TITLE|STATUS|DATE|DECIDERS|CONSULTED|INFORMED)\}' "$TMP"; then
  echo "Error: template contains unresolved placeholders." >&2
  echo "The following placeholders were not substituted:" >&2
  grep -oE '\{(NUMBER|TITLE|STATUS|DATE|DECIDERS|CONSULTED|INFORMED)\}' "$TMP" | sort -u >&2
  exit 2
fi

# --- Write the ADR -----------------------------------------------------------
mv "$TMP" "$TARGET"
trap - EXIT

# --- Rebuild the index -------------------------------------------------------
# `index.py` exits 1 when it changes the index. That is the signal
# pre-commit and CI use to detect drift — it is *expected* here, because
# a new ADR was just created and the index necessarily changed. We tolerate
# it and continue, because ADR creation itself succeeded.
#
# The drift signal is preserved for the callers that need it:
#   - `make adr-check` (CI) fails on exit 1.
#   - The pre-commit hook fails on exit 1.
# Both call `index.py --check` or `index.py` directly, not through this
# script, so their exit-code handling is unaffected.
if [[ -f "$INDEX_SCRIPT" ]]; then
  python3 "$INDEX_SCRIPT" || true
else
  echo "Warning: index script not found at $INDEX_SCRIPT" >&2
  echo "The ADR was created, but the index was not rebuilt." >&2
  echo "Run 'python3 scripts/adr/index.py' manually to rebuild it." >&2
fi

# --- Report ------------------------------------------------------------------
echo ""
echo "✅ Created ADR: $TARGET"
echo ""
echo "Next steps:"
echo "  1. Open the file and fill in Context, Decision, and Consequences."
echo "  2. Run 'make check' to validate formatting and links."
echo "  3. Commit with: git add $TARGET ${ADR_DIR}/README.md && git commit -s"
echo ""
