"""Contract and failure tests for the single customer publisher."""

from __future__ import annotations

import fcntl
import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts import deploy_parallax_customer_publisher as deployment
from scripts import parallax_customer_publisher as publisher

NOW = datetime(2026, 10, 8, 0, 30, tzinfo=timezone.utc)


def play(market_id="1083096", *, title=True, matchup=None, start="2026-10-11T17:00:00+00:00"):
    result = {"action": "BUY", "publication_eligible": True, "sport": "NFL", "venue": "PMUS",
              "market_id": market_id, "contract_side": "NO", "matchup": matchup,
              "resolution_time": start, "price": 0.4, "model_probability": 0.6, "edge_pp": 20.0}
    if title:
        result["market_title"] = "Who will win in the upcoming football event Indianapolis Colts vs Pittsburgh Steelers scheduled for October 11, 2026 at 5:00 PM UTC?"
    return result


def source(lane="nfl", plays=None, buy=None):
    plays = [play()] if plays is None and lane == "nfl" else (plays or [])
    result = {"schema_version": "parallax.public.v1", "lane": lane.upper(), "read_only": True,
              "generated_at": NOW.isoformat(), "buy_publication_eligible": True,
              "summary": {"buy": len(plays) if buy is None else buy}, "plays": plays}
    if lane == "nfl":
        result["slate"] = {"dates": [{"games": [{"away_team": "IND", "home_team": "PIT",
            "date": "2026-10-11", "schedule_status": "SCHEDULED", "status": "BUY",
            "start_time": "2026-10-11T17:00:00+00:00"}]}]}
    return result


def fixtures(tmp_path):
    feed = tmp_path / "public_feed"
    feed.mkdir()
    for lane in publisher.LANES:
        (feed / f"{lane}.json").write_text(json.dumps(source(lane)))
    db = tmp_path / "alerts.sqlite"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE buy_alert_state (sport TEXT, venue TEXT, market_id TEXT, side TEXT, matchup TEXT, game_start TEXT)")
    return feed, db


def test_missing_nfl_matchup_1083096_uses_only_sanitized_title_and_slate(tmp_path):
    feed, db = fixtures(tmp_path)
    expected, _ = publisher.build_all(feed, db, NOW)
    assert expected["nfl"]["summary"]["buy"] == 1
    assert expected["nfl"]["plays"][0]["matchup"] == "IND at PIT"
    assert expected["nfl"]["plays"][0]["start_time"] == "2026-10-11T17:00:00+00:00"


def test_nfl_certified_buy_is_authority_not_slate_status(tmp_path):
    feed, db = fixtures(tmp_path)
    nfl = source()
    nfl["plays"] = [play("1083103")]
    nfl["plays"][0]["market_title"] = "Who will win in the upcoming football event Chicago Bears vs Green Bay Packers scheduled for October 11, 2026 at 8:25 PM UTC?"
    nfl["summary"]["buy"] = 1
    nfl["slate"]["dates"][0]["games"] = [{
        "away_team": "CHI", "home_team": "GB", "date": "2026-10-11",
        "schedule_status": "SCHEDULED", "status": "PASS",
        "start_time": "2026-10-11T20:25:00+00:00",
    }]
    (feed / "nfl.json").write_text(json.dumps(nfl))
    expected, _ = publisher.build_all(feed, db, NOW)
    assert expected["nfl"]["summary"]["buy"] == 1
    assert expected["nfl"]["plays"][0]["matchup"] == "CHI at GB"
    assert expected["nfl"]["plays"][0]["start_time"] == "2026-10-11T20:25:00+00:00"


def test_kalshi_rams_market_id_matches_slate_la_alias(tmp_path):
    feed, db = fixtures(tmp_path)
    nfl = source()
    nfl["plays"] = [{
        **play("KXNFLGAME-26OCT12BUFLAR-LAR", title=False),
        "venue": "KALSHI",
        "contract_side": "YES",
        "resolution_time": "2026-10-12T20:15:00+00:00",
    }]
    nfl["summary"]["buy"] = 1
    nfl["slate"]["dates"][0]["games"] = [{
        "away_team": "BUF", "home_team": "LA", "date": "2026-10-12",
        "schedule_status": "SCHEDULED", "status": "PASS",
        "start_time": "2026-10-12T20:15:00+00:00",
    }]
    (feed / "nfl.json").write_text(json.dumps(nfl))
    expected, _ = publisher.build_all(feed, db, NOW)
    assert expected["nfl"]["summary"]["buy"] == 1
    assert expected["nfl"]["plays"][0]["matchup"] == "BUF at LA"
    assert expected["nfl"]["plays"][0]["start_time"] == "2026-10-12T20:15:00+00:00"


def test_db_enrichment_requires_exact_sport_venue_market_and_side(tmp_path):
    feed, db = fixtures(tmp_path)
    cfb = source("cfb", [{**play("777", title=False, start=None), "sport": "CFB"}])
    (feed / "cfb.json").write_text(json.dumps(cfb))
    with sqlite3.connect(db) as con:
        con.executemany("INSERT INTO buy_alert_state VALUES (?,?,?,?,?,?)", [
            ("CFB", "POLYMARKET", "777", "YES", "wrong side", "2026-10-10T00:00:00+00:00"),
            ("CFB", "KALSHI", "777", "NO", "wrong venue", "2026-10-10T00:00:00+00:00"),
            ("NFL", "POLYMARKET", "777", "NO", "wrong sport", "2026-10-10T00:00:00+00:00"),
        ])
    with pytest.raises(ValueError, match="missing matchup/start_time"):
        publisher.build_all(feed, db, NOW)
    with sqlite3.connect(db) as con:
        con.execute("INSERT INTO buy_alert_state VALUES (?,?,?,?,?,?)",
                    ("CFB", "POLYMARKET", "777", "NO", "Texas at Oklahoma", "2026-10-10T19:30:00+00:00"))
    expected, _ = publisher.build_all(feed, db, NOW)
    assert expected["cfb"]["plays"][0]["matchup"] == "Texas at Oklahoma"
    assert expected["cfb"]["plays"][0]["start_time"] == "2026-10-10T19:30:00+00:00"


def test_all_lane_source_customer_count_equality_is_mandatory(tmp_path):
    feed, db = fixtures(tmp_path)
    (feed / "nfl.json").write_text(json.dumps(source(buy=2)))
    with pytest.raises(ValueError, match="BUY mismatch source=2 customer=1"):
        publisher.build_all(feed, db, NOW)
    (feed / "nfl.json").write_text(json.dumps(source(plays=[{**play(), "publication_eligible": False}])))
    with pytest.raises(ValueError, match="uncertified source BUY"):
        publisher.build_all(feed, db, NOW)


def test_wrong_and_dirty_customer_worktree_fail_closed(monkeypatch, tmp_path):
    def fake_git(_worktree, *args, **_kwargs):
        if args[:2] == ("rev-parse", "--show-toplevel"):
            return subprocess.CompletedProcess(args, 0, str(tmp_path) + "\n", "")
        if args[0] == "branch":
            return subprocess.CompletedProcess(args, 0, "main\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(publisher, "git", fake_git)
    with pytest.raises(RuntimeError, match="wrong customer worktree"):
        publisher.checked_worktree(tmp_path)
    def dirty_git(_worktree, *args, **_kwargs):
        if args[:2] == ("rev-parse", "--show-toplevel"):
            return subprocess.CompletedProcess(args, 0, str(tmp_path) + "\n", "")
        return subprocess.CompletedProcess(args, 0, "parallax-live-data\n" if args[0] == "branch" else " M feeds/nfl.json\n", "")
    monkeypatch.setattr(publisher, "git", dirty_git)
    with pytest.raises(RuntimeError, match="dirty customer worktree"):
        publisher.checked_worktree(tmp_path)


def test_lock_contention_does_not_touch_health_or_worktree(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    lock = state / "customer_publisher.lock"
    with lock.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="already running"):
            publisher.publish(tmp_path / "worktree", tmp_path / "feeds", tmp_path / "db", state, NOW)
    assert not (state / "customer_publisher_health.json").exists()


def test_push_race_marks_health_failed_and_never_claims_remote_sha(monkeypatch, tmp_path):
    feed, db = fixtures(tmp_path)
    worktree, state = tmp_path / "worktree", tmp_path / "state"
    (worktree / "feeds").mkdir(parents=True)
    for lane in publisher.LANES:
        (worktree / "feeds" / f"{lane}.json").write_text(json.dumps({"schema_version": "parallax.customer.v1", "summary": {"buy": 0}, "plays": [], "source_feed_generated_at": NOW.isoformat()}))
    monkeypatch.setattr(publisher, "checked_worktree", lambda _path: "a" * 40)
    calls = []
    def fake_git(_worktree, *args, **kwargs):
        calls.append(args)
        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(args, 0, ("a" if args[1] == "origin/parallax-live-data" else "b") * 40 + "\n", "")
        if args[0] == "push":
            return subprocess.CompletedProcess(args, 1, "", "non-fast-forward")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(publisher, "git", fake_git)
    with pytest.raises(RuntimeError, match="push race or failure"):
        publisher.publish(worktree, feed, db, state, NOW)
    health = json.loads((state / "customer_publisher_health.json").read_text())
    assert health["state"] == "FAILED"
    assert "remote_head" not in health
    assert [args[0] for args in calls].count("push") == 1


def test_remote_payload_verification_rejects_count_drift(monkeypatch, tmp_path):
    def fake_git(_worktree, *args, **_kwargs):
        return subprocess.CompletedProcess(args, 0, json.dumps({"schema_version": publisher.CUSTOMER_SCHEMA,
            "summary": {"buy": 0}, "plays": []}), "")
    monkeypatch.setattr(publisher, "git", fake_git)
    expected = {lane: {"summary": {"buy": 1}, "plays": [{}]} for lane in publisher.LANES}
    with pytest.raises(RuntimeError, match="remote customer BUY mismatch"):
        publisher.verify_payloads(tmp_path, expected, "f" * 40)


def test_unchanged_remote_writes_healthy_count_and_sha(monkeypatch, tmp_path):
    feed, db = fixtures(tmp_path)
    worktree, state = tmp_path / "worktree", tmp_path / "state"
    (worktree / "feeds").mkdir(parents=True)
    expected, _ = publisher.build_all(feed, db, NOW)
    for lane in publisher.LANES:
        (worktree / "feeds" / f"{lane}.json").write_text(json.dumps(expected[lane]))
    monkeypatch.setattr(publisher, "checked_worktree", lambda _path: "a" * 40)
    calls = []
    def fake_git(_worktree, *args, **_kwargs):
        calls.append(args[0])
        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(args, 0, "a" * 40 + "\n", "")
        if args[0] == "ls-remote":
            return subprocess.CompletedProcess(args, 0, "a" * 40 + "\trefs/heads/parallax-live-data\n", "")
        if args[0] == "show":
            lane = args[1].split("/")[-1]
            return subprocess.CompletedProcess(args, 0, (worktree / "feeds" / lane).read_text(), "")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(publisher, "git", fake_git)
    health = publisher.publish(worktree, feed, db, state, NOW)
    assert health["state"] == "HEALTHY"
    assert health["remote_head"] == "a" * 40
    assert health["changed_lanes"] == []
    assert all(health["lanes"][lane]["source_buy"] == health["lanes"][lane]["customer_buy"] for lane in publisher.LANES)
    assert "push" not in calls


def test_live_source_refresh_after_snapshot_does_not_invalidate_publication(monkeypatch, tmp_path):
    feed, db = fixtures(tmp_path)
    worktree, state = tmp_path / "worktree", tmp_path / "state"
    (worktree / "feeds").mkdir(parents=True)
    expected, _ = publisher.build_all(feed, db, NOW)
    for lane in publisher.LANES:
        (worktree / "feeds" / f"{lane}.json").write_text(json.dumps(expected[lane]))
    monkeypatch.setattr(publisher, "checked_worktree", lambda _path: "a" * 40)
    mutated = {"done": False}

    def fake_git(_worktree, *args, **_kwargs):
        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(args, 0, "a" * 40 + "\n", "")
        if args[0] == "ls-remote":
            return subprocess.CompletedProcess(args, 0, "a" * 40 + "\trefs/heads/parallax-live-data\n", "")
        if args[0] == "show":
            if not mutated["done"]:
                current = json.loads((feed / "nfl.json").read_text())
                current["generated_at"] = "2026-10-08T00:30:01+00:00"
                (feed / "nfl.json").write_text(json.dumps(current))
                mutated["done"] = True
            lane = args[1].split("/")[-1]
            return subprocess.CompletedProcess(args, 0, (worktree / "feeds" / lane).read_text(), "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(publisher, "git", fake_git)
    health = publisher.publish(worktree, feed, db, state, NOW)
    assert mutated["done"] is True
    assert health["state"] == "HEALTHY"


def test_deployment_bootstrap_failure_restores_prior_plists_and_loaded_jobs(tmp_path):
    daemons, backups = tmp_path / "daemons", tmp_path / "backups"
    daemons.mkdir()
    before = {}
    for label in deployment.LABELS:
        before[label] = f"prior {label}".encode()
        (daemons / f"{label}.plist").write_bytes(before[label])

    class FakeDeployment(deployment.Deployment):
        def __init__(self):
            super().__init__(daemons, backups)
            self.running = set(deployment.OLD)
            self.fail_new = True
        def preflight(self, *_args):
            pass
        def loaded(self, label):
            return label in self.running
        def bootout(self, label):
            self.running.discard(label)
        def bootstrap(self, label):
            if label == deployment.NEW and self.fail_new:
                self.fail_new = False
                raise RuntimeError("simulated bootstrap failure")
            self.running.add(label)
        def command(self, *args, **kwargs):
            return subprocess.CompletedProcess(args, 1, "", "")

    deploy = FakeDeployment()
    with pytest.raises(RuntimeError, match="simulated bootstrap failure"):
        deploy.apply(tmp_path / "release", "f" * 40, tmp_path / "worktree", "tester")
    assert deploy.running == set(deployment.OLD)
    for label in deployment.LABELS:
        assert (daemons / f"{label}.plist").read_bytes() == before[label]
    assert len(list(backups.glob("*/manifest.json"))) == 1
