#!/usr/bin/env python3
"""Fail closed when running P16 core containers do not share one release revision."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

CORE_SERVICES = (
    "home-vision",
    "home-vision-nvr-scan",
    "home-vision-archive-worker",
    "home-vision-thumbnail-worker",
)
ROLE_BY_SERVICE = {
    "home-vision": "api",
    "home-vision-nvr-scan": "nvr-scan",
    "home-vision-archive-worker": "archive-worker",
    "home-vision-thumbnail-worker": "thumbnail-worker",
}
CORE_ROLES = tuple(ROLE_BY_SERVICE.values())
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
LABEL_KEY = "io.tevriq.p16.release-revision"
ENV_KEY = "P16_RELEASE_REVISION"
PRODUCT_LABEL = "io.tevriq.product"
ROLE_LABEL = "io.tevriq.role"
STATE_ID_LABEL = "io.tevriq.state-id"


def _run(command: list[str], cwd: Path) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n{completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def _env_map(values: list[str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values or []:
        key, separator, item = value.partition("=")
        if separator:
            result[key] = item
    return result


def validate_expected_revision(value: str) -> str:
    revision = value.strip().lower()
    if not REVISION_RE.fullmatch(revision):
        raise ValueError("expected revision must be the exact 40-character lowercase Git SHA")
    return revision


def evaluate_containers(
    inspections: list[dict], expected_revision: str, *, require_all_core: bool = False
) -> dict:
    expected_revision = validate_expected_revision(expected_revision)
    by_service: dict[str, dict] = {}
    runtime_entries: list[dict] = []
    failures: list[str] = []

    for inspection in inspections:
        config = inspection.get("Config") or {}
        labels = config.get("Labels") or {}
        env = _env_map(config.get("Env"))
        compose_service = labels.get("com.docker.compose.service")
        product = labels.get(PRODUCT_LABEL) or ""
        if compose_service not in CORE_SERVICES and product != "P16":
            continue
        image_id = inspection.get("Image") or ""
        configured_image = config.get("Image") or ""
        label_revision = labels.get(LABEL_KEY) or ""
        env_revision = env.get(ENV_KEY) or ""
        role = labels.get(ROLE_LABEL) or ""
        entry = {
            "container_id": inspection.get("Id") or "",
            "compose_service": compose_service or "",
            "project": labels.get("com.docker.compose.project") or "",
            "product": product,
            "role": role,
            "state_id": labels.get(STATE_ID_LABEL) or "",
            "image_id": image_id,
            "configured_image": configured_image,
            "label_revision": label_revision,
            "env_revision": env_revision,
        }
        runtime_entries.append(entry)
        if compose_service in CORE_SERVICES and compose_service not in by_service:
            by_service[compose_service] = entry
        if product != "P16":
            failures.append(f"{compose_service or '<unknown>'}: product label {product or '<missing>'} != P16")
        if role not in CORE_ROLES:
            failures.append(f"{compose_service or '<unknown>'}: missing or invalid role label")
        if not entry["state_id"]:
            failures.append(f"{compose_service or '<unknown>'}: missing state-id label")
        if label_revision != expected_revision:
            failures.append(
                f"{compose_service}: image label revision {label_revision or '<missing>'} != {expected_revision}"
            )
        if env_revision != expected_revision:
            failures.append(
                f"{compose_service}: container env revision {env_revision or '<missing>'} != {expected_revision}"
            )

    state_ids = {entry["state_id"] for entry in runtime_entries if entry["state_id"]}
    if len(state_ids) > 1:
        failures.append("running P16 containers use different state-id labels: " + ", ".join(sorted(state_ids)))
    roles: dict[str, int] = {}
    for entry in runtime_entries:
        if entry["role"]:
            roles[entry["role"]] = roles.get(entry["role"], 0) + 1
    for role, count in roles.items():
        if count > 1:
            failures.append(f"duplicate P16 role: {role} ({count} running containers)")
    identities = {
        (entry["project"], entry["role"], entry["state_id"], entry["label_revision"], entry["configured_image"], entry["image_id"])
        for entry in runtime_entries
    }
    if len(identities) != len(runtime_entries):
        failures.append("duplicate P16 runtime identity (project/role/state/revision/image ref/image ID)")

    if "home-vision" not in by_service:
        failures.append("home-vision API container is not running")
    if require_all_core:
        for service in CORE_SERVICES:
            if service not in by_service:
                failures.append(f"{service}: required core container is not running")

    image_ids = {entry["image_id"] for entry in by_service.values() if entry["image_id"]}
    if len(image_ids) > 1:
        failures.append(
            "running P16 core containers use different image IDs: " + ", ".join(sorted(image_ids))
        )

    configured_images = {
        entry["configured_image"] for entry in by_service.values() if entry["configured_image"]
    }
    if len(configured_images) > 1:
        failures.append(
            "running P16 core containers were started from different image references: "
            + ", ".join(sorted(configured_images))
        )

    return {
        "expected_revision": expected_revision,
        "services": by_service,
        "runtime_entries": runtime_entries,
        "failures": failures,
        "ok": not failures,
    }


def _running_container_ids(compose_dir: Path) -> list[str]:
    command = ["docker", "ps", "-q"]
    output = _run(command, compose_dir)
    return [line.strip() for line in output.splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--expected-revision",
        default=os.environ.get(ENV_KEY, ""),
        help="Exact 40-character P16 Git revision (default: P16_RELEASE_REVISION).",
    )
    parser.add_argument(
        "--require-all-core",
        action="store_true",
        help="Also fail when any NVR/archive/normalize/thumbnail core service is not running.",
    )
    parser.add_argument(
        "--compose-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    args = parser.parse_args()

    try:
        expected = validate_expected_revision(args.expected_revision)
        container_ids = _running_container_ids(args.compose_dir)
        if not container_ids:
            raise RuntimeError("no running containers found for the P16 release compose project")
        inspection_text = _run(["docker", "inspect", *container_ids], args.compose_dir)
        inspections = json.loads(inspection_text)
        result = evaluate_containers(
            inspections, expected, require_all_core=args.require_all_core
        )
    except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
