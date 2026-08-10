"""run_demo.py — bring the whole demo up: one bot per industry, plus the showcase.

    python run_demo.py            → http://localhost:8080

Each bot lives in its own process on its own port (see demo.py: one process =
one business). One of them crashing does not take the rest down, which during a
demo matters more than elegance.

Ctrl+C stops everything.
"""

import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from demo import CARDS, DEMO_PORT, MAX_CALL_SECONDS, demo_db

HERE = Path(__file__).parent
PYTHON = sys.executable


def prune_old(days: int = 1) -> None:
    """Clear old records out of the demo databases.

    Otherwise, after a week of demos the clinic is fully booked and the bot
    turns everyone away — the worst possible thing to happen while a business
    owner is listening.
    """
    for card in CARDS:
        path = demo_db(card["key"])
        if not path.exists():
            continue
        try:
            with sqlite3.connect(path) as conn:
                for table in ("bookings", "messages"):
                    conn.execute(
                        f"DELETE FROM {table} WHERE created_at < datetime('now', ?)",
                        (f"-{days} day",),
                    )
                conn.commit()
        except sqlite3.OperationalError:
            pass  # the database has no tables yet — nothing to clean


def start_bot(card: dict) -> subprocess.Popen:
    env = {
        **os.environ,
        "BUSINESS": card["key"],
        "CRM_DB": str(demo_db(card["key"])),
        "MAX_CALL_SECONDS": str(MAX_CALL_SECONDS),
    }
    return subprocess.Popen(
        [PYTHON, "bot.py", "--host", "0.0.0.0", "--port", str(card["port"])],
        cwd=HERE,
        env=env,
    )


def main() -> int:
    prune_old()

    procs = []
    for card in CARDS:
        procs.append(start_bot(card))
        print(f"  bot {card['key']:11} → :{card['port']}")

    print(f"\n  showcase → http://localhost:{DEMO_PORT}\n  Ctrl+C to stop everything\n")

    import uvicorn

    from demo import app

    def shutdown(*_):
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()

    try:
        # Give the bots a moment to claim their ports before the showcase starts
        # pointing people at them.
        time.sleep(2)
        uvicorn.run(app, host=os.getenv("DEMO_HOST", "0.0.0.0"), port=DEMO_PORT)
    except KeyboardInterrupt:
        pass
    finally:
        shutdown()
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    sys.exit(main())
