"""Continuous out-of-range alert timing tests."""

from __future__ import annotations

import pytest

from pitch_guard.alert import AlertTracker
from pitch_guard.models import RangeStatus
from tests.conftest import make_sample


def feed_range(
    tracker: AlertTracker,
    start: float,
    end: float,
    hz: float | None,
    *,
    step: float = 0.1,
    duration: float | None = None,
):
    decisions = []
    moment = start
    while moment <= end + 1e-9:
        decisions.append(
            tracker.feed(make_sample(moment, hz, duration_s=step if duration is None else duration))
        )
        moment = round(moment + step, 10)
    return decisions


def test_no_alert_for_short_episode() -> None:
    tracker = AlertTracker(3.0, 10.0)
    decisions = feed_range(tracker, 0.0, 2.8, 140.0)
    assert not any(decision.should_buzz for decision in decisions)
    tracker.feed(make_sample(3.0, 200.0))
    decisions = feed_range(tracker, 3.1, 5.8, 140.0)
    assert not any(decision.should_buzz for decision in decisions)


def test_one_alert_after_continuous_threshold() -> None:
    tracker = AlertTracker(3.0, 10.0)
    decisions = feed_range(tracker, 0.0, 6.0, 140.0)
    buzzes = [decision for decision in decisions if decision.should_buzz]
    assert len(buzzes) == 1
    assert buzzes[0].out_of_range_s >= 3.0
    assert buzzes[0].out_of_range_s < 3.2


def test_in_range_frame_resets_timer() -> None:
    tracker = AlertTracker(3.0, 10.0, max_gap_s=1.0)
    feed_range(tracker, 0.0, 2.0, 140.0)
    tracker.feed(make_sample(2.1, 200.0))
    decisions = feed_range(tracker, 2.2, 4.9, 140.0)
    assert not any(decision.should_buzz for decision in decisions)
    decisions = feed_range(tracker, 5.0, 5.4, 140.0)
    assert any(decision.should_buzz for decision in decisions)


def test_silence_and_unvoiced_interrupt_timer() -> None:
    for status in (RangeStatus.SILENT, RangeStatus.UNVOICED):
        tracker = AlertTracker(2.0, 10.0, max_gap_s=1.0)
        feed_range(tracker, 0.0, 1.5, 140.0)
        tracker.feed(make_sample(1.6, None, status=status))
        decisions = feed_range(tracker, 1.7, 3.5, 140.0)
        assert not any(decision.should_buzz for decision in decisions), status
        decisions = feed_range(tracker, 3.6, 3.8, 140.0)
        assert any(decision.should_buzz for decision in decisions), status


def test_silence_alone_does_not_rearm_alerted_tracker() -> None:
    tracker = AlertTracker(3.0, 0.0)
    feed_range(tracker, 0.0, 3.2, 140.0)
    assert tracker.armed is False
    tracker.feed(make_sample(3.3, None))
    assert tracker.armed is False
    decisions = feed_range(tracker, 3.4, 6.4, 140.0)
    assert not any(decision.should_buzz for decision in decisions)
    tracker.feed(make_sample(6.5, 200.0))
    assert tracker.armed is True
    decisions = feed_range(tracker, 6.6, 9.7, 140.0)
    assert sum(decision.should_buzz for decision in decisions) == 1


def test_processing_gap_interrupts_episode() -> None:
    tracker = AlertTracker(3.0, 10.0, max_gap_s=0.35)
    feed_range(tracker, 0.0, 1.5, 140.0, step=0.1)
    decision = tracker.feed(make_sample(2.0, 140.0, duration_s=0.1))
    assert not decision.should_buzz
    assert decision.out_of_range_s == pytest.approx(0.1)
    decisions = feed_range(tracker, 2.1, 3.9, 140.0)
    assert not any(item.should_buzz for item in decisions)


def test_above_and_below_trigger_same_behavior() -> None:
    for hz in (100.0, 400.0):
        tracker = AlertTracker(2.0, 10.0)
        decisions = feed_range(tracker, 0.0, 2.2, hz)
        assert sum(decision.should_buzz for decision in decisions) == 1
        assert decisions[-1].status.is_out_of_range


def test_below_to_above_is_still_one_episode() -> None:
    tracker = AlertTracker(2.0, 10.0)
    decisions = feed_range(tracker, 0.0, 1.0, 100.0)
    decisions += feed_range(tracker, 1.1, 2.3, 400.0)
    assert sum(decision.should_buzz for decision in decisions) == 1


def test_cooldown_and_rearm_require_in_range() -> None:
    tracker = AlertTracker(2.0, 5.0)
    decisions = feed_range(tracker, 0.0, 2.2, 100.0)
    assert sum(decision.should_buzz for decision in decisions) == 1
    # Back in range while the cooldown is still active: no re-arm yet.
    tracker.feed(make_sample(2.3, 200.0))
    assert tracker.armed is False
    # Out again before the cooldown expires: threshold is reached, but no buzz.
    decisions = feed_range(tracker, 2.4, 4.5, 400.0)
    assert not any(decision.should_buzz for decision in decisions)
    # Return in range after the cooldown, then a new complete episode buzzes.
    tracker.feed(make_sample(7.2, 200.0))
    assert tracker.armed is True
    decisions = feed_range(tracker, 7.3, 9.5, 100.0)
    assert sum(decision.should_buzz for decision in decisions) == 1


def test_pause_and_resume_reset_episode() -> None:
    tracker = AlertTracker(3.0, 10.0)
    feed_range(tracker, 0.0, 2.0, 140.0)
    tracker.pause()
    tracker.resume()
    decisions = feed_range(tracker, 2.1, 4.9, 140.0)
    assert not any(decision.should_buzz for decision in decisions)
    decisions = feed_range(tracker, 5.0, 5.2, 140.0)
    assert any(decision.should_buzz for decision in decisions)


def test_progress_tracks_threshold() -> None:
    tracker = AlertTracker(4.0, 10.0, max_gap_s=5.0)
    first = tracker.feed(make_sample(0.0, 140.0, duration_s=0.0))
    mid = tracker.feed(make_sample(2.0, 140.0, duration_s=0.0))
    assert first.progress == 0.0
    assert mid.progress == pytest.approx(0.5)
    assert mid.episode_active is True
