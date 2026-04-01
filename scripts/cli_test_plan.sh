#!/usr/bin/env bash
# Manual CLI test plan + runner for TRNS.
#
# Usage:
#   1) Fill in the URL variables below.
#   2) Optionally tweak intervals and flags.
#   3) Run: bash scripts/cli_test_plan.sh
#
# Outputs:
#   - Per-case output logs in: ./test_outputs/cli_YYYYmmdd_HHMMSS/
#   - Summary index:          ./test_outputs/cli_YYYYmmdd_HHMMSS/INDEX.txt
#
# Notes:
#   - Live stream cases are time-boxed (LIVE_DURATION_SEC).
#   - Non-live full processing only works with Whisper (auto+no-subs or method=whisper).
#   - LM calls are optional; set RUN_LM=1 if you want to exercise OpenRouter.
#   - If a URL variable is empty, that case is skipped.

set -u
set -o pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TS="$(date +%Y%m%d_%H%M%S)"
OUT_DIR="${OUT_DIR:-$ROOT_DIR/test_outputs/cli_${TS}}"
mkdir -p "$OUT_DIR"

# Ensure src/ is importable when running the module directly
export PYTHONPATH="$ROOT_DIR/src:${PYTHONPATH:-}"
export PYTHONUNBUFFERED=1

# ----------------------------
# REQUIRED URLS (fill these)
# ----------------------------
# Non-live YouTube with subtitles available (best for subtitle-only and auto+subs cases)
YOUTUBE_NONLIVE_WITH_SUBS=""

# Non-live YouTube without subtitles (or with subtitles disabled) to force Whisper
# If you can't find a no-subs video, use any non-live and set FORCE_WHISPER=1.
YOUTUBE_NONLIVE_NO_SUBS=""

# Live stream URL (ongoing or scheduled/live replay that is currently live)
YOUTUBE_LIVE_URL=""

# Optional: Twitter/X video URL (for yt-dlp handling of non-YouTube)
TWITTER_URL=""

# ----------------------------
# GLOBAL CLI SETTINGS
# ----------------------------
INTERVAL="${INTERVAL:-30}"
OVERLAP="${OVERLAP:-2}"
LANGUAGE="${LANGUAGE:-en}"
PROCESS_MODE_AUTO="auto"

# Translation output mode: russian-only | both | original-only
TRANSLATION_OUTPUT="${TRANSLATION_OUTPUT:-russian-only}"

# LM settings
LM_OUTPUT_MODE="${LM_OUTPUT_MODE:-transcriptions-only}"  # transcriptions-only | lm-only | both
LM_INTERVAL="${LM_INTERVAL:-30}"
LM_WINDOW_SECONDS="${LM_WINDOW_SECONDS:-120}"
RUN_LM="${RUN_LM:-0}"

# Debug logging to stdout (avoids logs.txt pollution)
DEBUG="${DEBUG:-1}"

# Timebox for live cases (seconds)
LIVE_DURATION_SEC="${LIVE_DURATION_SEC:-180}"

# Force Whisper even if subtitles are available
FORCE_WHISPER="${FORCE_WHISPER:-0}"

# ----------------------------
# Helper functions
# ----------------------------
INDEX_FILE="$OUT_DIR/INDEX.txt"

log_index() {
  echo "$*" | tee -a "$INDEX_FILE" >/dev/null
}

run_with_timeout() {
  local timeout="$1"; shift
  local outfile="$1"; shift
  python - <<'PY' "$timeout" "$outfile" "$@"
import os, sys, subprocess, time

_timeout = int(sys.argv[1])
_outfile = sys.argv[2]
_cmd = sys.argv[3:]

with open(_outfile, "ab") as f:
    f.write(b"\n[TEST HARNESS] Starting process...\n")
    f.write(("[TEST HARNESS] CMD: " + " ".join(_cmd) + "\n").encode())
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    p = subprocess.Popen(_cmd, stdout=f, stderr=subprocess.STDOUT, env=env)
    try:
        p.wait(timeout=_timeout)
        rc = p.returncode
    except subprocess.TimeoutExpired:
        f.write(b"\n[TEST HARNESS] Timeout reached, terminating...\n")
        p.terminate()
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            f.write(b"[TEST HARNESS] Force killing...\n")
            p.kill()
            p.wait()
        rc = p.returncode if p.returncode is not None else 124
    f.write((f"\n[TEST HARNESS] Exit code: {rc}\n").encode())
    sys.exit(rc if rc is not None else 0)
PY
}

run_case() {
  local name="$1"; shift
  local timeout="$1"; shift
  local outfile="$OUT_DIR/${name}.txt"

  {
    echo "CASE: $name"
    echo "START: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
    echo "OUTFILE: $outfile"
  } >> "$outfile"

  log_index "---"
  log_index "CASE: $name"
  log_index "OUTFILE: $outfile"

  local rc=0
  if [[ "$timeout" -gt 0 ]]; then
    run_with_timeout "$timeout" "$outfile" "$@"
    rc=$?
  else
    # Non-live runs: capture stdout+stderr
    "$@" >> "$outfile" 2>&1
    rc=$?
  fi

  echo "END: $(date -u '+%Y-%m-%d %H:%M:%S UTC')" >> "$outfile"
  log_index "RESULT: exit_code=$rc"

  return 0
}

# Base args (shared)
BASE_ARGS=(
  --interval "$INTERVAL"
  --overlap "$OVERLAP"
  --language "$LANGUAGE"
  --translation-output "$TRANSLATION_OUTPUT"
  --lm-output-mode "$LM_OUTPUT_MODE"
  --lm-interval "$LM_INTERVAL"
  --lm-window-seconds "$LM_WINDOW_SECONDS"
  --config "$OUT_DIR/config.json"
)

if [[ "$DEBUG" == "1" ]]; then
  BASE_ARGS+=(--debug)
fi

# CLI runner (module) - adjust if you prefer `trns` entrypoint
CLI=(python -m trns.cli.main)

# ----------------------------
# TEST MATRIX
# ----------------------------
# 1) Non-live video (subtitles only)
if [[ -n "$YOUTUBE_NONLIVE_WITH_SUBS" ]]; then
  run_case "nonlive_subtitles_only_chunked" 0 "${CLI[@]}" "$YOUTUBE_NONLIVE_WITH_SUBS" \
    --method subtitles --process-mode chunked "${BASE_ARGS[@]}"
fi

# 2) Non-live video (auto, chunked)
if [[ -n "$YOUTUBE_NONLIVE_WITH_SUBS" ]]; then
  run_case "nonlive_auto_chunked" 0 "${CLI[@]}" "$YOUTUBE_NONLIVE_WITH_SUBS" \
    --method auto --process-mode chunked "${BASE_ARGS[@]}"
fi

# 3) Non-live video (whisper, chunked)
if [[ -n "$YOUTUBE_NONLIVE_NO_SUBS" || "$FORCE_WHISPER" == "1" ]]; then
  url="$YOUTUBE_NONLIVE_NO_SUBS"
  if [[ -z "$url" ]]; then
    url="$YOUTUBE_NONLIVE_WITH_SUBS"
  fi
  run_case "nonlive_whisper_chunked" 0 "${CLI[@]}" "$url" \
    --method whisper --process-mode chunked "${BASE_ARGS[@]}"
fi

# 4) Non-live video (whisper, full)
if [[ -n "$YOUTUBE_NONLIVE_NO_SUBS" || "$FORCE_WHISPER" == "1" ]]; then
  url="$YOUTUBE_NONLIVE_NO_SUBS"
  if [[ -z "$url" ]]; then
    url="$YOUTUBE_NONLIVE_WITH_SUBS"
  fi
  run_case "nonlive_whisper_full" 0 "${CLI[@]}" "$url" \
    --method whisper --process-mode full "${BASE_ARGS[@]}"
fi

# 5) Non-live video (auto, full) - only meaningful if no subtitles available
if [[ -n "$YOUTUBE_NONLIVE_NO_SUBS" ]]; then
  run_case "nonlive_auto_full_fallback" 0 "${CLI[@]}" "$YOUTUBE_NONLIVE_NO_SUBS" \
    --method auto --process-mode full "${BASE_ARGS[@]}"
fi

# 6) Live stream (auto, chunked via auto)
if [[ -n "$YOUTUBE_LIVE_URL" ]]; then
  run_case "live_auto_chunked" "$LIVE_DURATION_SEC" "${CLI[@]}" "$YOUTUBE_LIVE_URL" \
    --method auto --process-mode auto "${BASE_ARGS[@]}"
fi

# 7) Live stream (whisper-only)
if [[ -n "$YOUTUBE_LIVE_URL" ]]; then
  run_case "live_whisper_chunked" "$LIVE_DURATION_SEC" "${CLI[@]}" "$YOUTUBE_LIVE_URL" \
    --method whisper --process-mode chunked "${BASE_ARGS[@]}"
fi

# 8) Twitter/X video (auto)
if [[ -n "$TWITTER_URL" ]]; then
  run_case "twitter_auto" 0 "${CLI[@]}" "$TWITTER_URL" \
    --method auto --process-mode chunked "${BASE_ARGS[@]}"
fi

# 9) Twitter/X video (whisper-only)
if [[ -n "$TWITTER_URL" ]]; then
  run_case "twitter_whisper" 0 "${CLI[@]}" "$TWITTER_URL" \
    --method whisper --process-mode chunked "${BASE_ARGS[@]}"
fi

# 10) Optional LM coverage (re-run one case with LM enabled)
if [[ "$RUN_LM" == "1" ]]; then
  if [[ -n "$YOUTUBE_NONLIVE_WITH_SUBS" ]]; then
    run_case "nonlive_auto_chunked_with_lm" 0 "${CLI[@]}" "$YOUTUBE_NONLIVE_WITH_SUBS" \
      --method auto --process-mode chunked \
      --lm-output-mode both "${BASE_ARGS[@]}"
  fi
fi

log_index "---"
log_index "DONE. Outputs in: $OUT_DIR"

