import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src.domain import Actor, InvalidTransition, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine, effective_version, miss_count_12m, quarter_of
from src.service import DomainService


def _today():
    return datetime.now(timezone.utc).date()


def _current_quarter():
    return quarter_of(_today().isoformat())


def _quarter_first_day(quarter):
    year, q = quarter.split("-Q")
    return date(int(year), (int(q) - 1) * 3 + 1, 1)


def _prev_quarter(quarter):
    year, q = quarter.split("-Q")
    year, q = int(year), int(q)
    if q == 1:
        return "%04d-Q4" % (year - 1)
    return "%04d-Q%d" % (year, q - 1)


class WhereaboutsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.inspector = Actor("ins-1", "inspector")
        self.athlete = self.service.create(
            self.admin, "athlete", {"name": "A. Rider", "discipline": "cycling"}
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _file(self, slot_date=None, slot_start="08:00", location="Track A", actor=None):
        data = {
            "athlete_id": self.athlete["id"],
            "quarter": _current_quarter(),
            "slot_date": (slot_date or _today()).isoformat(),
            "slot_start": slot_start,
            "location": location,
        }
        return self.service.create(actor or self.admin, "whereabouts", data)

    def _dispatch(self, check_date=None, check_time=None, actor=None):
        data = {
            "athlete_id": self.athlete["id"],
            "check_date": (check_date or _today()).isoformat(),
        }
        if check_time:
            data["check_time"] = check_time
        return self.service.create(actor or self.inspector, "dispatch", data)

    def _miss(self, dispatch, missed_at=None):
        self.service.transition(self.inspector, dispatch["id"], "execute", {})
        data = {"missed_at": missed_at.isoformat()} if missed_at else {}
        return self.service.transition(self.inspector, dispatch["id"], "register_miss", data)

    def _whereabouts_cases(self):
        return [
            case
            for case in self.service.list("case")
            if case["data"].get("source") == "whereabouts"
        ]

    def test_file_and_amend_keeps_versions(self):
        filing = self._file(location="Track A")
        self.assertEqual(filing["status"], "filed")
        self.assertEqual(filing["data"]["revision"], 1)
        self.assertEqual(filing["data"]["slot_minutes"], 60)

        amended = self.service.transition(
            self.admin,
            filing["id"],
            "amend",
            {"slot_date": _today().isoformat(), "slot_start": "19:00", "location": "Track B"},
        )
        self.assertEqual(amended["data"]["revision"], 2)
        self.assertEqual(amended["data"]["location"], "Track B")
        self.assertEqual(len(amended["data"]["versions"]), 1)
        self.assertEqual(amended["data"]["versions"][0]["location"], "Track A")

        again = self.service.transition(
            self.admin,
            filing["id"],
            "amend",
            {"slot_date": _today().isoformat(), "slot_start": "20:00", "location": "Track C"},
        )
        self.assertEqual(again["data"]["revision"], 3)
        self.assertEqual(
            [v["location"] for v in again["data"]["versions"]], ["Track A", "Track B"]
        )

    def test_duplicate_filing_same_quarter_rejected(self):
        self._file()
        with self.assertRaises(ValidationError):
            self._file(location="Track B")

    def test_backfill_after_quarter_closed_rejected(self):
        old_quarter = _prev_quarter(_current_quarter())
        with self.assertRaises(ValidationError):
            self.service.create(
                self.admin,
                "whereabouts",
                {
                    "athlete_id": self.athlete["id"],
                    "quarter": old_quarter,
                    "slot_date": _quarter_first_day(old_quarter).isoformat(),
                    "slot_start": "08:00",
                    "location": "Track A",
                },
            )

    def test_amend_after_quarter_closed_rejected(self):
        old_quarter = _prev_quarter(_current_quarter())
        seeded = self.repo.create_entity(
            "seeded-old-filing",
            "whereabouts",
            "filed",
            {
                "athlete_id": self.athlete["id"],
                "quarter": old_quarter,
                "slot_date": _quarter_first_day(old_quarter).isoformat(),
                "slot_start": "08:00",
                "location": "Track A",
                "filed_at": "2020-01-01T00:00:00+00:00",
                "revision": 1,
                "versions": [],
            },
            "seed",
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin,
                seeded["id"],
                "amend",
                {
                    "slot_date": _quarter_first_day(old_quarter).isoformat(),
                    "slot_start": "09:00",
                    "location": "Track B",
                },
            )

    def test_slot_outside_quarter_rejected(self):
        first_day = _quarter_first_day(_current_quarter())
        outside = first_day - timedelta(days=1)
        with self.assertRaises(ValidationError):
            self._file(slot_date=outside)

    def test_dispatch_snapshots_effective_version(self):
        filing = self._file(location="Track A")
        dispatch = self._dispatch()
        self.assertEqual(dispatch["status"], "dispatched")
        self.assertEqual(dispatch["data"]["location"], "Track A")
        self.assertEqual(dispatch["data"]["whereabouts_revision"], 1)
        self.assertEqual(dispatch["data"]["whereabouts_id"], filing["id"])

        self.service.transition(
            self.admin,
            filing["id"],
            "amend",
            {"slot_date": _today().isoformat(), "slot_start": "08:00", "location": "Track B"},
        )
        newer = self._dispatch()
        self.assertEqual(newer["data"]["location"], "Track B")
        self.assertEqual(newer["data"]["whereabouts_revision"], 2)
        # 已派出的单子保持原快照不变
        unchanged = self.service.get(dispatch["id"])
        self.assertEqual(unchanged["data"]["location"], "Track A")

    def test_dispatch_rejected_without_filing(self):
        with self.assertRaises(ValidationError):
            self._dispatch()

    def test_dispatch_rejected_when_location_missing(self):
        self.repo.create_entity(
            "seeded-no-location",
            "whereabouts",
            "filed",
            {
                "athlete_id": self.athlete["id"],
                "quarter": _current_quarter(),
                "slot_date": _today().isoformat(),
                "slot_start": "08:00",
                "location": "",
                "filed_at": _today().isoformat() + "T00:00:00+00:00",
                "revision": 1,
                "versions": [],
            },
            "seed",
        )
        with self.assertRaises(ValidationError):
            self._dispatch()

    def test_dispatch_rejected_when_slot_passed(self):
        self._file(slot_start="00:00")
        with self.assertRaises(ValidationError):
            self._dispatch(check_time="01:01")
        ok = self._dispatch(check_time="00:30")
        self.assertEqual(ok["status"], "dispatched")

    def test_dispatch_rejected_when_check_date_after_slot(self):
        first_day = _quarter_first_day(_current_quarter())
        self._file(slot_date=first_day, slot_start="08:00")
        with self.assertRaises(ValidationError):
            self._dispatch(check_date=first_day + timedelta(days=1))

    def test_register_miss_requires_executed(self):
        self._file()
        dispatch = self._dispatch()
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.inspector, dispatch["id"], "register_miss", {})

    def test_cancel_not_counted(self):
        self._file()
        cancelled = self._dispatch()
        cancelled = self.service.transition(self.inspector, cancelled["id"], "cancel", {})
        self.assertEqual(cancelled["status"], "cancelled")
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.inspector, cancelled["id"], "execute", {})

        self._miss(self._dispatch())
        self._miss(self._dispatch())
        self.assertEqual(self._whereabouts_cases(), [])
        summary = self.service.whereabouts_summary(self.athlete["id"])
        self.assertEqual(summary["miss_count_12m"], 2)

    def test_third_miss_within_12_months_opens_case(self):
        self._file()
        for _ in range(2):
            self._miss(self._dispatch())
        self.assertEqual(self._whereabouts_cases(), [])

        self._miss(self._dispatch())
        cases = self._whereabouts_cases()
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["status"], "open")
        self.assertEqual(cases[0]["data"]["miss_count"], 3)
        self.assertEqual(cases[0]["data"]["athlete_id"], self.athlete["id"])

        # 已有待审案件时不重复立案
        self._miss(self._dispatch())
        self.assertEqual(len(self._whereabouts_cases()), 1)

    def test_misses_older_than_12_months_not_counted(self):
        self._file()
        old_date = _today() - timedelta(days=400)
        self._miss(self._dispatch(), missed_at=old_date)
        self._miss(self._dispatch())
        self._miss(self._dispatch())
        self.assertEqual(self._whereabouts_cases(), [])

        self._miss(self._dispatch())
        self.assertEqual(len(self._whereabouts_cases()), 1)
        summary = self.service.whereabouts_summary(self.athlete["id"])
        self.assertEqual(summary["miss_count_12m"], 3)
        self.assertEqual(summary["miss_count_total"], 4)

    def test_permissions(self):
        viewer = Actor("viewer", "viewer")
        with self.assertRaises(PermissionDenied):
            self._file(actor=viewer)
        with self.assertRaises(PermissionDenied):
            self._file(actor=self.inspector)
        self._file()
        with self.assertRaises(PermissionDenied):
            self._dispatch(actor=viewer)
        dispatch = self._dispatch(actor=self.inspector)
        self.assertEqual(dispatch["status"], "dispatched")

    def test_summary_lists_versions_dispatches_and_counts(self):
        filing = self._file(location="Track A")
        self.service.transition(
            self.admin,
            filing["id"],
            "amend",
            {"slot_date": _today().isoformat(), "slot_start": "08:00", "location": "Track B"},
        )
        self._miss(self._dispatch())
        self._dispatch()

        summary = self.service.whereabouts_summary(self.athlete["id"])
        self.assertEqual(summary["athlete"]["id"], self.athlete["id"])
        self.assertEqual(len(summary["filings"]), 1)
        self.assertEqual(summary["filings"][0]["data"]["revision"], 2)
        self.assertEqual(len(summary["filings"][0]["data"]["versions"]), 1)
        self.assertEqual(len(summary["dispatches"]), 2)
        self.assertEqual(summary["miss_count_12m"], 1)


class WhereaboutsRuleFunctionsTest(unittest.TestCase):
    def test_effective_version_by_check_date(self):
        filing = {
            "data": {
                "revision": 3,
                "filed_at": "2026-03-10T09:00:00+00:00",
                "slot_date": "2026-03-20",
                "slot_start": "07:00",
                "location": "C",
                "versions": [
                    {
                        "revision": 1,
                        "filed_at": "2026-01-05T09:00:00+00:00",
                        "slot_date": "2026-01-10",
                        "slot_start": "06:00",
                        "location": "A",
                    },
                    {
                        "revision": 2,
                        "filed_at": "2026-02-05T09:00:00+00:00",
                        "slot_date": "2026-02-10",
                        "slot_start": "06:30",
                        "location": "B",
                    },
                ],
            }
        }
        self.assertEqual(effective_version(filing, "2026-01-20")["location"], "A")
        self.assertEqual(effective_version(filing, "2026-02-05")["location"], "B")
        self.assertEqual(effective_version(filing, "2026-03-31")["location"], "C")
        self.assertIsNone(effective_version(filing, "2026-01-01"))

    def test_miss_count_12m_window(self):
        dispatches = [
            {"status": "missed", "data": {"missed_at": "2026-09-01"}},
            {"status": "missed", "data": {"missed_at": "2025-10-01"}},
            {"status": "missed", "data": {"missed_at": "2025-09-25"}},
            {"status": "cancelled", "data": {"check_date": "2026-09-01"}},
            {"status": "executed", "data": {"check_date": "2026-09-01"}},
        ]
        self.assertEqual(miss_count_12m(dispatches, "2026-09-26"), 2)
        self.assertEqual(miss_count_12m(dispatches, "2025-10-01"), 2)


if __name__ == "__main__":
    unittest.main()
