from datetime import datetime, timezone
from uuid import uuid4

from .audit import AuditTrail
from .domain import Actor, ConflictError, NotFoundError
from .rules import (
    CASE_REVIEW_STATUSES,
    MISS_CASE_THRESHOLD,
    RuleEngine,
    miss_count_12m,
)


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        payload = self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        self._after_transition(updated, action)
        return updated

    def _after_transition(self, entity, action):
        if entity["kind"] == "dispatch" and action == "register_miss":
            self._open_case_on_third_miss(entity)

    def _open_case_on_third_miss(self, dispatch):
        athlete_id = dispatch["data"].get("athlete_id")
        dispatches = self.repository.find_entities("dispatch", "athlete_id", athlete_id)
        count = miss_count_12m(dispatches, dispatch["data"].get("missed_at"))
        if count < MISS_CASE_THRESHOLD:
            return None
        pending = [
            case
            for case in self.repository.find_entities("case", "athlete_id", athlete_id)
            if case["data"].get("source") == "whereabouts"
            and case["status"] in CASE_REVIEW_STATUSES
        ]
        if pending:
            return None
        system = Actor("system", "admin")
        return self.create(
            system,
            "case",
            {
                "athlete_id": athlete_id,
                "alleged_rule": "whereabouts-missed-tests",
                "source": "whereabouts",
                "miss_count": count,
                "last_missed_at": dispatch["data"].get("missed_at"),
            },
        )

    def whereabouts_summary(self, athlete_id):
        athlete = self.get(athlete_id)
        filings = self.repository.find_entities("whereabouts", "athlete_id", athlete_id)
        dispatches = self.repository.find_entities("dispatch", "athlete_id", athlete_id)
        today = datetime.now(timezone.utc).date().isoformat()
        return {
            "athlete": athlete,
            "filings": filings,
            "dispatches": dispatches,
            "miss_count_12m": miss_count_12m(dispatches, today),
            "miss_count_total": sum(
                1 for item in dispatches if item["status"] == "missed"
            ),
        }

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
