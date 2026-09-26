from datetime import timedelta
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError
from .repository import utcnow
from .rules import RuleEngine, parse_datetime, period_bounds, to_iso

TRAILING_DAYS = 365


class DomainService:
    def __init__(self, repository, rules=None, clock=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)
        self.clock = clock

    def _now(self):
        return parse_datetime(self.clock() if self.clock else utcnow())

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    # ------------------------------------------------------------------ create

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        if kind == "whereabouts":
            entity = self._create_whereabouts(actor, payload)
        elif kind == "dispatch":
            entity = self._create_dispatch(actor, payload)
        else:
            entity = self._create_generic(actor, kind, payload)
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity["id"])
        return entity

    def _create_generic(self, actor, kind, payload):
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        return entity

    def _filing_for(self, athlete_id, period):
        period_bounds(period)
        for filing in self._lookup("whereabouts", "athlete_id", athlete_id):
            if filing["data"].get("period") == period:
                return filing
        return None

    def _create_whereabouts(self, actor, payload):
        normalized = self.rules.validate_create(actor, "whereabouts", payload, self._lookup)
        normalized.pop("id", None)
        if self._filing_for(normalized["athlete_id"], normalized["period"]):
            raise ConflictError(
                "whereabouts filing already exists for %s %s"
                % (normalized["athlete_id"], normalized["period"])
            )
        entity_id = str(uuid4())
        entity = self.repository.create_entity(
            entity_id, "whereabouts", "active", normalized, actor.user_id
        )
        self.repository.append_whereabouts_version(
            entity_id,
            1,
            normalized,
            actor.user_id,
            created_at=self._now().isoformat(timespec="seconds"),
        )
        self.audit.record(
            entity_id, actor, "file", None, "active",
            {"kind": "whereabouts", "period": normalized["period"], "version_no": 1},
        )
        return entity

    def effective_whereabouts(self, athlete_id, period, check_at):
        """Version effective for the check date: latest submission made
        before the check instant. Closed quarters stay readable."""
        check_instant = parse_datetime(check_at)
        period_bounds(period)
        filing = self._filing_for(athlete_id, period)
        if not filing:
            raise ValidationError(
                "no whereabouts filing for athlete %s in %s" % (athlete_id, period)
            )
        effective = None
        for version in self.repository.list_whereabouts_versions(filing["id"]):
            if parse_datetime(version["created_at"]) <= check_instant:
                effective = version
        if not effective:
            raise ValidationError(
                "no whereabouts version was effective on the check date in %s" % period
            )
        result = dict(effective)
        result["filing_status"] = filing["status"]
        result["period"] = period
        return result

    def _create_dispatch(self, actor, payload):
        self.rules._ensure_role(actor, self.rules.CREATE_ROLES["dispatch"])
        self.rules._require(payload, ("athlete_id", "period", "check_at"))
        check_iso = to_iso(payload["check_at"])
        effective = self.effective_whereabouts(
            payload["athlete_id"], payload["period"], check_iso
        )
        request = dict(payload)
        request["check_at"] = check_iso
        request["effective"] = {
            "whereabouts_id": effective["whereabouts_id"],
            "version_no": effective["version_no"],
            "location": effective["location"],
            "window_starts_at": effective["window_starts_at"],
            "window_ends_at": effective["window_ends_at"],
        }
        normalized = self.rules.validate_create(actor, "dispatch", request, self._lookup)
        normalized.pop("effective", None)
        normalized["outcome"] = None
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        entity = self.repository.create_entity(
            entity_id, "dispatch", "planned", normalized, actor.user_id
        )
        self.audit.record(
            entity_id, actor, "create", None, "planned",
            {
                "kind": "dispatch",
                "period": normalized["period"],
                "whereabouts_id": normalized["whereabouts_id"],
                "version_no": normalized["version_no"],
            },
        )
        return entity

    # -------------------------------------------------------------- transitions

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        kind = self.rules.normalize_kind(entity["kind"])
        patch = dict(data or {})
        next_status, validated = self.rules.validate_transition(
            actor, entity, action, patch, self._lookup
        )
        merged = dict(entity["data"])
        merged.update(validated)

        review = None
        if kind == "dispatch" and action == "execute":
            if not merged.get("checked_at"):
                merged["checked_at"] = self._now().isoformat(timespec="seconds")
            if merged["outcome"] == "miss":
                review = self._open_review_if_needed(entity_id, entity, merged, actor)

        updated = self.repository.update_entity(entity_id, expected, next_status, merged)

        if kind == "whereabouts" and action == "amend":
            self.repository.append_whereabouts_version(
                entity_id,
                updated["version"],
                updated["data"],
                actor.user_id,
                created_at=self._now().isoformat(timespec="seconds"),
            )

        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": validated, "review_id": review["id"] if review else None},
        )
        if review:
            self.audit.record(
                review["id"], actor, "auto_open", None, review["status"],
                {"kind": "review", "trigger_dispatch_id": entity_id},
            )
        return updated

    def _athlete_misses(self, athlete_id, at_instant):
        window_start = at_instant - timedelta(days=TRAILING_DAYS)
        misses = []
        for dispatch in self._lookup("dispatch", "athlete_id", athlete_id):
            if dispatch["status"] != "executed":
                continue
            if dispatch["data"].get("outcome") != "miss":
                continue
            checked_at = dispatch["data"].get("checked_at")
            if not checked_at:
                continue
            checked = parse_datetime(checked_at)
            if window_start <= checked <= at_instant:
                misses.append(dispatch)
        misses.sort(key=lambda item: item["data"]["checked_at"])
        return misses, window_start

    def _open_review_if_needed(self, dispatch_id, dispatch_entity, merged, actor):
        """Third missed test in a trailing 12-month window opens one
        pending-review case. Cancelled dispatches were never executed and
        therefore never count."""
        athlete_id = dispatch_entity["data"]["athlete_id"]
        checked_at = parse_datetime(merged["checked_at"])
        prior, window_start = self._athlete_misses(athlete_id, checked_at)
        if len(prior) + 1 < 3:
            return None
        for review in self._lookup("review", "athlete_id", athlete_id):
            if review["status"] == "pending_review":
                merged["review_id"] = review["id"]
                return None
        athlete = self.repository.get_entity(athlete_id)
        athlete_name = athlete["data"].get("name", "") if athlete else ""
        payload = {
            "athlete_id": athlete_id,
            "athlete_name": athlete_name,
            "alleged_rule": "three missed tests / filing failures within 12 months",
            "reason": "third missed test within trailing 12 months",
            "trigger_dispatch_id": dispatch_id,
            "miss_count": len(prior) + 1,
            "window_start": window_start.isoformat(timespec="seconds"),
            "window_end": checked_at.isoformat(timespec="seconds"),
        }
        review = self.repository.create_entity(
            str(uuid4()), "review", "pending_review", payload, actor.user_id
        )
        merged["review_id"] = review["id"]
        return review

    # ------------------------------------------------------------------ reads

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def whereabouts_versions(self, whereabouts_id):
        if not self.repository.get_entity(whereabouts_id):
            raise NotFoundError("entity not found: " + whereabouts_id)
        return self.repository.list_whereabouts_versions(whereabouts_id)

    def miss_count(self, athlete_id, at=None):
        at_instant = parse_datetime(at) if at else self._now()
        misses, window_start = self._athlete_misses(athlete_id, at_instant)
        return {
            "athlete_id": athlete_id,
            "window_days": TRAILING_DAYS,
            "window_start": window_start.isoformat(timespec="seconds"),
            "window_end": at_instant.isoformat(timespec="seconds"),
            "count": len(misses),
            "misses": [
                {
                    "dispatch_id": item["id"],
                    "checked_at": item["data"].get("checked_at"),
                }
                for item in misses
            ],
        }

    def dashboard(self):
        athletes = self.repository.list_entities(kind="athlete")
        dispatches = self.repository.list_entities(kind="dispatch")
        filings = self.repository.list_entities(kind="whereabouts")
        reviews = self.repository.list_entities(kind="review")
        now = self._now()

        filings_by_athlete = {}
        filing_rows = []
        for filing in filings:
            filings_by_athlete.setdefault(filing["data"].get("athlete_id"), []).append(filing)
            versions = self.repository.list_whereabouts_versions(filing["id"])
            current = versions[-1] if versions else None
            filing_rows.append(
                {
                    "whereabouts_id": filing["id"],
                    "athlete_id": filing["data"].get("athlete_id"),
                    "period": filing["data"].get("period"),
                    "status": filing["status"],
                    "version": filing["version"],
                    "version_count": len(versions),
                    "current_location": current["location"] if current else None,
                    "current_window": [
                        current["window_starts_at"],
                        current["window_ends_at"],
                    ] if current else None,
                }
            )
        filing_rows.sort(key=lambda row: (row["athlete_id"] or "", row["period"] or ""))

        dispatch_rows = []
        dispatches_by_athlete = {}
        for dispatch in dispatches:
            data = dispatch["data"]
            row = {
                "dispatch_id": dispatch["id"],
                "athlete_id": data.get("athlete_id"),
                "period": data.get("period"),
                "status": dispatch["status"],
                "outcome": data.get("outcome"),
                "check_at": data.get("check_at"),
                "location": data.get("location"),
                "version_no": data.get("version_no"),
                "whereabouts_id": data.get("whereabouts_id"),
                "review_id": data.get("review_id"),
            }
            dispatch_rows.append(row)
            dispatches_by_athlete.setdefault(data.get("athlete_id"), []).append(dispatch)
        dispatch_rows.sort(key=lambda row: row["check_at"] or "")

        athlete_rows = []
        for athlete in athletes:
            athlete_dispatches = dispatches_by_athlete.get(athlete["id"], [])
            miss_total = sum(
                1 for item in athlete_dispatches
                if item["status"] == "executed" and item["data"].get("outcome") == "miss"
            )
            trailing, _ = self._athlete_misses(athlete["id"], now)
            pending = [
                review["id"]
                for review in reviews
                if review["data"].get("athlete_id") == athlete["id"]
                and review["status"] == "pending_review"
            ]
            athlete_rows.append(
                {
                    "athlete_id": athlete["id"],
                    "name": athlete["data"].get("name"),
                    "status": athlete["status"],
                    "dispatch_total": len(athlete_dispatches),
                    "miss_total": miss_total,
                    "misses_trailing_12m": len(trailing),
                    "pending_reviews": pending,
                    "filings": [
                        {
                            "whereabouts_id": filing["id"],
                            "period": filing["data"].get("period"),
                            "status": filing["status"],
                        }
                        for filing in sorted(
                            filings_by_athlete.get(athlete["id"], []),
                            key=lambda item: item["data"].get("period", ""),
                        )
                    ],
                }
            )

        return {
            "generated_at": now.isoformat(timespec="seconds"),
            "athletes": athlete_rows,
            "whereabouts": filing_rows,
            "dispatches": dispatch_rows,
            "reviews": [
                {
                    "review_id": review["id"],
                    "athlete_id": review["data"].get("athlete_id"),
                    "status": review["status"],
                    "reason": review["data"].get("reason"),
                    "trigger_dispatch_id": review["data"].get("trigger_dispatch_id"),
                    "miss_count": review["data"].get("miss_count"),
                }
                for review in sorted(reviews, key=lambda item: item["created_at"])
            ],
        }

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
