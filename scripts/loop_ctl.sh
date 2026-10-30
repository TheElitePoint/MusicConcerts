#!/bin/bash
# Precise loop control via PID file -- never pattern-matches other processes.
cd /Users/anirudhtyagi/TEP/MusicIndustry
PIDF=data/loop.pid
case "$1" in
  stop)
    if [ -f "$PIDF" ] && kill -0 "$(cat $PIDF)" 2>/dev/null; then
      kill "$(cat $PIDF)" && echo "stopped loop pid $(cat $PIDF)"; rm -f "$PIDF"
    else echo "loop not running"; fi ;;
  start)
    if [ -f "$PIDF" ] && kill -0 "$(cat $PIDF)" 2>/dev/null; then
      echo "already running: pid $(cat $PIDF)"
    else
      PYTHONPATH=src nohup .venv/bin/python scripts/continuous_loop.py \
        > data/logs/mp_loop.log 2>&1 &
      echo "started pid $!"
    fi ;;
  status)
    if [ -f "$PIDF" ] && kill -0 "$(cat $PIDF)" 2>/dev/null; then
      echo "running: pid $(cat $PIDF) ($(ps -o etime= -p $(cat $PIDF) | tr -d ' '))"
    else echo "not running"; fi ;;
  *) echo "usage: $0 {start|stop|status}" ;;
esac
