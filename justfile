set shell := ["bash", "-uc"]

config := env("XDG_CONFIG_HOME", env("HOME") / ".config") / "digimem"
service := config / "service.json"

# List what there is to run.
default:
    @just --list

# Run the service in the foreground. Ctrl-C stops it.
dev *args:
    @just stop
    python3 -m digimem service {{ args }}

# Stop a service running in the background, and wait for it to let go.
stop:
    #!/usr/bin/env bash
    # The lock outlives the signal by a moment, so starting again too quickly
    # is refused with "DigiMem is already running". Waiting here is what makes
    # `just dev` safe to run twice in a row.
    set -uo pipefail
    [[ -f "{{ service }}" ]] || exit 0
    pid=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["pid"])' "{{ service }}" 2>/dev/null) || exit 0
    kill -0 "$pid" 2>/dev/null || exit 0
    echo "stopping DigiMem ($pid)…"
    kill "$pid"
    for _ in {1..40}; do
        kill -0 "$pid" 2>/dev/null || exit 0
        sleep 0.5
    done
    echo "it did not stop" >&2
    exit 1

# Regenerate every icon from assets/logo.png. Needs Pillow.
icons:
    python3 assets/make-icons.py
