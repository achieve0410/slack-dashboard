#!/usr/bin/env bash

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export PROJECT_ROOT

if [[ -d "$PROJECT_ROOT/venv" ]]; then
  for venv_bin in "$PROJECT_ROOT"/venv/*/bin; do
    [[ -d "$venv_bin" ]] && PATH="$venv_bin:$PATH"
  done
  export PATH
fi
