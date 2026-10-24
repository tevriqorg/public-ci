from __future__ import annotations

import hashlib
import re
import uuid
from urllib.parse import urlparse
from typing import Any

from .db import Store, utcnow

SKILL_STATES = {"candidate", "validated", "stable", "degraded"}
ALLOWED_CONFIG = {"resolver", "engine", "header_strategy", "cookie_strategy", "referer_strategy", "auth_ref", "fallback_strategy", "retry_policy"}


def fingerprint(job: dict[str, Any], resolver: str = "unknown", error: Exception | str = "unknown") -> dict[str, str]:
    host = (urlparse(job.get("source") or "").hostname or "local").lower()
    error_class = type(error).__name__ if isinstance(error, Exception) else "TransferError"
    # Use only a safe, normalized classification. In particular never fingerprint the raw message,
    # which can contain signed URLs, cookies, or downloader command output.
    signature = re.sub(r"\d+", "#", str(error_class))[:80]
    components = [host, str(job.get("source_type", "unknown")), resolver, str(job.get("engine", "unknown")), error_class, signature]
    return {"fingerprint": hashlib.sha256("|".join(components).encode()).hexdigest()[:24], "domain": host,
            "source_type": str(job.get("source_type", "unknown")), "resolver": resolver,
            "engine": str(job.get("engine", "unknown")), "error_class": error_class, "signature": signature}


def _safe_config(config: dict[str, Any]) -> dict[str, Any]:
    if set(config) - ALLOWED_CONFIG:
        raise ValueError("unsupported skill configuration fields")
    out = dict(config)
    if out.get("auth_ref") and not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", out["auth_ref"]):
        raise ValueError("invalid auth_ref")
    allowed = {
        "resolver": {"auto", "direct", "twitter", "manifest", "webpage-manifest"},
        "engine": {"auto", "aria2", "yt-dlp", "gallery-dl", "nm3u8dl"},
        "fallback_strategy": {"browser", "none"},
    }
    for key, values in allowed.items():
        if key in out and out[key] not in values:
            raise ValueError(f"invalid {key}")
    safe_strategies = {
        "header_strategy": {"none", "standard", "auth_ref", "source_origin"},
        "cookie_strategy": {"none", "auth_ref"},
        "referer_strategy": {"none", "source", "origin"},
        "retry_policy": {"default", "single", "exponential"},
    }
    for key, accepted in safe_strategies.items():
        if key in out and out[key] not in accepted:
            raise ValueError(f"invalid {key}")
    return out


class SkillRegistry:
    def __init__(self, store: Store):
        self.store = store

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("name", "")).strip()
        if not name or len(name) > 120:
            raise ValueError("skill name must be 1-120 characters")
        rules = payload.get("rules") or {}
        if not isinstance(rules, dict) or set(rules) - {"domain", "url_prefix", "source_type"}:
            raise ValueError("invalid skill match rules")
        for value in rules.values():
            if not isinstance(value, str) or len(value) > 300:
                raise ValueError("invalid skill match rule")
        if rules.get("domain"):
            rules["domain"] = rules["domain"].lower().lstrip(".")
            if not re.fullmatch(r"[a-z0-9.-]{1,253}", rules["domain"]):
                raise ValueError("invalid skill domain")
        if rules.get("url_prefix"):
            from .security import validate_public_http_url
            from urllib.parse import urlsplit
            validate_public_http_url(rules["url_prefix"])
            parsed = urlsplit(rules["url_prefix"])
            if parsed.query or parsed.username or parsed.password:
                raise ValueError("skill URL prefix cannot contain credentials or query parameters")
        config = _safe_config(payload.get("config") or {})
        skill = {"id": str(payload.get("id") or uuid.uuid4().hex), "name": name,
                 "rules_json": __import__("json").dumps(rules), "config_json": __import__("json").dumps(config),
                 "state": "candidate", "failure_count": 0, "success_count": 0, "version": 1,
                 "validation_json": "[]", "last_verified": None}
        result = self.store.save_skill(skill)
        self.store.event(None, "skill.created", {"skill_id": result["id"], "state": "candidate"})
        return result

    def matching(self, source: str, source_type: str) -> dict[str, Any] | None:
        host = (urlparse(source).hostname or "").lower()
        for skill in self.store.list_skills():
            if skill["state"] not in {"validated", "stable"}:
                continue
            rules = skill["rules"]
            if rules.get("source_type") and rules["source_type"] != source_type:
                continue
            if rules.get("domain") and not (host == rules["domain"] or host.endswith("." + rules["domain"])):
                continue
            if rules.get("url_prefix") and not source.startswith(rules["url_prefix"]):
                continue
            return skill
        return None

    def apply(self, skill_id: str, job: dict[str, Any], *, explicit: bool = False) -> dict[str, Any]:
        skill = self.store.get_skill(skill_id)
        if not skill:
            raise KeyError(skill_id)
        if skill["state"] == "degraded" or (skill["state"] == "candidate" and not explicit):
            raise ValueError("skill is not eligible for automatic application")
        options = dict(job["options"])
        config = skill["config"]
        if config.get("resolver"): options["resolver_override"] = config["resolver"]
        if config.get("engine"): options["engine_override"] = config["engine"]
        if config.get("auth_ref"): options["auth_ref"] = config["auth_ref"]
        options["applied_skill_id"] = skill_id
        updated = self.store.update(job["id"], options=options)
        self.store.event(job["id"], "skill.applied", {"skill_id": skill_id, "version": skill["version"], "explicit": explicit})
        return updated

    def validate(self, skill_id: str, success: bool, detail: str = "") -> dict[str, Any]:
        skill = self.store.get_skill(skill_id)
        if not skill: raise KeyError(skill_id)
        history = skill["validation_history"][-19:] + [{"at": utcnow(), "success": bool(success), "detail": str(detail)[:160]}]
        successes = skill["success_count"] + int(success)
        failures = skill["failure_count"] + int(not success)
        state = "degraded" if failures >= 3 and failures > successes else ("stable" if successes >= 3 else "validated" if success else skill["state"])
        if state != skill["state"]: skill["version"] += 1
        result = self.store.save_skill({**skill, "state": state, "success_count": successes, "failure_count": failures,
                                      "validation_json": __import__("json").dumps(history),
                                      "last_verified": utcnow() if success else skill["last_verified"]})
        self.store.event(None, "skill.validation", {"skill_id": skill_id, "success": bool(success), "state": result["state"], "version": result["version"]})
        return result

    def promote(self, skill_id: str, target: str) -> dict[str, Any]:
        if target not in {"validated", "stable"}: raise ValueError("target must be validated or stable")
        skill = self.store.get_skill(skill_id)
        if not skill: raise KeyError(skill_id)
        if target == "stable" and skill["state"] != "validated": raise ValueError("only validated skills can be promoted to stable")
        if target == "validated" and skill["state"] != "candidate": raise ValueError("only candidate skills can be validated")
        skill.update(state=target, version=skill["version"] + 1,
                     validation_json=__import__("json").dumps(skill["validation_history"] + [{"at": utcnow(), "success": True, "detail": "manually promoted"}]),
                     last_verified=utcnow())
        result = self.store.save_skill(skill)
        self.store.event(None, "skill.promoted", {"skill_id": skill_id, "state": target, "version": result["version"]})
        return result
