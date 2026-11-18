#!/usr/bin/env bash
set -euo pipefail

output=${PR9760_OUTPUT:?}
summary="$output/stages.txt"
setup=NOT_RUN
build=NOT_RUN
lint=NOT_RUN
test=NOT_RUN
cgroup="/sys/fs/cgroup$(sed -n 's/^0:://p' /proc/self/cgroup)"

record_result() {
  local exit_code=$?
  {
    printf 'workflow=%s\ncandidate=%s\nexit=%s\n' "$PR9760_WORKFLOW_SHA" \
      3af2dc671b5edd6a27d78056f308ecea7c2d6b49 "$exit_code"
    printf 'setup=%s\nbuild=%s\nlint=%s\ntest=%s\n' "$setup" "$build" "$lint" "$test"
    for file in memory.max memory.swap.max memory.peak memory.events pids.max pids.peak pids.events; do
      if [[ -r "$cgroup/$file" ]]; then
        printf '\n%s\n%s\n' "$file" "$(<"$cgroup/$file")"
      fi
    done
    printf '\n%s\n' "$(grep -E '^(Cpus_allowed_list|NoNewPrivs):' /proc/self/status)"
  } > "$summary"
  cat "$summary"
}
trap record_result EXIT

test "$(<"$cgroup/memory.max")" = 805306368
test "$(<"$cgroup/memory.swap.max")" = 0
test "$(<"$cgroup/pids.max")" = 16
test "$(id -u)" -ne 0
python -c 'import os, sys; assert len(os.sched_getaffinity(0)) == 1 and sys.version_info[:2] == (3, 11)'
test "$(git hash-object src/tokenless/adapters/tokenless/common/tokenless-env-fix.sh)" = f5d17d9168204ea1e16d7f2c7a66d66151f8c395
test "$(git hash-object src/tokenless/tests/test_env_fix_normalization.py)" = 2cf93846a16ca0a26d1f24d6a00b31af8e05852b
test "$(git hash-object src/tokenless/Makefile)" = ecdacd1861cbc4b8bc69106791cac53deb8d0ef9

run_stage() {
  local stage=$1 limit=$2 exit_code
  shift 2
  printf '\n%s started at %s\n' "$stage" "$(date -u +%FT%TZ)"
  if timeout --kill-after=10s "$limit" "$@" > "$output/$stage.log" 2>&1; then
    printf -v "$stage" 'PASS'
    tail -n 20 "$output/$stage.log"
  else
    exit_code=$?
    printf -v "$stage" 'FAIL(exit=%s)' "$exit_code"
    tail -n 120 "$output/$stage.log" >&2
    exit "$exit_code"
  fi
}

run_stage setup 360s bash -euc '
  uname -r
  python3 --version
  node --version
  [[ "$(node --version)" == v20.* ]]
  npm --version
  just --version
  jq --version
  make --version
  cc --version
  pkg-config --version
  rustup toolchain install 1.91.0 --profile minimal --component rustfmt --component clippy
  rustc --version
  cargo --version
  cargo clippy --version
'
run_stage build 900s make -C src/tokenless -j1 build
run_stage lint 900s make -C src/tokenless -j1 lint
run_stage test 900s make -C src/tokenless -j1 test
