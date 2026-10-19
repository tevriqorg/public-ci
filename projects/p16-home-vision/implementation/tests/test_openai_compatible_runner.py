from __future__ import annotations

import json

import httpx
import pytest

from analysis_worker.openai_compatible_runner import (
    EvidenceImage,
    OpenAICompatibleModelRunner,
    OpenAICompatibleRunnerConfig,
)
from app.domain.analysis_contract import AnalysisRequest, EvidenceRef, ModelRunnerError


def _request(*, evidence: tuple[EvidenceRef, ...]) -> AnalysisRequest:
    return AnalysisRequest(
        run_id="run-1",
        asset_id="asset-1",
        profile_id="profile-1",
        profile_revision=1,
        profile_snapshot_hash="hash",
        semantic_intent="Describe the person's visible action.",
        output_schema_json=None,
        media_ref="home-vision://asset/asset-1",
        evidence=evidence,
    )


def _ref() -> EvidenceRef:
    return EvidenceRef(
        artifact_id="asset-1:frame:7",
        artifact_type="selected_frame",
        start_offset_ms=1250,
        end_offset_ms=1250,
    )


def _runner(handler) -> OpenAICompatibleModelRunner:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenAICompatibleModelRunner(
        OpenAICompatibleRunnerConfig(base_url="https://provider.invalid/v1", model="vision-model"),
        evidence_loader=lambda ref: EvidenceImage(
            artifact_id=ref.artifact_id,
            media_type="image/jpeg",
            data=b"jpeg-bytes",
        ),
        client=client,
    )


def test_runner_maps_grounded_structured_event() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["model"] == "vision-model"
        content = payload["messages"][0]["content"]
        labels = [item["text"] for item in content if item["type"] == "text"]
        assert any("artifact_id=asset-1:frame:7" in label for label in labels)
        assert any(item["type"] == "image_url" for item in content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "events": [
                                        {
                                            "start_offset_ms": 1250,
                                            "end_offset_ms": 1250,
                                            "event_type": "person_visible",
                                            "summary": "A person is visible.",
                                            "confidence": 0.9,
                                            "evidence_artifact_ids": ["asset-1:frame:7"],
                                        }
                                    ]
                                }
                            )
                        }
                    }
                ]
            },
        )

    result = _runner(handler).analyze(_request(evidence=(_ref(),)))

    assert result.run_id == "run-1"
    assert result.events[0].evidence_artifact_ids == ("asset-1:frame:7",)


def test_runner_rejects_provider_evidence_not_supplied_by_p16() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "events": [
                                        {
                                            "start_offset_ms": 1250,
                                            "end_offset_ms": None,
                                            "event_type": "person_visible",
                                            "summary": "A person is visible.",
                                            "confidence": None,
                                            "evidence_artifact_ids": ["invented-frame"],
                                        }
                                    ]
                                }
                            )
                        }
                    }
                ]
            },
        )

    with pytest.raises(ModelRunnerError) as excinfo:
        _runner(handler).analyze(_request(evidence=(_ref(),)))

    assert excinfo.value.code == "ANALYSIS_PROVIDER_UNGROUNDED_EVIDENCE"


def test_runner_refuses_visual_analysis_without_image_evidence() -> None:
    runner = _runner(lambda request: pytest.fail(f"unexpected provider call: {request}"))
    transcript = EvidenceRef(
        artifact_id="transcript-1",
        artifact_type="transcript",
        start_offset_ms=0,
        end_offset_ms=1000,
    )

    with pytest.raises(ModelRunnerError) as excinfo:
        runner.analyze(_request(evidence=(transcript,)))

    assert excinfo.value.code == "ANALYSIS_VISUAL_EVIDENCE_REQUIRED"


def test_runner_maps_rate_limit_without_persisting_provider_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(429, json={"error": "secret provider detail"})

    with pytest.raises(ModelRunnerError) as excinfo:
        _runner(handler).analyze(_request(evidence=(_ref(),)))

    assert excinfo.value.code == "ANALYSIS_PROVIDER_RATE_LIMITED"
    assert "secret provider detail" not in str(excinfo.value)
