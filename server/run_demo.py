"""run_demo.py — підняти демо цілком: по боту на кожну галузь + вітрину.

    python run_demo.py            → http://localhost:8080

Кожен бот живе в своєму процесі на своєму порту (див. demo.py: один процес =
один заклад). Падіння одного не роняє решту — на показі це важливіше за
елегантність.

Ctrl+C гасить усе разом.
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
    """Почистити демо-бази від старих записів.

    Інакше через тиждень показів демо-клініка «забита» і бот усім відмовляє —
    найгірше, що може статись під час розмови з власником.
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
            pass  # база ще без таблиць — нічого чистити


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
        print(f"  бот {card['key']:11} → :{card['port']}")

    print(f"\n  вітрина → http://localhost:{DEMO_PORT}\n  Ctrl+C щоб зупинити все\n")

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
        # Дати ботам підняти порти, перш ніж вітрина почне їх пропонувати.
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
