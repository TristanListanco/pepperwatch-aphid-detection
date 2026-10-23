#!/usr/bin/env bash
# Update this Pi to the newest commit that passed CI, then reinstall.
#
#   sudo ./deploy/update.sh
#
# CI fast-forwards the `stable` branch only after every test job has passed on
# main (see .github/workflows/ci.yml), so tracking `stable` instead of `main`
# means the Pi never runs a commit the test suite has not seen. Like
# install.sh it needs internet, so run it with Ethernet plugged in.
#
# Set PW_DEPLOY_BRANCH to deploy something else, e.g. main for a bench test.
set -euo pipefail

# Everything is inside main(), which bash parses in full before running. The
# pull below can rewrite this very file, and bash otherwise reads a script
# incrementally as it executes.
main() {
  local branch="${PW_DEPLOY_BRANCH:-stable}"
  local source_dir repo owner before after

  [ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

  source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  repo="$(git -C "$source_dir" rev-parse --show-toplevel)"
  # Run git as the checkout's owner: as root it would refuse the repository
  # ("dubious ownership") or leave root-owned files in a user's clone.
  owner="$(stat -c %U "$repo")"
  as_owner() { sudo -u "$owner" git -C "$repo" "$@"; }

  if [ -n "$(as_owner status --porcelain --untracked-files=no)" ]; then
    echo "local changes in $repo; commit or discard them first:" >&2
    as_owner status --short --untracked-files=no >&2
    exit 1
  fi

  before="$(as_owner rev-parse --short HEAD)"
  echo "==> Fetching $branch"
  as_owner fetch --quiet origin "$branch"

  if as_owner rev-parse --verify --quiet "refs/heads/$branch" >/dev/null; then
    as_owner checkout --quiet "$branch"
    as_owner merge --quiet --ff-only "origin/$branch"
  else
    as_owner checkout --quiet -b "$branch" --track "origin/$branch"
  fi
  after="$(as_owner rev-parse --short HEAD)"

  if [ "$before" = "$after" ]; then
    echo "    already at $after"
  else
    echo "    $before -> $after"
    as_owner log --oneline "$before..$after" | sed 's/^/      /'
  fi

  "$source_dir/deploy/install.sh"

  # install.sh enables the services but leaves running ones alone. Restart
  # only what is already running, so an update never starts a stopped system.
  systemctl try-restart pepperwatch-worker pepperwatch-web
  echo "==> Deployed $after"
}

main "$@"
