from __future__ import annotations

import hashlib
import json
from datetime import datetime, time
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PROFILE_KEYS = {
    "enabled",
    "camera_scope",
    "time_scope",
    "roi",
    "object_interests",
    "screening_policy",
    "candidate_policy",
    "semantic_intent",
    "prompt_schema",
    "preferred_model_class",
}


class AnalysisProfileError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _canonical_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonical_json(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical_json(item) for item in value]
    return value


def normalized_profile_payload(*, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(payload) - PROFILE_KEYS
    if unknown:
        raise AnalysisProfileError("ANALYSIS_PROFILE_FIELD_UNKNOWN")
    normalized = {
        "name": name,
        "enabled": bool(payload.get("enabled", True)),
        "camera_scope": payload.get("camera_scope"),
        "time_scope": payload.get("time_scope"),
        "roi": payload.get("roi"),
        "object_interests": payload.get("object_interests"),
        "screening_policy": payload.get("screening_policy"),
        "candidate_policy": payload.get("candidate_policy"),
        "semantic_intent": payload.get("semantic_intent"),
        "prompt_schema": payload.get("prompt_schema"),
        "preferred_model_class": payload.get("preferred_model_class"),
    }
    return _canonical_json(normalized)


def profile_snapshot_hash_from_input(*, name: str, payload: Mapping[str, Any]) -> str:
    body = json.dumps(
        normalized_profile_payload(name=name, payload=payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _json_text(value: str | None) -> Any:
    if value in (None, ""):
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise AnalysisProfileError("ANALYSIS_PROFILE_JSON_INVALID") from exc


def stored_profile_payload(profile: object) -> dict[str, Any]:
    return {
        "enabled": bool(getattr(profile, "enabled")),
        "camera_scope": _json_text(getattr(profile, "camera_scope_json", None)),
        "time_scope": _json_text(getattr(profile, "time_scope_json", None)),
        "roi": _json_text(getattr(profile, "roi_json", None)),
        "object_interests": _json_text(getattr(profile, "object_interests_json", None)),
        "screening_policy": _json_text(getattr(profile, "screening_policy_json", None)),
        "candidate_policy": _json_text(getattr(profile, "candidate_policy_json", None)),
        "semantic_intent": getattr(profile, "semantic_intent", None),
        "prompt_schema": _json_text(getattr(profile, "prompt_schema_json", None)),
        "preferred_model_class": getattr(profile, "preferred_model_class", None),
    }


def stored_profile_snapshot_hash(profile: object) -> str:
    return profile_snapshot_hash_from_input(
        name=str(getattr(profile, "name")),
        payload=stored_profile_payload(profile),
    )


def validate_profile_snapshot(profile: object) -> None:
    if str(getattr(profile, "snapshot_hash")) != stored_profile_snapshot_hash(profile):
        raise AnalysisProfileError("ANALYSIS_PROFILE_SNAPSHOT_MISMATCH")


def _parse_hhmm(value: str) -> time:
    try:
        hour_raw, minute_raw = value.split(":", 1)
        hour, minute = int(hour_raw), int(minute_raw)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
        return time(hour=hour, minute=minute)
    except (AttributeError, TypeError, ValueError) as exc:
        raise AnalysisProfileError("ANALYSIS_PROFILE_TIME_SCOPE_INVALID") from exc


def _local_time(scope: Mapping[str, Any], at: datetime) -> datetime:
    if at.tzinfo is None:
        raise AnalysisProfileError("ANALYSIS_PROFILE_TIME_REQUIRES_TZ")
    timezone_name = scope.get("timezone")
    if not timezone_name:
        return at
    try:
        return at.astimezone(ZoneInfo(str(timezone_name)))
    except ZoneInfoNotFoundError as exc:
        raise AnalysisProfileError("ANALYSIS_PROFILE_TIMEZONE_INVALID") from exc


def _time_matches(scope: Mapping[str, Any] | None, at: datetime) -> bool:
    if not scope:
        return True
    local = _local_time(scope, at)
    days = scope.get("days")
    if days is not None and local.weekday() not in {int(day) for day in days}:
        return False

    start_raw = scope.get("start")
    end_raw = scope.get("end")
    if start_raw is None and end_raw is None:
        return True
    if start_raw is None or end_raw is None:
        raise AnalysisProfileError("ANALYSIS_PROFILE_TIME_SCOPE_INVALID")

    start = _parse_hhmm(str(start_raw))
    end = _parse_hhmm(str(end_raw))
    current = local.timetz().replace(tzinfo=None)
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


def profile_matches(profile: object, *, camera_id: str, at: datetime) -> bool:
    if not bool(getattr(profile, "enabled", False)):
        return False

    camera_scope = _json_text(getattr(profile, "camera_scope_json", None))
    if camera_scope:
        if not isinstance(camera_scope, dict):
            raise AnalysisProfileError("ANALYSIS_PROFILE_CAMERA_SCOPE_INVALID")
        ids = camera_scope.get("camera_ids")
        if ids is not None and camera_id not in {str(item) for item in ids}:
            return False

    time_scope = _json_text(getattr(profile, "time_scope_json", None))
    if time_scope is not None and not isinstance(time_scope, dict):
        raise AnalysisProfileError("ANALYSIS_PROFILE_TIME_SCOPE_INVALID")
    return _time_matches(time_scope, at)


def select_current_profiles(
    profiles: Iterable[object],
    *,
    camera_id: str,
    at: datetime,
) -> list[object]:
    latest: dict[str, object] = {}
    for profile in profiles:
        if not profile_matches(profile, camera_id=camera_id, at=at):
            continue
        profile_id = str(getattr(profile, "id"))
        revision = int(getattr(profile, "revision"))
        prior = latest.get(profile_id)
        if prior is None or revision > int(getattr(prior, "revision")):
            latest[profile_id] = profile
    return [latest[key] for key in sorted(latest)]
