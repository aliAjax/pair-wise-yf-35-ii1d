from datetime import datetime, time, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)

SLOT_MINUTES = 60
MISS_WINDOW_DAYS = 365
MISS_CASE_THRESHOLD = 3
CASE_REVIEW_STATUSES = ("open", "suspended", "hearing", "appeal")


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_date(value, field):
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except (TypeError, ValueError):
        raise ValidationError("invalid date for %s: %s" % (field, value))


def _parse_time(value, field):
    try:
        return datetime.strptime(str(value), "%H:%M").time()
    except (TypeError, ValueError):
        raise ValidationError("invalid time for %s: %s" % (field, value))


def quarter_of(date_value):
    day = _parse_date(date_value, "date")
    return "%04d-Q%d" % (day.year, (day.month - 1) // 3 + 1)


def current_quarter():
    today = datetime.now(timezone.utc).date().isoformat()
    return quarter_of(today)


def _version_snapshot(data):
    return {
        "revision": int(data.get("revision", 1)),
        "filed_at": data.get("filed_at"),
        "slot_date": data.get("slot_date"),
        "slot_start": data.get("slot_start"),
        "location": data.get("location"),
    }


def effective_version(filing, check_date):
    """Resolve the whereabouts version effective on check_date, or None."""
    day = str(check_date)[:10]
    versions = list(filing["data"].get("versions", []))
    versions.append(_version_snapshot(filing["data"]))
    candidates = [
        version
        for version in versions
        if version.get("filed_at") and str(version["filed_at"])[:10] <= day
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda version: (str(version["filed_at"]), int(version.get("revision", 0))),
    )


def _slot_end(version):
    day = _parse_date(version.get("slot_date"), "slot_date")
    start = _parse_time(version.get("slot_start"), "slot_start")
    return datetime.combine(day, start) + timedelta(minutes=SLOT_MINUTES)


def miss_count_12m(dispatches, on_date):
    """Count missed dispatches inside the 12-month window ending on_date."""
    end = _parse_date(on_date, "on_date").toordinal()
    start = end - MISS_WINDOW_DAYS
    count = 0
    for dispatch in dispatches:
        if dispatch.get("status") != "missed":
            continue
        missed = dispatch["data"].get("missed_at") or dispatch["data"].get("check_date")
        if not missed:
            continue
        day = _parse_date(missed, "missed_at").toordinal()
        if start <= day <= end:
            count += 1
    return count


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
    sample_id = data.get("sample_id")
    if sample_id:
        sample = _find_one(lookup, "sample", "id", sample_id)
        if not sample or sample["status"] != "adverse":
            raise ValidationError("case requires an adverse sample")
    elif not data.get("source"):
        raise ValidationError("case requires an adverse sample or a source")


def _validate_whereabouts(actor, data, lookup):
    athlete = _find_one(lookup, "athlete", "id", data.get("athlete_id"))
    if not athlete or athlete["status"] != "active":
        raise ValidationError("whereabouts requires an active athlete")
    quarter = str(data.get("quarter"))
    if quarter != current_quarter():
        raise ValidationError("whereabouts can only be filed for the current quarter")
    if quarter_of(data.get("slot_date")) != quarter:
        raise ValidationError("slot_date must fall inside the filing quarter")
    _parse_time(data.get("slot_start"), "slot_start")
    if not str(data.get("location") or "").strip():
        raise ValidationError("location is required")
    existing = _find_all(lookup, "whereabouts", "athlete_id", data.get("athlete_id"))
    if any(item["data"].get("quarter") == quarter for item in existing):
        raise ValidationError("whereabouts already filed for this quarter; use amend")
    return {
        "filed_at": _now_iso(),
        "revision": 1,
        "versions": [],
        "slot_minutes": SLOT_MINUTES,
    }


def _validate_whereabouts_amend(actor, entity, data, lookup):
    quarter = entity["data"].get("quarter")
    if quarter != current_quarter():
        raise ValidationError("quarter %s is closed; cannot amend" % quarter)
    if quarter_of(data.get("slot_date")) != quarter:
        raise ValidationError("slot_date must fall inside the filing quarter")
    _parse_time(data.get("slot_start"), "slot_start")
    if not str(data.get("location") or "").strip():
        raise ValidationError("location is required")
    versions = list(entity["data"].get("versions", []))
    versions.append(_version_snapshot(entity["data"]))
    return {
        "versions": versions,
        "revision": int(entity["data"].get("revision", 1)) + 1,
        "filed_at": _now_iso(),
    }


def _validate_dispatch(actor, data, lookup):
    athlete = _find_one(lookup, "athlete", "id", data.get("athlete_id"))
    if not athlete or athlete["status"] != "active":
        raise ValidationError("dispatch requires an active athlete")
    check_date = str(data.get("check_date"))[:10]
    quarter = quarter_of(check_date)
    filings = [
        item
        for item in _find_all(lookup, "whereabouts", "athlete_id", data.get("athlete_id"))
        if item["data"].get("quarter") == quarter
    ]
    if not filings:
        raise ValidationError("no whereabouts filing covers the check date")
    version = effective_version(filings[0], check_date)
    if version is None:
        raise ValidationError("no effective whereabouts version on the check date")
    if not str(version.get("location") or "").strip():
        raise ValidationError("whereabouts location is missing")
    check_day = _parse_date(check_date, "check_date")
    check_time = data.get("check_time")
    if check_time:
        moment = datetime.combine(check_day, _parse_time(check_time, "check_time"))
    else:
        moment = datetime.combine(check_day, time(0, 0, 0))
    if _slot_end(version) < moment:
        raise ValidationError("whereabouts slot has already passed")
    return {
        "whereabouts_id": filings[0]["id"],
        "whereabouts_revision": version.get("revision"),
        "quarter": quarter,
        "location": version.get("location"),
        "slot_date": version.get("slot_date"),
        "slot_start": version.get("slot_start"),
    }


def _validate_dispatch_execute(actor, entity, data, lookup):
    if data.get("executed_at"):
        return {}
    return {"executed_at": _now_iso()}


def _validate_dispatch_miss(actor, entity, data, lookup):
    missed_at = str(data.get("missed_at") or entity["data"].get("check_date") or "")[:10]
    _parse_date(missed_at, "missed_at")
    return {"missed_at": missed_at}


def _validate_report_adverse(actor, entity, data, lookup):
    if entity["data"].get("result") != "adverse":
        raise ValidationError("only an adverse lab result can open a case")
    return {"confirmed_by": actor.user_id}


def _validate_case_decision(actor, entity, data, lookup):
    if data.get("decision") not in ("sanction", "no_sanction"):
        raise ValidationError("decision must be sanction or no_sanction")
    return {"decided_by": actor.user_id}


CUSTOM_CREATE = {'athlete': _validate_athlete, 'sample': _validate_sample, 'case': _validate_case, 'whereabouts': _validate_whereabouts, 'dispatch': _validate_dispatch}
CUSTOM_TRANSITIONS = {('sample', 'report_adverse'): _validate_report_adverse, ('case', 'decide'): _validate_case_decision, ('case', 'resolve_appeal'): _validate_case_decision, ('whereabouts', 'amend'): _validate_whereabouts_amend, ('dispatch', 'execute'): _validate_dispatch_execute, ('dispatch', 'register_miss'): _validate_dispatch_miss}


class RuleEngine:
    ALIASES = {'athletes': 'athlete', 'samples': 'sample', 'cases': 'case', 'dispatches': 'dispatch'}
    INITIAL_STATUS = {'athlete': 'active', 'sample': 'scheduled', 'case': 'open', 'whereabouts': 'filed', 'dispatch': 'dispatched'}
    TRANSITIONS = {'athlete': {'retire': (('active',), 'retired')}, 'sample': {'collect': (('scheduled',), 'collected'), 'seal': (('collected',), 'sealed'), 'ship': (('sealed',), 'in_transit'), 'receive': (('in_transit',), 'received'), 'analyze': (('received',), 'analyzed'), 'report_adverse': (('analyzed',), 'adverse'), 'clear': (('analyzed',), 'cleared')}, 'case': {'provisional_suspend': (('open',), 'suspended'), 'schedule_hearing': (('suspended',), 'hearing'), 'decide': (('hearing',), 'closed'), 'appeal': (('closed',), 'appeal'), 'resolve_appeal': (('appeal',), 'closed')}, 'whereabouts': {'amend': (('filed',), 'filed')}, 'dispatch': {'execute': (('dispatched',), 'executed'), 'register_miss': (('executed',), 'missed'), 'cancel': (('dispatched',), 'cancelled')}}
    CREATE_REQUIRED = {'athlete': ('name', 'discipline'), 'sample': ('athlete_id', 'sample_code', 'event'), 'case': ('athlete_id', 'alleged_rule'), 'whereabouts': ('athlete_id', 'quarter', 'slot_date', 'slot_start', 'location'), 'dispatch': ('athlete_id', 'check_date')}
    ACTION_REQUIRED = {('sample', 'collect'): ('collected_at',), ('sample', 'seal'): ('seal_id',), ('sample', 'ship'): ('carrier',), ('sample', 'receive'): ('lab_id',), ('sample', 'analyze'): ('result',), ('sample', 'clear'): ('reason',), ('case', 'provisional_suspend'): ('reason',), ('case', 'schedule_hearing'): ('hearing_at',), ('case', 'decide'): ('decision',), ('case', 'appeal'): ('grounds',), ('case', 'resolve_appeal'): ('decision',), ('whereabouts', 'amend'): ('slot_date', 'slot_start', 'location')}
    CREATE_ROLES = {'athlete': ('admin', 'panel'), 'sample': ('admin', 'inspector'), 'case': ('admin', 'panel'), 'whereabouts': ('admin',), 'dispatch': ('admin', 'inspector')}
    ROLE_ACTIONS = {'retire': ('admin', 'panel'), 'collect': ('admin', 'inspector'), 'seal': ('admin', 'inspector'), 'ship': ('admin', 'inspector'), 'receive': ('admin', 'lab'), 'analyze': ('admin', 'lab'), 'report_adverse': ('admin', 'lab'), 'clear': ('admin', 'lab'), 'provisional_suspend': ('admin', 'panel'), 'schedule_hearing': ('admin', 'panel'), 'decide': ('admin', 'panel'), 'appeal': ('admin', 'panel'), 'resolve_appeal': ('admin', 'panel'), ('whereabouts', 'amend'): ('admin',), ('dispatch', 'execute'): ('admin', 'inspector'), ('dispatch', 'register_miss'): ('admin', 'inspector'), ('dispatch', 'cancel'): ('admin', 'inspector')}

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
        extra = custom(actor, data, lookup) if custom else None
        payload = dict(data)
        if extra:
            payload.update(extra)
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


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _find_all(lookup, kind, field, value):
    if lookup is None:
        return []
    return lookup(kind, field, value) or []


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
