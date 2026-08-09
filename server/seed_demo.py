"""Наповнити базу закладу демо-записами — щоб дашборд виглядав живим для показу.

Запуск:
    python seed_demo.py                    # готель: ~130 броней за ~65 днів
    BUSINESS=clinic python seed_demo.py    # стоматологія: візити по сітці годин
    python seed_demo.py --keep             # не чистити, лише додати

Дані реалістичні, але вигадані. Для реального клієнта просто не запускай цей
скрипт — дашборд рахує з тих записів, що створив голосовий агент.
"""

import argparse
import random
import sqlite3
import uuid
from datetime import date, datetime, timedelta

import crm
from business import BUSINESS, slot_grid

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

BOOK = BUSINESS.booking
# Перші позиції прайсу замовляють частіше за останні — інакше графік виручки
# виходить рівний, а рівний графік на демо виглядає як заглушка.
WEIGHTS = [0.40, 0.32, 0.20, 0.08, 0.05, 0.03][: len(BOOK.resources)]


def _phone() -> str:
    op = random.choice(["67", "68", "50", "66", "63", "73", "97", "95"])
    return f"+380 {op} {random.randint(100, 999)} {random.randint(10, 99)} {random.randint(10, 99)}"


def _slot_span(created: datetime) -> tuple[str, str, int] | None:
    """Випадковий вільний за графіком візит поблизу дати створення."""
    res = random.choices(BOOK.resources, weights=WEIGHTS)[0]
    for _ in range(20):
        day = created.date() + timedelta(days=random.randint(0, 14))
        grid = slot_grid(day)
        if not grid:
            continue
        begin = random.choice(grid)
        finish = begin + timedelta(minutes=res.duration_min)
        hours = BOOK.hours_for(day)
        if finish.time() > hours[1]:
            continue
        return (
            begin.strftime("%Y-%m-%dT%H:%M"),
            finish.strftime("%Y-%m-%dT%H:%M"),
            res.price,
        ), res.key
    return None


def _nights_span(created: datetime) -> tuple[tuple[str, str, int], str]:
    res = random.choices(BOOK.resources, weights=WEIGHTS)[0]
    nights = random.randint(1, 5)
    # ~40% — майбутні брони (щоб була завантаженість), решта — історія.
    if random.random() < 0.4:
        check_in = date.today() + timedelta(days=random.randint(0, 30))
    else:
        check_in = created.date() + timedelta(days=random.randint(1, 10))
    check_out = check_in + timedelta(days=nights)
    return (check_in.isoformat(), check_out.isoformat(), res.price * nights), res.key


def seed(n: int, keep: bool) -> None:
    crm.init_db()
    with sqlite3.connect(crm.DB_PATH) as conn:
        if not keep:
            conn.execute("DELETE FROM bookings")
        made = 0
        for _ in range(n):
            created = datetime.now() - timedelta(
                days=random.randint(0, 65), hours=random.randint(0, 23)
            )
            span = _slot_span(created) if BOOK.is_slots else _nights_span(created)
            if not span:
                continue
            (starts_at, ends_at, price), key = span
            conn.execute(
                """
                INSERT INTO bookings
                    (id, ref_code, resource, starts_at, ends_at, party_size,
                     customer_name, phone, total_price, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    BOOK.ref_prefix + uuid.uuid4().hex[:6].upper(),
                    key,
                    starts_at,
                    ends_at,
                    random.randint(1, 4) if BOOK.ask_party else 1,
                    f"{random.choice(FIRST)} {random.choice(LAST)}",
                    _phone(),
                    price,
                    created.strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            made += 1
        conn.commit()

    total = crm.stats()
    print(
        f"{BUSINESS.key}: засіяно {made} записів у {crm.DB_PATH.name}. "
        f"За період: {total['kpi']['total_bookings']}, "
        f"виручка {total['kpi']['total_revenue']} {BUSINESS.currency}"
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="не чистити наявні записи")
    ap.add_argument("-n", type=int, default=130, help="скільки записів згенерувати")
    args = ap.parse_args()
    seed(args.n, args.keep)
