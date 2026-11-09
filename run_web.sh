#!/usr/bin/env bash
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec /home/teacher_haprog/PyCharmMiscProject/.venv/bin/python "$DIR/web_agent.py" "$@"
