"""Vercel entry point: every /api/* request runs the same handler as the local server.

Vercel's Python runtime looks for a class named `handler` (a BaseHTTPRequestHandler).
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from server import Handler as handler  # noqa: E402,F401
