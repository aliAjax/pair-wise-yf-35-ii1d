import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from src.http_api import create_server
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class HttpApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.now = datetime(2026, 6, 25, tzinfo=timezone.utc)
        service = DomainService(repo, RuleEngine(), clock=lambda: _iso(self.now))
        self.server = create_server("127.0.0.1", 0, service, RuleEngine(), "static")
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def _request(self, method, path, body=None, role="admin", user="tester"):
        data = None
        headers = {"X-User-Id": user, "X-Role": role}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            "http://127.0.0.1:%s%s" % (self.port, path),
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_filing_dispatch_effective_and_dashboard_over_http(self):
        status, athlete = self._request(
            "POST", "/api/athletes", {"name": "A. Runner", "discipline": "track"}
        )
        self.assertEqual(status, 201)
        athlete_id = athlete["id"]

        status, filing = self._request(
            "POST",
            "/api/whereabouts",
            {
                "athlete_id": athlete_id,
                "period": "2026-Q3",
                "location": "Old Gym",
                "window_starts_at": "2026-07-10T08:00:00Z",
            },
            role="athlete",
            user="athlete-1",
        )
        self.assertEqual(status, 201)
        self.assertEqual(filing["version"], 1)

        # Inspector cannot file on the athlete's behalf.
        status, payload = self._request(
            "POST",
            "/api/whereabouts",
            {
                "athlete_id": athlete_id,
                "period": "2026-Q4",
                "location": "Pool",
                "window_starts_at": "2026-10-10T08:00:00Z",
            },
            role="inspector",
        )
        self.assertEqual(status, 403)

        # Amend: old version retained, new version added.
        self.now = datetime(2026, 7, 15, tzinfo=timezone.utc)
        status, amended = self._request(
            "POST",
            "/api/entities/%s/actions" % filing["id"],
            {
                "action": "amend",
                "data": {"location": "New Track", "window_starts_at": "2026-07-20T10:00:00Z"},
            },
            role="athlete",
            user="athlete-1",
        )
        self.assertEqual(status, 200)
        self.assertEqual(amended["version"], 2)
        status, versions = self._request(
            "GET", "/api/whereabouts/%s/versions" % filing["id"]
        )
        self.assertEqual([v["location"] for v in versions["items"]], ["Old Gym", "New Track"])

        # Effective version resolved by check date.
        status, effective = self._request(
            "GET",
            "/api/whereabouts/effective?athlete_id=%s&period=2026-Q3&check_at=2026-07-10T08:30:00Z"
            % athlete_id,
            role="inspector",
        )
        self.assertEqual(status, 200)
        self.assertEqual(effective["location"], "Old Gym")

        # Dispatch against an elapsed window is rejected.
        status, payload = self._request(
            "POST",
            "/api/dispatches",
            {
                "athlete_id": athlete_id,
                "period": "2026-Q3",
                "check_at": "2026-07-20T11:30:00Z",
            },
            role="inspector",
        )
        self.assertEqual(status, 400)

        status, dispatch = self._request(
            "POST",
            "/api/dispatches",
            {
                "athlete_id": athlete_id,
                "period": "2026-Q3",
                "check_at": "2026-07-20T10:30:00Z",
            },
            role="inspector",
        )
        self.assertEqual(status, 201)
        self.assertEqual(dispatch["data"]["version_no"], 2)
        self.assertEqual(dispatch["data"]["location"], "New Track")

        status, executed = self._request(
            "POST",
            "/api/entities/%s/actions" % dispatch["id"],
            {"action": "execute", "data": {"outcome": "miss"}},
            role="inspector",
        )
        self.assertEqual(status, 200)
        self.assertEqual(executed["status"], "executed")

        status, counts = self._request(
            "GET", "/api/miss-count?athlete_id=%s" % athlete_id, role="inspector"
        )
        self.assertEqual(status, 200)
        self.assertEqual(counts["count"], 1)

        status, board = self._request("GET", "/api/dashboard")
        self.assertEqual(status, 200)
        self.assertEqual(len(board["athletes"]), 1)
        self.assertEqual(len(board["whereabouts"]), 1)
        self.assertEqual(len(board["dispatches"]), 1)
        self.assertEqual(board["dispatches"][0]["outcome"], "miss")


if __name__ == "__main__":
    unittest.main()
