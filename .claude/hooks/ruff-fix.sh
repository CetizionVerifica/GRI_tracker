#!/usr/bin/env bash
# PostToolUse hook: format and auto-fix a Python file under backend/ after Claude edits it.
# Remaining lint errors are reported back to Claude (exit 2) so it can fix them.
set -uo pipefail

file=$(jq -r '.tool_input.file_path // .tool_input.notebook_path // empty')
backend="$CLAUDE_PROJECT_DIR/backend"

case "$file" in
  "$backend"/*.py | "$backend"/*.pyi) ;;
  *) exit 0 ;;
esac
[ -f "$file" ] || exit 0

cd "$backend" || exit 0
uv run --frozen ruff format --force-exclude --quiet "$file"
if ! out=$(uv run --frozen ruff check --fix --force-exclude --quiet "$file" 2>&1); then
  printf 'ruff check found issues it could not auto-fix in %s:\n%s\n' "$file" "$out" >&2
  exit 2
fi
