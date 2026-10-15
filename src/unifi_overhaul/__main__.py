"""
Entry point for `python3 -m unifi_overhaul` (with src/ on PYTHONPATH).
"""

import sys

from unifi_overhaul.client import main

sys.exit(main())
