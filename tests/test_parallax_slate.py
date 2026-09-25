from __future__ import annotations

import pytest

from parallax.slate import reconcile_slate


def games(count: int):
    return [
        {
            "game_id": f"g-{index}",
            "date": "2026-09-27",
            "start_time": f"2026-09-27T{17 + index // 4:02d}:00:00+00:00",
            "away_team": f"A{index}",
            "home_team": f"H{index}",
            "schedule_status": "SCHEDULED",
        }
        for index in range(count)
    ]


@pytest.mark.parametrize("count", [5, 9, 11, 14, 15, 16])
def test_dynamic_slate_never_assumes_game_count(count):
    report = reconcile_slate(games(count), [], discovery_complete=True)

    assert report["expected_games"] == count
    assert report["accounted_games"] == count
    assert report["all_games_accounted"] is True
    assert report["status_counts"] == {"NO_MARKET": count}
    assert report["dates"][0]["expected_games"] == count
    assert report["dates"][0]["accounted_games"] == count


def test_strongest_evaluated_action_wins_at_game_level():
    report = reconcile_slate(
        games(3),
        [
            {"game_id": "g-0", "verdict": "PASS"},
            {"game_id": "g-0", "verdict": "WATCH"},
            {"game_id": "g-1", "verdict": "PASS"},
            {"game_id": "g-2", "verdict": "WATCH"},
            {"game_id": "g-2", "verdict": "BUY"},
        ],
        discovery_complete=True,
    )

    assert [row["status"] for row in report["dates"][0]["games"]] == [
        "WATCH",
        "PASS",
        "BUY",
    ]


def test_partial_discovery_never_mislabels_unseen_game_as_no_market():
    report = reconcile_slate(
        games(2),
        [{"game_id": "g-0", "verdict": "PASS"}],
        discovery_complete=False,
    )

    assert report["dates"][0]["games"][0]["status"] == "PASS"
    assert report["dates"][0]["games"][1]["status"] == "DATA_UNAVAILABLE"
    assert report["market_data_complete"] is False


def test_explicit_failure_precedence_is_mapping_then_data_then_no_market():
    report = reconcile_slate(
        games(3),
        [],
        discovery_complete=True,
        data_unavailable_game_ids={"g-1"},
        mapping_failure_game_ids={"g-0"},
    )

    statuses = {row["game_id"]: row["status"] for row in report["dates"][0]["games"]}
    assert statuses == {
        "g-0": "MAPPING_FAILURE",
        "g-1": "DATA_UNAVAILABLE",
        "g-2": "NO_MARKET",
    }


def test_postponed_and_cancelled_games_remain_accounted_without_market_data():
    scheduled = games(2)
    scheduled[0]["schedule_status"] = "POSTPONED"
    scheduled[1]["schedule_status"] = "CANCELED"

    report = reconcile_slate(scheduled, [], discovery_complete=False)

    assert [row["status"] for row in report["dates"][0]["games"]] == [
        "POSTPONED",
        "CANCELLED",
    ]
    assert report["all_games_accounted"] is True


def test_duplicate_authoritative_game_ids_fail_closed():
    duplicated = games(2)
    duplicated[1]["game_id"] = duplicated[0]["game_id"]

    with pytest.raises(ValueError, match="duplicate game_id"):
        reconcile_slate(duplicated, [], discovery_complete=True)
