"""The persistence policy — what turns 490,000 daily frames into useful rows.

Every test drives the clock explicitly rather than sleeping, so the suite stays
fast and the boundaries are exact.
"""

from __future__ import annotations

import pytest

from pepperwatch.worker import PersistPolicy


def policy(**overrides) -> PersistPolicy:
    settings = dict(
        heartbeat_detected_s=30.0,
        heartbeat_clean_s=300.0,
        on_state_change=True,
        state_change_debounce_s=4.0,
        min_write_interval_s=8.0,
    )
    settings.update(overrides)
    return PersistPolicy(**settings)


def test_first_observation_always_writes() -> None:
    decision = policy().decide("none", now=0.0)
    assert decision.write
    assert decision.trigger == "auto"


def test_steady_state_does_not_write() -> None:
    p = policy()
    p.decide("low", now=0.0)
    for tick in range(1, 100):
        assert not p.decide("low", now=tick * 0.1).write


def test_severity_change_writes_once_debounce_elapses() -> None:
    p = policy()
    p.decide("none", now=0.0)

    # Held for less than the debounce window: not yet believed.
    assert not p.decide("high", now=10.0).write
    assert not p.decide("high", now=12.0).write

    decision = p.decide("high", now=14.1)
    assert decision.write
    assert decision.trigger == "state_change"
    assert "none -> high" in decision.reason or "clean -> high" in decision.reason


def test_flapping_severity_never_writes() -> None:
    """The bug this policy exists to prevent: 5.6 events/second on real frames.

    A box count sitting on a band boundary alternates every frame. No
    alternation should ever hold long enough to be believed.
    """
    p = policy()
    p.decide("moderate", now=0.0)

    writes = 0
    for tick in range(1, 2000):
        now = tick * 0.1  # 10 fps for 200 seconds
        band = "moderate" if tick % 2 else "high"
        if p.decide(band, now=now).write:
            writes += 1

    # Only heartbeats may fire: 200 s at a 30 s detected heartbeat is about 6.
    assert writes <= 7, f"flapping produced {writes} writes"
    # Debounce alone is sufficient here: a flapping band is never believed, so
    # no write ever becomes due and the rate cap is never consulted. The cap is
    # exercised separately by test_rate_cap_counts_what_it_suppressed.
    assert p.suppressed == 0


def test_a_change_that_settles_is_still_written_after_the_flapping_stops() -> None:
    """Debounce must delay a real change, never lose it."""
    p = policy()
    p.decide("low", now=0.0)

    for tick in range(1, 40):  # flap for 4 s
        p.decide("low" if tick % 2 else "high", now=tick * 0.1)

    # Now "high" settles.
    assert not p.decide("high", now=4.1).write
    decision = p.decide("high", now=9.0)
    assert decision.write
    assert decision.trigger == "state_change"


def test_heartbeat_fires_for_a_clean_scan() -> None:
    p = policy()
    p.decide("none", now=0.0)
    assert not p.decide("none", now=299.0).write
    decision = p.decide("none", now=301.0)
    assert decision.write
    assert decision.trigger == "heartbeat"


def test_detected_heartbeat_is_faster_than_the_clean_one() -> None:
    p = policy()
    p.decide("high", now=0.0)
    assert not p.decide("high", now=29.0).write
    assert p.decide("high", now=31.0).write


def test_rate_cap_bounds_writes_regardless_of_trigger() -> None:
    p = policy(heartbeat_detected_s=0.1, heartbeat_clean_s=0.1, min_write_interval_s=8.0)
    p.decide("low", now=0.0)

    writes = sum(1 for tick in range(1, 601) if p.decide("low", now=tick * 0.1).write)
    # 60 seconds with an 8-second floor allows at most 7 further writes.
    assert writes <= 7, f"rate cap breached: {writes} writes in 60 s"


def test_rate_cap_counts_what_it_suppressed() -> None:
    """Suppression is reported, so a misconfiguration is visible not silent."""
    p = policy(heartbeat_detected_s=0.1, heartbeat_clean_s=0.1)
    p.decide("low", now=0.0)
    for tick in range(1, 50):
        p.decide("low", now=tick * 0.1)
    assert p.suppressed > 0


def test_state_change_can_be_disabled() -> None:
    p = policy(on_state_change=False)
    p.decide("none", now=0.0)
    for tick in range(1, 200):
        assert not p.decide("high", now=tick * 0.1).write


def test_confirmed_severity_lags_until_debounce_passes() -> None:
    p = policy()
    p.decide("none", now=0.0)
    p.decide("high", now=1.0)
    assert p.confirmed_severity == "none"
    p.decide("high", now=12.0)
    assert p.confirmed_severity == "high"


@pytest.mark.parametrize("fps", [5, 10, 17, 25])
def test_daily_write_volume_stays_manageable_at_any_frame_rate(fps: int) -> None:
    """The headline requirement: an eight-hour cycle must not flood the card.

    Worst case is a detector flapping across bands for the whole session.
    """
    p = policy()
    p.decide("moderate", now=0.0)

    seconds = 8 * 3600
    total_frames = seconds * fps
    writes = 0
    for index in range(1, total_frames + 1, max(1, fps // 5)):
        now = index / fps
        band = ["low", "moderate", "high"][index % 3]
        if p.decide(band, now=now).write:
            writes += 1

    # An 8-hour cycle floored at 8 s between writes cannot exceed 3,600.
    assert writes <= 3600, f"{fps} fps produced {writes} writes in 8 h"
