from __future__ import annotations

import pytest

from scripts.verify_release_consistency import evaluate_containers, validate_expected_revision

REVISION = "a" * 40
OTHER_REVISION = "b" * 40


def container(service: str, *, image_id: str = "sha256:one", label_revision: str = REVISION, env_revision: str = REVISION, role: str | None = None, state_id: str = "state-1", product: str = "P16", project: str = "p16-release") -> dict:
    return {
        "Id": f"container-{service}",
        "Image": image_id,
        "Config": {
            "Image": f"home-vision:{REVISION}",
            "Labels": {
                "com.docker.compose.service": service,
                "io.tevriq.p16.release-revision": label_revision,
                "io.tevriq.product": product,
                "io.tevriq.role": role or service.removeprefix("home-vision").strip("-") or "api",
                "io.tevriq.state-id": state_id,
                "com.docker.compose.project": project,
            },
            "Env": [f"P16_RELEASE_REVISION={env_revision}"],
        },
    }


def test_exact_revision_is_required() -> None:
    assert validate_expected_revision(REVISION) == REVISION
    with pytest.raises(ValueError):
        validate_expected_revision("dev-unpinned")
    with pytest.raises(ValueError):
        validate_expected_revision("a" * 7)


def test_consistent_running_core_services_pass() -> None:
    result = evaluate_containers(
        [
            container("home-vision"),
            container("home-vision-nvr-scan"),
            container("home-vision-archive-worker"),
        ],
        REVISION,
    )
    assert result["ok"] is True
    assert result["failures"] == []


def test_stale_image_revision_fails_closed() -> None:
    result = evaluate_containers(
        [container("home-vision", label_revision=OTHER_REVISION)], REVISION
    )
    assert result["ok"] is False
    assert any("image label revision" in failure for failure in result["failures"])


def test_container_env_revision_mismatch_fails_closed() -> None:
    result = evaluate_containers(
        [container("home-vision", env_revision=OTHER_REVISION)], REVISION
    )
    assert result["ok"] is False
    assert any("container env revision" in failure for failure in result["failures"])


def test_mixed_core_image_ids_fail_closed() -> None:
    result = evaluate_containers(
        [
            container("home-vision", image_id="sha256:one"),
            container("home-vision-thumbnail-worker", image_id="sha256:two"),
        ],
        REVISION,
    )
    assert result["ok"] is False
    assert any("different image IDs" in failure for failure in result["failures"])


def test_require_all_core_detects_missing_workers() -> None:
    result = evaluate_containers(
        [container("home-vision")], REVISION, require_all_core=True
    )
    assert result["ok"] is False
    assert any("home-vision-archive-worker" in failure for failure in result["failures"])


def test_global_p16_labels_and_roles_are_required() -> None:
    result = evaluate_containers([container("home-vision", product="other")], REVISION, require_all_core=True)
    assert result["ok"] is False
    assert any("product" in failure for failure in result["failures"])


def test_duplicate_p16_role_or_state_is_rejected() -> None:
    result = evaluate_containers(
        [container("home-vision", role="api"), container("home-vision-nvr-scan", role="api")],
        REVISION,
    )
    assert result["ok"] is False
    assert any("duplicate" in failure for failure in result["failures"])
