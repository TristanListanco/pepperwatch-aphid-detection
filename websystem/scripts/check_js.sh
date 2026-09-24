#!/usr/bin/env bash
# Syntax-check the ES6 modules.
#
# There is no build step (§3.3.2.2 rules out a bundler), so nothing would
# otherwise catch a syntax error before a browser hit it. Note the .mjs copy:
# `node --check` on a .js file parses it as a CommonJS script and silently
# accepts things that are invalid in a module, so checking the files in place
# gives a false pass.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
js_dir="$root/pepperwatch/web/static/js"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

failed=0
for file in "$js_dir"/*.js; do
  name="$(basename "$file" .js)"
  cp "$file" "$tmp/$name.mjs"
  if node --check "$tmp/$name.mjs" 2>"$tmp/err"; then
    printf '  ok    %s\n' "$(basename "$file")"
  else
    printf '  FAIL  %s\n' "$(basename "$file")"
    sed 's/^/        /' "$tmp/err"
    failed=1
  fi
done

if [ "$failed" -ne 0 ]; then
  echo "JavaScript syntax check failed" >&2
  exit 1
fi
echo "all modules parse as ES modules"
