import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from src.domain import (
    Actor,
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService

ADMIN = Actor("admin", "admin")
ATHLETE_ROLE = Actor("athlete-1", "athlete")
INSPECTOR = Actor("inspector-1", "inspector")
PANEL = Actor("panel", "panel")


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class WhereaboutsFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        # Filings are submitted before the quarter starts; the clock is
        # mutable so tests can move "now" across the quarter.
        self.now = [datetime(2026, 6, 25, tzinfo=timezone.utc)]
        self.service = DomainService(
            self.repo, RuleEngine(), clock=lambda: _iso(self.now[0])
        )
        self.athlete = self.service.create(
            ADMIN, "athlete", {"name": "A. Runner", "discipline": "track"}
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _file(self, period="2026-Q3", location="Arena A", day=10, hour=8, actor=None):
        month = 7 if period == "2026-Q3" else 10
        return self.service.create(
            actor or ATHLETE_ROLE,
            "whereabouts",
            {
                "athlete_id": self.athlete["id"],
                "period": period,
                "location": location,
                "window_starts_at": "2026-%02d-%02dT%02d:00:00Z" % (month, day, hour),
            },
        )

    def _dispatch(self, check_at, period="2026-Q3", actor=None):
        return self.service.create(
            actor or INSPECTOR,
            "dispatch",
            {
                "athlete_id": self.athlete["id"],
                "period": period,
                "check_at": check_at,
            },
        )

    def test_filing_creates_version_one_and_duplicate_period_is_rejected(self):
        filing = self._file()
        self.assertEqual(filing["status"], "active")
        self.assertEqual(filing["version"], 1)
        versions = self.service.whereabouts_versions(filing["id"])
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["location"], "Arena A")
        self.assertEqual(versions[0]["version_no"], 1)
        with self.assertRaises(ConflictError):
            self._file()

    def test_amend_keeps_old_version_and_bumps_number(self):
        filing = self._file(location="Old Gym")
        updated = self.service.transition(
            ATHLETE_ROLE,
            filing["id"],
            "amend",
            {"location": "New Track", "window_starts_at": "2026-07-10T10:00:00Z"},
        )
        self.assertEqual(updated["version"], 2)
        self.assertEqual(updated["data"]["location"], "New Track")
        versions = self.service.whereabouts_versions(filing["id"])
        self.assertEqual([v["version_no"] for v in versions], [1, 2])
        self.assertEqual([v["location"] for v in versions], ["Old Gym", "New Track"])

    def test_dispatch_snapshots_effective_version_for_check_date(self):
        filing = self._file(location="Old Gym", hour=8)
        # Athlete moves training ground mid-quarter; the old version stays.
        self.now[0] = datetime(2026, 7, 15, tzinfo=timezone.utc)
        self.service.transition(
            ATHLETE_ROLE,
            filing["id"],
            "amend",
            {"location": "New Track", "window_starts_at": "2026-07-20T10:00:00Z"},
        )
        before = self.service.effective_whereabouts(
            self.athlete["id"], "2026-Q3", "2026-07-10T08:30:00Z"
        )
        after = self.service.effective_whereabouts(
            self.athlete["id"], "2026-Q3", "2026-07-20T10:30:00Z"
        )
        self.assertEqual(before["version_no"], 1)
        self.assertEqual(before["location"], "Old Gym")
        self.assertEqual(after["version_no"], 2)
        self.assertEqual(after["location"], "New Track")

        dispatch = self._dispatch("2026-07-20T10:30:00Z")
        self.assertEqual(dispatch["status"], "planned")
        self.assertEqual(dispatch["data"]["version_no"], 2)
        self.assertEqual(dispatch["data"]["location"], "New Track")

    def test_dispatch_rejects_missing_location_and_elapsed_window(self):
        filing = self._file()
        self.repo.execute_raw(
            "UPDATE whereabouts_versions SET location = '' WHERE whereabouts_id = ?",
            (filing["id"],),
        )
        with self.assertRaises(ValidationError):
            self._dispatch("2026-07-10T08:30:00Z")
        self.repo.execute_raw(
            "UPDATE whereabouts_versions SET location = 'Arena A' WHERE whereabouts_id = ?",
            (filing["id"],),
        )
        with self.assertRaises(ValidationError):
            self._dispatch("2026-07-10T09:00:00Z")  # window end reached
        with self.assertRaises(ValidationError):
            self._dispatch("2026-07-10T11:00:00Z")

    def test_dispatch_requires_filing(self):
        with self.assertRaises(ValidationError):
            self._dispatch("2026-07-10T08:30:00Z", period="2026-Q4")

    def test_closed_quarter_blocks_amend_but_stays_readable(self):
        filing = self._file()
        closed = self.service.transition(
            ADMIN, filing["id"], "close", {"reason": "quarter archived"}
        )
        self.assertEqual(closed["status"], "closed")
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                ATHLETE_ROLE, filing["id"], "amend", {"location": "Too Late"}
            )
        # Inspectors can still read the archived version for an in-period date.
        effective = self.service.effective_whereabouts(
            self.athlete["id"], "2026-Q3", "2026-07-10T08:30:00Z"
        )
        self.assertEqual(effective["filing_status"], "closed")

    def test_misses_cumulate_and_third_opens_review_cancel_ignored(self):
        self._file()
        real_now = datetime.now(timezone.utc)
        # Seed two older misses: one outside the trailing 12 months, one inside.
        self._seed_miss(real_now - timedelta(days=400))
        self._seed_miss(real_now - timedelta(days=10))
        counts = self.service.miss_count(self.athlete["id"], at=_iso(real_now))
        self.assertEqual(counts["count"], 1)

        dispatch1 = self._dispatch("2026-07-10T08:30:00Z")
        self.service.transition(
            INSPECTOR,
            dispatch1["id"],
            "execute",
            {"outcome": "miss", "checked_at": _iso(real_now - timedelta(days=5))},
        )
        counts = self.service.miss_count(self.athlete["id"], at=_iso(real_now))
        self.assertEqual(counts["count"], 2)

        # A cancellation never counts.
        cancelled = self._dispatch("2026-07-10T08:40:00Z")
        self.service.transition(INSPECTOR, cancelled["id"], "cancel", {"reason": "weather"})
        self.assertEqual(
            self.service.miss_count(self.athlete["id"], at=_iso(real_now))["count"], 2
        )
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                INSPECTOR, cancelled["id"], "execute", {"outcome": "miss"}
            )

        # Third trailing miss opens the case pending review.
        dispatch3 = self._dispatch("2026-07-10T08:45:00Z")
        updated = self.service.transition(
            INSPECTOR,
            dispatch3["id"],
            "execute",
            {"outcome": "miss", "checked_at": _iso(real_now - timedelta(days=1))},
        )
        review_id = updated["data"]["review_id"]
        self.assertTrue(review_id)
        review = self.service.get(review_id)
        self.assertEqual(review["kind"], "review")
        self.assertEqual(review["status"], "pending_review")
        self.assertEqual(review["data"]["miss_count"], 3)
        self.assertEqual(review["data"]["trigger_dispatch_id"], dispatch3["id"])

        # A fourth miss while a review is open does not duplicate the case.
        dispatch4 = self._dispatch("2026-07-10T08:50:00Z")
        updated4 = self.service.transition(
            INSPECTOR,
            dispatch4["id"],
            "execute",
            {"outcome": "miss", "checked_at": _iso(real_now - timedelta(hours=12))},
        )
        self.assertEqual(updated4["data"]["review_id"], review_id)
        pending = [r for r in self.service.list("review") if r["status"] == "pending_review"]
        self.assertEqual(len(pending), 1)

        closed = self.service.transition(
            PANEL, review_id, "close_review", {"decision": "sanction"}
        )
        self.assertEqual(closed["status"], "closed")

    def _seed_miss(self, checked_at):
        return self.repo.create_entity(
            str(uuid4()),
            "dispatch",
            "executed",
            {
                "athlete_id": self.athlete["id"],
                "period": "2026-Q2",
                "check_at": _iso(checked_at),
                "checked_at": _iso(checked_at),
                "outcome": "miss",
            },
            INSPECTOR.user_id,
        )

    def test_hit_does_not_count(self):
        self._file()
        dispatch = self._dispatch("2026-07-10T08:30:00Z")
        executed = self.service.transition(
            INSPECTOR, dispatch["id"], "execute", {"outcome": "hit"}
        )
        self.assertEqual(executed["data"]["outcome"], "hit")
        self.assertNotIn("review_id", executed["data"])
        self.assertEqual(self.service.miss_count(self.athlete["id"])["count"], 0)

    def test_roles_are_enforced(self):
        with self.assertRaises(PermissionDenied):
            self.service.create(
                INSPECTOR,
                "whereabouts",
                {
                    "athlete_id": self.athlete["id"],
                    "period": "2026-Q3",
                    "location": "Arena A",
                    "window_starts_at": "2026-07-10T08:00:00Z",
                },
            )
        with self.assertRaises(PermissionDenied):
            self.service.create(
                ATHLETE_ROLE,
                "dispatch",
                {
                    "athlete_id": self.athlete["id"],
                    "period": "2026-Q3",
                    "check_at": "2026-07-10T08:30:00Z",
                },
            )

    def test_window_must_be_60_minutes_inside_period(self):
        filing = self._file()
        version = self.service.whereabouts_versions(filing["id"])[0]
        start = datetime.fromisoformat(version["window_starts_at"])
        end = datetime.fromisoformat(version["window_ends_at"])
        self.assertEqual(end - start, timedelta(minutes=60))
        with self.assertRaises(ValidationError):
            self.service.create(
                ATHLETE_ROLE,
                "whereabouts",
                {
                    "athlete_id": self.athlete["id"],
                    "period": "2026-Q3",
                    "location": "Arena A",
                    "window_starts_at": "2026-10-01T08:00:00Z",
                },
            )
        with self.assertRaises(ValidationError):
            self.service.create(
                ATHLETE_ROLE,
                "whereabouts",
                {
                    "athlete_id": self.athlete["id"],
                    "period": "bad",
                    "location": "Arena A",
                    "window_starts_at": "2026-07-10T08:00:00Z",
                },
            )

    def test_dashboard_shows_versions_dispatches_and_counts(self):
        filing = self._file()
        self.service.transition(
            ATHLETE_ROLE, filing["id"], "amend", {"location": "New Track"}
        )
        dispatch = self._dispatch("2026-07-10T08:30:00Z")
        self.now[0] = datetime(2026, 7, 10, 8, 35, tzinfo=timezone.utc)
        self.service.transition(INSPECTOR, dispatch["id"], "execute", {"outcome": "miss"})
        board = self.service.dashboard()
        row = next(
            item for item in board["athletes"] if item["athlete_id"] == self.athlete["id"]
        )
        self.assertEqual(row["dispatch_total"], 1)
        self.assertEqual(row["miss_total"], 1)
        self.assertEqual(row["misses_trailing_12m"], 1)
        filing_row = next(
            item for item in board["whereabouts"] if item["whereabouts_id"] == filing["id"]
        )
        self.assertEqual(filing_row["version"], 2)
        self.assertEqual(filing_row["version_count"], 2)
        dispatch_row = board["dispatches"][0]
        self.assertEqual(dispatch_row["outcome"], "miss")
        self.assertEqual(dispatch_row["version_no"], 2)


if __name__ == "__main__":
    unittest.main()
