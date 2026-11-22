"""Deterministic 90-day Ukrainian personal banking demo dataset generator."""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

from app.data.database import (
    clear_transactions,
    init_db,
    seed_default_budgets,
    set_sync_meta,
    upsert_transactions,
)
from app.data.mcc_catalog import resolve_mcc_category
from app.schemas import TransactionRecord

_KYIV_TZ = timezone(timedelta(hours=2))

_CASHBACK_CATEGORIES: frozenset[str] = frozenset(
    {
        "Продукти",
        "Кафе та ресторани",
        "Аптеки та здоров'я",
    }
)


def _spread_offsets(
    rng: random.Random,
    count: int,
    max_offset: int = 89,
    min_gap: int = 1,
) -> list[int]:
    """Generate sorted unique day offsets within [0, max_offset] with natural spacing."""
    step = max(min_gap, max_offset // max(count, 1))
    offsets: set[int] = set()
    for i in range(count):
        base = min(max_offset, i * step + rng.randint(0, max(1, step - 1)))
        while base in offsets and base < max_offset:
            base += 1
        while base in offsets and base > 0:
            base -= 1
        offsets.add(base)
    return sorted(offsets)


def generate_demo_transactions(
    reference_date: date | None = None,
) -> list[TransactionRecord]:
    """Generate ~144 deterministic Ukrainian transactions over a 90-day window."""
    rng = random.Random(42)
    ref = reference_date or date.today()
    start_date = ref - timedelta(days=89)

    raw_items: list[tuple[date, int, int, str, int, int]] = []

    # 1. Salary / FOP transfers on 1st and 15th of each month (plus current month up to ref)
    cursor_date = start_date
    while cursor_date <= ref:
        if cursor_date.day == 1:
            raw_items.append(
                (cursor_date, 9, 30, "Зарплата / ФОП переказ", 4829, 3800000)
            )
        elif cursor_date.day == 15:
            raw_items.append(
                (cursor_date, 9, 45, "Зарплата / ФОП переказ", 4829, 3800000)
            )
        cursor_date += timedelta(days=1)

    # 2. Exact recurring monthly subscriptions (3 occurrences, 30 days apart)
    subscriptions: list[tuple[str, int, int, tuple[int, int, int], int, int]] = [
        ("Netflix.com", 5815, -44900, (3, 33, 63), 8, 10),
        ("Spotify", 5815, -19900, (5, 35, 65), 8, 20),
        ("YouTube Premium", 5815, -14900, (8, 38, 68), 8, 30),
        ("Apple.com/bill iCloud+", 5734, -11900, (11, 41, 71), 8, 40),
        ("Kyivstar Home+Mobile", 4814, -35000, (14, 44, 74), 9, 0),
    ]
    for desc, mcc, amount_minor, offsets, hour, minute in subscriptions:
        for offset in offsets:
            tx_d = ref - timedelta(days=offset)
            raw_items.append((tx_d, hour, minute, desc, mcc, amount_minor))

    # 3. Two clear high-value spending anomalies within last 35 days on distinct dates
    raw_items.append(
        (
            ref - timedelta(days=12),
            15,
            20,
            'Rozetka.ua (Монітор 27")',
            5732,
            -1649000,
        )
    )
    raw_items.append(
        (
            ref - timedelta(days=22),
            20,
            45,
            "Ресторан Catch (День народження)",
            5812,
            -435000,
        )
    )

    # 4. Daily groceries (42 transactions)
    grocery_merchants = [
        ("Сільпо", 5411, 55000, 138000, 14),
        ("АТБ-Маркет", 5411, 29000, 82000, 12),
        ("Фора", 5411, 24000, 68000, 8),
        ("Novus", 5411, 62000, 142000, 8),
    ]
    for desc, mcc, min_minor, max_minor, count in grocery_merchants:
        for offset in _spread_offsets(rng, count):
            tx_d = ref - timedelta(days=offset)
            amt = -rng.randrange(min_minor, max_minor + 1, 100)
            raw_items.append(
                (tx_d, rng.randint(11, 20), rng.randint(0, 55), desc, mcc, amt)
            )

    # 5. Cafes & food delivery (28 transactions)
    cafe_merchants = [
        ("Glovo", 5814, 36000, 74000, 7),
        ("Aroma Kava", 5814, 9500, 19500, 9),
        ("McDonald's", 5814, 24000, 47000, 6),
        ("Пузата Хата", 5812, 22000, 41000, 6),
    ]
    for desc, mcc, min_minor, max_minor, count in cafe_merchants:
        for offset in _spread_offsets(rng, count):
            tx_d = ref - timedelta(days=offset)
            amt = -rng.randrange(min_minor, max_minor + 1, 100)
            raw_items.append(
                (tx_d, rng.randint(10, 21), rng.randint(0, 55), desc, mcc, amt)
            )

    # 6. Transport & fuel (21 transactions; WOG spaced every 15 days and <= 1440 UAH)
    for desc, mcc, min_minor, max_minor, count in [
        ("Uklon", 4121, 16000, 34000, 8),
        ("Bolt", 4121, 15000, 31000, 7),
    ]:
        for offset in _spread_offsets(rng, count):
            tx_d = ref - timedelta(days=offset)
            amt = -rng.randrange(min_minor, max_minor + 1, 100)
            raw_items.append(
                (tx_d, rng.randint(8, 22), rng.randint(0, 55), desc, mcc, amt)
            )

    for offset in (4, 19, 34, 49, 64, 79):
        tx_d = ref - timedelta(days=offset)
        amt = -rng.randrange(115000, 144001, 500)
        raw_items.append(
            (tx_d, rng.randint(10, 19), rng.randint(0, 55), "WOG", 5541, amt)
        )

    # 7. Postal delivery (7 transactions, ~12 day spacing)
    for offset in (2, 13, 25, 38, 51, 66, 80):
        tx_d = ref - timedelta(days=offset)
        amt = -rng.randrange(8000, 22001, 500)
        raw_items.append(
            (tx_d, rng.randint(12, 18), rng.randint(0, 55), "Нова Пошта", 4215, amt)
        )

    # 8. Pharmacy (6 transactions, ~14 day spacing)
    for offset in (6, 20, 36, 50, 67, 83):
        tx_d = ref - timedelta(days=offset)
        amt = -rng.randrange(26000, 78001, 500)
        raw_items.append(
            (
                tx_d,
                rng.randint(11, 19),
                rng.randint(0, 55),
                "Аптека Доброго Дня",
                5912,
                amt,
            )
        )

    # 9. Clothing (3 transactions, >38 day spacing so not flagged as subscription)
    for offset, amt in ((7, -129000), (47, -145000), (86, -138000)):
        raw_items.append(
            (
                ref - timedelta(days=offset),
                16,
                25,
                "Zara / Reserved",
                5651,
                amt,
            )
        )

    # 10. Sport & fitness (2 transactions, 45 day spacing, < 1500 UAH)
    for offset, amt in ((16, -139000), (61, -145000)):
        raw_items.append(
            (
                ref - timedelta(days=offset),
                18,
                15,
                "Sport Life",
                7997,
                amt,
            )
        )

    # 11. Small tech purchases (baseline for Техніка та електроніка)
    for offset, desc, amt in (
        (28, "Comfy Аксесуари", -65000),
        (55, "Алло Кабель USB-C", -42000),
        (76, "Rozetka.ua Навушники", -89000),
    ):
        raw_items.append((ref - timedelta(days=offset), 14, 40, desc, 5732, amt))

    # 12. Books & hobbies (5 transactions, ~18 day spacing)
    for offset in (9, 27, 45, 62, 81):
        amt = -rng.randrange(34000, 76001, 500)
        raw_items.append(
            (ref - timedelta(days=offset), 17, 10, "Книгарня Є", 5942, amt)
        )

    # 13. Transfers (4 transactions, irregular spacing)
    for offset, amt in ((6, -80000), (21, -110000), (37, -65000), (75, -95000)):
        raw_items.append(
            (ref - timedelta(days=offset), 19, 5, "Переказ на картку", 4829, amt)
        )

    # Sort chronologically and compute running balance and deterministic IDs
    sortable: list[tuple[int, str, int, date, str, int, int]] = []
    for tx_d, hour, minute, desc, mcc, amt_minor in raw_items:
        dt = datetime(
            tx_d.year, tx_d.month, tx_d.day, hour, minute, tzinfo=_KYIV_TZ
        )
        sortable.append(
            (int(dt.timestamp()), desc, amt_minor, tx_d, desc, mcc, amt_minor)
        )

    sortable.sort(key=lambda item: (item[0], item[1], item[2]))

    records: list[TransactionRecord] = []
    running_balance = 45000.00

    for idx, (ts, _, _, tx_d, desc, mcc, amt_minor) in enumerate(sortable, start=1):
        amount_uah = round(amt_minor / 100.0, 2)
        running_balance = round(running_balance + amount_uah, 2)
        category = resolve_mcc_category(mcc, description=desc, amount_minor=amt_minor)
        is_expense = amt_minor < 0
        cashback_uah = (
            round(abs(amount_uah) * rng.choice((0.01, 0.015, 0.02)), 2)
            if is_expense and category in _CASHBACK_CATEGORIES
            else 0.0
        )

        records.append(
            TransactionRecord(
                id=f"demo_{idx:04d}",
                account_id="0",
                source="demo",
                timestamp=ts,
                tx_date=tx_d.isoformat(),
                tx_month=tx_d.strftime("%Y-%m"),
                description=desc,
                mcc=mcc,
                category=category,
                amount_minor=amt_minor,
                amount_uah=amount_uah,
                operation_amount_minor=amt_minor,
                currency_code=980,
                cashback_uah=cashback_uah,
                balance_uah=running_balance,
                is_expense=is_expense,
                comment="",
            )
        )

    return records


def load_demo_dataset(db_path: str | None = None, reset: bool = True) -> int:
    """Initialize SQLite schema, seed default budgets and demo transactions, set active_mode=demo."""
    init_db(db_path=db_path)
    if reset:
        clear_transactions(db_path=db_path)
    seed_default_budgets(db_path=db_path)
    records = generate_demo_transactions()
    inserted = upsert_transactions(records, db_path=db_path)
    set_sync_meta("active_mode", "demo", db_path=db_path)
    return inserted
