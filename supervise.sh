#!/usr/bin/env bash
ROOT="${ROOT:-$HOME/mm-lab}"
cd "$ROOT" || exit 1
mkdir -p data
echo "[$(date '+%Y-%m-%d %H:%M:%S') America/Sao_Paulo] supervisor up" >> data/supervisor.log
while true; do
  if [[ -f data/lab.pid ]] && kill -0 "$(cat data/lab.pid)" 2>/dev/null; then
    sleep 30
    continue
  fi
  echo "[$(date '+%Y-%m-%d %H:%M:%S') America/Sao_Paulo] starting child" >> data/supervisor.log
  "$ROOT/.venv/bin/python" -u "$ROOT/lab.py" >> data/lab.out 2>&1 &
  echo $! > data/lab.pid
  wait $! || true
  echo "[$(date '+%Y-%m-%d %H:%M:%S') America/Sao_Paulo] child exited, restart in 5s" >> data/supervisor.log
  sleep 5
done
