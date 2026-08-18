#!/usr/bin/env python3
"""Claim a session's queued resume request. Prints it, or nothing if already taken."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import afk_common as afk  # noqa: E402

if len(sys.argv) < 2:
    sys.exit(0)
claimed = afk.claim_request(sys.argv[1])
if claimed:
    print(json.dumps(claimed))
