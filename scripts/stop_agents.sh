#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PID_DIR=".pids"

if [[ ! -d "$PID_DIR" ]]; then
  echo "No PID directory found. No agents to stop."
  exit 0
fi

stop_agent() {
  local name="$1"
  local pid_file="${PID_DIR}/${name}.pid"

  if [[ ! -f "$pid_file" ]]; then
    echo "${name} agent is not running: no PID file"
    return 0
  fi

  local pid
  pid="$(cat "$pid_file")"

  if [[ -z "$pid" ]]; then
    echo "${name} agent has an empty PID file; removing it"
    rm -f "$pid_file"
    return 0
  fi

  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid"

    local waited=0
    while kill -0 "$pid" 2>/dev/null && [[ "$waited" -lt 10 ]]; do
      sleep 1
      waited=$((waited + 1))
    done

    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid"
      echo "Force stopped ${name} agent with PID ${pid}"
    else
      echo "Stopped ${name} agent with PID ${pid}"
    fi
  else
    echo "${name} agent was not running; removing stale PID ${pid}"
  fi

  rm -f "$pid_file"
}

stop_agent "router"
stop_agent "rootcause"
stop_agent "reconciliation"
stop_agent "finance"
stop_agent "erp"
stop_agent "crm"
