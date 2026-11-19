#!/bin/bash
# Start the dashboard (Ctrl+C to stop; a running job pauses safely and resumes later)
cd "$(dirname "$0")/.."
source .venv/bin/activate
exec python -m verifier serve "$@"
