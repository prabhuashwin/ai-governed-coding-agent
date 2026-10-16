#!/usr/bin/env bash
# Publish eval results (+ eval log + per-job audit logs) to the `eval-results` branch under runs/<label>/.
# Usage: actions-publish-results.sh <label>
set -euo pipefail
label="$1"
src="$(pwd)"
tmp="$(mktemp -d)"
git config --global user.name "gca-ci"
git config --global user.email "gca-ci@users.noreply.github.com"
url="https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"
if git ls-remote --exit-code --heads "$url" eval-results >/dev/null 2>&1; then
  git clone -q --branch eval-results --depth 1 "$url" "$tmp"
else
  git init -q -b eval-results "$tmp"
  git -C "$tmp" remote add origin "$url"
fi
dest="$tmp/runs/$label"
mkdir -p "$dest"
cp -r "$src"/evals/results/. "$dest"/ 2>/dev/null || true
cp "$src"/eval.log "$dest"/ 2>/dev/null || true
cp "$src"/ollama.log "$dest"/ 2>/dev/null || true
{
  echo "# $label"
  echo
  echo "- run: ${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
  echo "- commit: ${GITHUB_SHA}"
  echo
  cat "$src"/evals/results/*.md 2>/dev/null || echo "_no results_"
} > "$dest/README.md"
cd "$tmp"
git add -A
git commit -q -m "results: $label" || { echo "nothing to publish"; exit 0; }
git push -q origin eval-results
echo "published to eval-results/runs/$label"
