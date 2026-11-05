#!/usr/bin/env bash
ROOT="${ROOT:-$HOME/mm-lab}"
cd "$ROOT" || exit 1
mkdir -p data
if [[ -f data/supervisor.pid ]] && kill -0 "$(cat data/supervisor.pid)" 2>/dev/null; then
  echo "supervisor already running pid=$(cat data/supervisor.pid)"
  exit 0
fi
nohup bash "$ROOT/supervise.sh" >> data/supervisor.log 2>&1 &
echo $! > data/supervisor.pid
echo "started supervisor pid=$(cat data/supervisor.pid)"
