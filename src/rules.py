import re
from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def parse_datetime(value):
    """Parse an ISO-8601 date or datetime; naive values are treated as UTC."""
    if value is None:
        raise ValidationError("datetime value is required")
    text = str(value).strip()
    if not text:
        raise ValidationError("datetime value is required")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError("invalid datetime: " + text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def to_iso(value):
    return parse_datetime(value).isoformat(timespec="seconds")


PERIOD_RE = re.compile(r"^(\d{4})-Q([1-4])$")


def period_bounds(period):
    match = PERIOD_RE.match(str(period or ""))
    if not match:
        raise ValidationError("period must look like 2026-Q3")
    year = int(match.group(1))
    quarter = int(match.group(2))
    start_month = 3 * (quarter - 1) + 1
    start = datetime(year, start_month, 1, tzinfo=timezone.utc)
    if quarter == 4:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, start_month + 3, 1, tzinfo=timezone.utc)
    return start, end


def window_end(starts_at):
    """A filed window always covers exactly 60 minutes."""
    return parse_datetime(starts_at) + timedelta(minutes=60)


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()


def _validate_athlete(actor, data, lookup):
    if len(data.get("discipline", "")) < 2:
        raise ValidationError("discipline is too short")


def _validate_sample(actor, data, lookup):
    athlete = _find_one(lookup, "athlete", "id", data.get("athlete_id"))
    if not athlete or athlete["status"] != "active":
        raise ValidationError("sample requires an active athlete")
    if not data.get("sample_code", "").strip():
        raise ValidationError("sample_code is required")


def _validate_case(actor, data, lookup):
    sample = _find_one(lookup, "sample", "id", data.get("sample_id"))
    if not sample or sample["status"] != "adverse":
        raise ValidationError("case requires an adverse sample")


def _require_active_athlete(lookup, athlete_id):
    athlete = _find_one(lookup, "athlete", "id", athlete_id)
    if not athlete:
        raise ValidationError("athlete_id is unknown: " + str(athlete_id))
    if athlete["status"] != "active":
        raise ValidationError("athlete is not available for testing: " + athlete["status"])
    return athlete


def _validate_whereabouts(actor, data, lookup):
    _require_active_athlete(lookup, data.get("athlete_id"))
    period_bounds(data.get("period"))
    if not str(data.get("location", "")).strip():
        raise ValidationError("location is required")
    starts_at = parse_datetime(data.get("window_starts_at"))
    start, end = period_bounds(data["period"])
    if not (start <= starts_at < end):
        raise ValidationError("60-minute window must fall inside period " + data["period"])
    return {
        "window_starts_at": to_iso(starts_at),
        "window_ends_at": window_end(starts_at).isoformat(timespec="seconds"),
    }


def _validate_dispatch(actor, data, lookup):
    athlete = _require_active_athlete(lookup, data.get("athlete_id"))
    period_bounds(data.get("period"))
    checked_at = parse_datetime(data.get("check_at"))
    # The effective version for the check date is resolved by the service and
    # handed in via the "effective" payload; missing location or an elapsed
    # window are the two hard rejects described by the business rules.
    effective = data.get("effective")
    if not isinstance(effective, dict):
        raise ValidationError("effective whereabouts version must be resolved first")
    if not str(effective.get("location", "")).strip():
        raise ValidationError("whereabouts version is missing a location")
    if checked_at >= parse_datetime(effective["window_ends_at"]):
        raise ValidationError("filed 60-minute window has already passed")
    return {
        "athlete_name": athlete["data"].get("name", ""),
        "check_at": to_iso(checked_at),
        "location": effective["location"],
        "window_starts_at": effective["window_starts_at"],
        "window_ends_at": effective["window_ends_at"],
        "whereabouts_id": effective["whereabouts_id"],
        "version_no": int(effective["version_no"]),
    }


def _validate_report_adverse(actor, entity, data, lookup):
    if entity["data"].get("result") != "adverse":
        raise ValidationError("only an adverse lab result can open a case")
    return {"confirmed_by": actor.user_id}


def _validate_case_decision(actor, entity, data, lookup):
    if data.get("decision") not in ("sanction", "no_sanction"):
        raise ValidationError("decision must be sanction or no_sanction")
    return {"decided_by": actor.user_id}


def _validate_whereabouts_amend(actor, entity, data, lookup):
    if entity["status"] != "active":
        raise InvalidTransition("quarter filing is closed and cannot be rewritten")
    merged = dict(entity["data"])
    merged.update({key: value for key, value in data.items() if value is not None})
    return _validate_whereabouts(actor, merged, lookup)


def _validate_dispatch_execute(actor, entity, data, lookup):
    outcome = data.get("outcome")
    if outcome not in ("hit", "miss"):
        raise ValidationError("outcome must be hit or miss")
    checked_at = data.get("checked_at")
    return {
        "outcome": outcome,
        "checked_at": to_iso(checked_at) if checked_at else None,
        "executed_by": actor.user_id,
    }


def _validate_dispatch_cancel(actor, entity, data, lookup):
    if not str(data.get("reason", "")).strip():
        raise ValidationError("cancel reason is required")
    return {"cancelled_by": actor.user_id}


CUSTOM_CREATE = {
    'athlete': _validate_athlete,
    'sample': _validate_sample,
    'case': _validate_case,
    'whereabouts': _validate_whereabouts,
    'dispatch': _validate_dispatch,
}
CUSTOM_TRANSITIONS = {
    ('sample', 'report_adverse'): _validate_report_adverse,
    ('case', 'decide'): _validate_case_decision,
    ('case', 'resolve_appeal'): _validate_case_decision,
    ('whereabouts', 'amend'): _validate_whereabouts_amend,
    ('dispatch', 'execute'): _validate_dispatch_execute,
    ('dispatch', 'cancel'): _validate_dispatch_cancel,
}


class RuleEngine:
    ALIASES = {
        'athletes': 'athlete',
        'samples': 'sample',
        'cases': 'case',
        'whereabouts': 'whereabouts',
        'dispatches': 'dispatch',
        'reviews': 'review',
    }
    INITIAL_STATUS = {
        'athlete': 'active',
        'sample': 'scheduled',
        'case': 'open',
        'whereabouts': 'active',
        'dispatch': 'planned',
        'review': 'pending_review',
    }
    TRANSITIONS = {
        'athlete': {'retire': (('active',), 'retired')},
        'sample': {'collect': (('scheduled',), 'collected'), 'seal': (('collected',), 'sealed'), 'ship': (('sealed',), 'in_transit'), 'receive': (('in_transit',), 'received'), 'analyze': (('received',), 'analyzed'), 'report_adverse': (('analyzed',), 'adverse'), 'clear': (('analyzed',), 'cleared')},
        'case': {'provisional_suspend': (('open',), 'suspended'), 'schedule_hearing': (('suspended',), 'hearing'), 'decide': (('hearing',), 'closed'), 'appeal': (('closed',), 'appeal'), 'resolve_appeal': (('appeal',), 'closed')},
        'whereabouts': {'amend': (('active',), 'active'), 'close': (('active',), 'closed')},
        'dispatch': {'execute': (('planned',), 'executed'), 'cancel': (('planned',), 'cancelled')},
        'review': {'close_review': (('pending_review',), 'closed')},
    }
    CREATE_REQUIRED = {
        'athlete': ('name', 'discipline'),
        'sample': ('athlete_id', 'sample_code', 'event'),
        'case': ('athlete_id', 'sample_id', 'alleged_rule'),
        'whereabouts': ('athlete_id', 'period', 'location', 'window_starts_at'),
        'dispatch': ('athlete_id', 'period', 'check_at'),
        'review': ('athlete_id', 'reason', 'trigger_dispatch_id'),
    }
    ACTION_REQUIRED = {
        ('sample', 'collect'): ('collected_at',),
        ('sample', 'seal'): ('seal_id',),
        ('sample', 'ship'): ('carrier',),
        ('sample', 'receive'): ('lab_id',),
        ('sample', 'analyze'): ('result',),
        ('sample', 'clear'): ('reason',),
        ('case', 'provisional_suspend'): ('reason',),
        ('case', 'schedule_hearing'): ('hearing_at',),
        ('case', 'decide'): ('decision',),
        ('case', 'appeal'): ('grounds',),
        ('case', 'resolve_appeal'): ('decision',),
        ('whereabouts', 'amend'): (),
        ('dispatch', 'execute'): ('outcome',),
        ('dispatch', 'cancel'): ('reason',),
        ('review', 'close_review'): ('decision',),
    }
    CREATE_ROLES = {
        'athlete': ('admin', 'panel'),
        'sample': ('admin', 'inspector'),
        'case': ('admin', 'panel'),
        'whereabouts': ('admin', 'athlete'),
        'dispatch': ('admin', 'inspector'),
        'review': ('admin', 'panel'),
    }
    ROLE_ACTIONS = {
        'retire': ('admin', 'panel'),
        'collect': ('admin', 'inspector'),
        'seal': ('admin', 'inspector'),
        'ship': ('admin', 'inspector'),
        'receive': ('admin', 'lab'),
        'analyze': ('admin', 'lab'),
        'report_adverse': ('admin', 'lab'),
        'clear': ('admin', 'lab'),
        'provisional_suspend': ('admin', 'panel'),
        'schedule_hearing': ('admin', 'panel'),
        'decide': ('admin', 'panel'),
        'appeal': ('admin', 'panel'),
        'resolve_appeal': ('admin', 'panel'),
        'amend': ('admin', 'athlete'),
        'close': ('admin', 'panel'),
        'execute': ('admin', 'inspector'),
        'cancel': ('admin', 'inspector'),
        'close_review': ('admin', 'panel'),
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        normalized = custom(actor, data, lookup) if custom else {}
        payload = dict(data)
        if normalized:
            payload.update(normalized)
        return payload

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch
