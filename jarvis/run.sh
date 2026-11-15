#!/bin/sh
# Add-on start: turn /data/options.json into environment variables, then start Jarvis.
set -e

OPTIONS=/data/options.json
if [ -f "$OPTIONS" ]; then
    eval "$(python3 -m jarvis_core.addon_env "$OPTIONS")"
fi

export JARVIS_ADDON=1
export JARVIS_DATA_DIR=/data
export JARVIS_HOST=0.0.0.0
export JARVIS_PORT=8099

exec python3 -m jarvis_core
