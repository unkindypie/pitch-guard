"""Seeded historical data for the reproducible demo dashboard."""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, time, timedelta

from pitch_guard.aggregation import BUCKET_RESOLUTION_S, Bucket, History, add_histogram
from pitch_guard.config import Config


def seed_demo_history(config: Config, now: datetime | None = None) -> History:
    """Build roughly 90 days of representative statistics for demo mode.

    The data lives only in memory.  It is clearly presented as DEMO in the UI
    and is never written to the user's real history files.
    """
    moment = (now or datetime.now(UTC)).astimezone()
    rng = random.Random(42)
    today = moment.date()
    buckets: list[Bucket] = []

    for day_offset in range(90, 0, -1):
        day = today - timedelta(days=day_offset)
        progress = (90 - day_offset) / 90.0
        in_range_base = 0.56 + 0.27 * progress
        for hour in range(24):
            if rng.random() < 0.12:
                continue
            start_local = datetime.combine(day, time(hour, 0), tzinfo=moment.tzinfo)
            start = start_local.astimezone(UTC)
            monitoring = rng.uniform(420.0, 1500.0)
            voiced = min(monitoring, rng.uniform(80.0, 520.0))
            in_range_pct = min(
                0.97,
                max(0.18, in_range_base + rng.uniform(-0.12, 0.08)),
            )
            bucket = Bucket(
                start=start,
                resolution_s=3600,
                monitoring_s=monitoring,
                voiced_s=voiced,
                in_range_s=voiced * in_range_pct,
                out_of_range_s=voiced * (1.0 - in_range_pct),
                alerts=1 if rng.random() < 0.04 else 0,
                min_hz=config.min_hz,
                max_hz=config.max_hz,
            )
            _fill_histogram(bucket, rng, 196.0 + 13.0 * math.sin(hour / 4.0), voiced)
            buckets.append(bucket)

    midnight_local = datetime.combine(today, time.min, tzinfo=moment.tzinfo)
    midnight = midnight_local.astimezone(UTC)
    step = timedelta(minutes=5)
    cursor = midnight
    base = 0.83
    while cursor < moment:
        if rng.random() < 0.18:
            cursor += step
            continue
        monitoring = step.total_seconds() * rng.uniform(0.75, 1.0)
        voiced = min(monitoring, rng.uniform(12.0, 130.0))
        in_range_pct = min(0.99, max(0.4, base + rng.uniform(-0.14, 0.1)))
        bucket = Bucket(
            start=cursor.astimezone(UTC),
            resolution_s=BUCKET_RESOLUTION_S,
            monitoring_s=monitoring,
            voiced_s=voiced,
            in_range_s=voiced * in_range_pct,
            out_of_range_s=voiced * (1.0 - in_range_pct),
            alerts=1 if rng.random() < 0.015 else 0,
            min_hz=config.min_hz,
            max_hz=config.max_hz,
        )
        _fill_histogram(bucket, rng, 204.0 + 8.0 * math.sin(cursor.timestamp() / 900.0), voiced)
        buckets.append(bucket)
        cursor += step

    return History(buckets, now=now or datetime.now(UTC))


def _fill_histogram(bucket: Bucket, rng: random.Random, center: float, voiced_s: float) -> None:
    if voiced_s <= 0:
        return
    offsets = ((-18, 0.08), (-9, 0.16), (-3, 0.24), (3, 0.25), (9, 0.17), (18, 0.10))
    for offset, weight in offsets:
        hz = max(60.0, center + offset + rng.uniform(-2.0, 2.0))
        add_histogram(bucket.bins, hz, voiced_s * weight)
