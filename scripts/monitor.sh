#!/bin/bash
# Thin wrapper so monitors.json stays independent of the python path.
exec python3 "$(dirname "${BASH_SOURCE[0]}")/monitor.py"
