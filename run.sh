#!/bin/sh
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ "${1:-}" = test ]; then
  cd "$project_dir"
  exec python3 -m unittest discover -s tests -v
fi
exec python3 "$project_dir/doctor.py" --data-dir "$project_dir/.data" "$@"
