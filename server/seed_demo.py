"""Наповнити hotel.db демо-бронями — щоб дашборд виглядав живим для показу.

Запуск:
    python seed_demo.py           # очистити й засіяти ~130 броней за ~65 днів
    python seed_demo.py --keep    # не чистити, лише додати

Дані реалістичні, але вигадані. Для реального готелю просто не запускай цей скрипт —
дашборд рахує з тих броней, що створив голосовий агент.
"""

import argparse
import random
import sqlite3
import uuid
from datetime import date, datetime, timedelta

import pms

random.seed(42)  # відтворюваність

FIRST = [
    "Андрій", "Олена", "Ігор", "Марія", "Юлія", "Олександр", "Наталія", "Дмитро",
    "Софія", "Максим", "Ірина", "Богдан", "Тетяна", "Віктор", "Оксана", "Сергій",
    "Катерина", "Павло", "Анна", "Роман", "Вікторія", "Микола", "Дарина", "Тарас",
]
LAST = [
    "Коваленко", "Мельник", "Сидоренко", "Ткаченко", "Бондаренко", "Шевченко",
    "Кравченко", "Бойко", "Ковальчук", "Поліщук", "Гончаренко", "Лисенко",
    "Марченко", "Савченко", "Руденко", "Мороз", "Кузьменко", "Пономаренко",
]

ROOM_KEYS = list(pms.ROOM_TYPES.keys())
# Ваги: стандарт/покращений частіше, люкс рідше
WEIGHTS = [0.40, 0.32, 0.20, 0.08]


def _phone() -> str:
    op = random.choice(["67", "68", "50", "66", "63", "73", "97", "95"])
    return f"+380 {op} {random.randint(100,999)} {random.randint(10,99)} {random.randint(10,99)}"


def seed(n: int, keep: bool) -> None:
    pms.init_db()
    with sqlite3.connect(pms.DB_PATH) as conn:
        if not keep:
            conn.execute("DELETE FROM bookings")
        today = date.today()
        for _ in range(n):
            key = random.choices(ROOM_KEYS, weights=WEIGHTS)[0]
            label, price, _total = pms.ROOM_TYPES[key]
            guests = random.randint(1, 4)
            nights = random.randint(1, 5)

            # ~40% — майбутні/поточні броні (щоб була завантаженість), решта — історія.
            days_back = random.randint(0, 65)
            created_dt = datetime.now() - timedelta(days=days_back, hours=random.randint(0, 23))
            if random.random() < 0.4:
                check_in = today + timedelta(days=random.randint(0, 30))
            else:
                check_in = created_dt.date() + timedelta(days=random.randint(1, 10))
            check_out = check_in + timedelta(days=nights)

            conn.execute(
                """
                INSERT INTO bookings
                    (id, ref_code, room_type, check_in, check_out, guests,
                     guest_name, phone, total_price, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    "БР-" + uuid.uuid4().hex[:6].upper(),
                    key,
                    check_in.isoformat(),
                    check_out.isoformat(),
                    guests,
                    f"{random.choice(FIRST)} {random.choice(LAST)}",
                    _phone(),
                    price * nights,
                    created_dt.strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
        conn.commit()

    total = pms.stats()
    print(f"Засіяно. Броней за період: {total['kpi']['total_bookings']}, "
          f"виручка: {total['kpi']['total_revenue']} грн")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="не чистити наявні броні")
    ap.add_argument("-n", type=int, default=130, help="скільки броней згенерувати")
    args = ap.parse_args()
    seed(args.n, args.keep)
