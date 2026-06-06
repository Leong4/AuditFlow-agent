#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -f ".env" ]]; then
  echo "Error: .env not found in project root: $ROOT_DIR" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source ".env"
set +a

mkdir -p ".pids" ".logs"

start_agent() {
  local name="$1"
  local script_path="$2"
  local pid_file=".pids/${name}.pid"
  local log_file=".logs/${name}.log"

  if [[ -f "$pid_file" ]]; then
    local existing_pid
    existing_pid="$(cat "$pid_file")"
    if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
      echo "${name} agent already running with PID ${existing_pid}"
      return 0
    fi
  fi

  nohup python3 "$script_path" > "$log_file" 2>&1 < /dev/null &
  local pid="$!"
  echo "$pid" > "$pid_file"

  sleep 1
  if kill -0 "$pid" 2>/dev/null; then
    echo "Started ${name} agent with PID ${pid} (log: ${log_file})"
  else
    echo "Error: ${name} agent failed to start" >&2
    echo "Check log: ${log_file}" >&2
    rm -f "$pid_file"
    return 1
  fi
}

start_agent "crm" "agents/system/crm_agent.py"
start_agent "erp" "agents/system/erp_agent.py"
start_agent "finance" "agents/system/finance_agent.py"

# TODO: Add router agent startup here when available.
# start_agent "router" "agents/system/router_agent.py"

# TODO: Add reconciliation agent startup here when available.
# start_agent "reconciliation" "agents/system/reconciliation_agent.py"

# TODO: Add root-cause agent startup here when available.
# start_agent "rootcause" "agents/system/rootcause_agent.py"
