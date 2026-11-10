"""Run job collection once from the command line (for testing / scheduled tasks).

    python run_collectors.py              # all enabled sources
    python run_collectors.py tg rss_x     # only these source keys
    python run_collectors.py --list       # show configured sources

If the RIZQ server is running it already collects automatically; use the
admin dashboard button instead. The storage lock keeps both safe anyway.
"""

import json
import logging
import sys

from backend import config
from collectors import runner


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--list" in sys.argv:
        for s in runner.load_sources():
            print(f"{s['key']:<22} {s['type']:<9} {'on ' if s.get('enabled', True) else 'off'}  {s['name']}")
        return
    print("Groq:", "on" if config.GROQ_API_KEY else "off (rules only)")
    summary = runner.run_all(only=args or None)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    from backend import bot
    if not bot.flush_alerts(timeout=600):  # instant alerts + group feeds are sent from a background thread
        print("Telegram alerts: some were still pending when we stopped (the daily message still has them)")


if __name__ == "__main__":
    main()
