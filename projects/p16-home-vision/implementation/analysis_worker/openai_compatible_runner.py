from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Callable

import httpx

from app.domain.analysis_contract import (
    AnalysisRequest,
    AnalysisResult,
    EvidenceRef,
    EventResult,
    ModelRunnerError,
    RunnerIdentity,
)


_IMAGE_ARTIFACT_HINTS = ("frame", "image", "keyframe")
_ALLOWED_IMAGE_MEDIA_TYPES = {"image/jpeg", "image/png", "image/webp"}


@dataclass(frozen=True)
class EvidenceImage:
    artifact_id: str
    media_type: str
    data: bytes


EvidenceLoader = Callable[[EvidenceRef], EvidenceImage]


@dataclass(frozen=True)
class OpenAICompatibleRunnerConfig:
    base_url: str
    model: str
    api_key: str = ""
    model_version: str = "provider-managed"
    timeout_seconds: float = 30.0
    max_images: int = 16


_DEFAULT_RESPONSE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["events"],
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "start_offset_ms",
                    "end_offset_ms",
                    "event_type",
                    "summary",
                    "confidence",
                    "evidence_artifact_ids",
                ],
                "properties": {
                    "start_offset_ms": {"type": "integer", "minimum": 0},
                    "end_offset_ms": {"type": ["integer", "null"], "minimum": 0},
                    "event_type": {"type": "string", "minLength": 1},
                    "summary": {"type": "string", "minLength": 1},
                    "confidence": {
                        "type": ["number", "null"],
                        "minimum": 0,
                        "maximum": 1,
                    },
                    "evidence_artifact_ids": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    },
                },
            },
        }
    },
}


class OpenAICompatibleModelRunner:
    """Production-shaped OpenAI-compatible VLM adapter for P16's ModelRunner.

    The runner never extracts frames from video. Image bytes are supplied through an
    injected evidence loader so P16's selected-frame and PTS provenance remain the
    evidence authority.
    """

    def __init__(
        self,
        config: OpenAICompatibleRunnerConfig,
        *,
        evidence_loader: EvidenceLoader,
        client: httpx.Client | None = None,
    ) -> None:
        if not config.base_url.strip() or not config.model.strip():
            raise ValueError("base_url and model are required")
        if config.timeout_seconds <= 0 or config.max_images < 1:
            raise ValueError("invalid runner limits")
        self._config = config
        self._evidence_loader = evidence_loader
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=config.timeout_seconds)

    def identity(self) -> RunnerIdentity:
        return RunnerIdentity(
            model_id=self._config.model,
            model_version=self._config.model_version,
            runtime_backend="openai-compatible",
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def analyze(self, request: AnalysisRequest) -> AnalysisResult:
        refs = tuple(
            ref
            for ref in request.evidence
            if any(hint in ref.artifact_type.lower() for hint in _IMAGE_ARTIFACT_HINTS)
        )[: self._config.max_images]
        if not refs:
            raise ModelRunnerError("ANALYSIS_VISUAL_EVIDENCE_REQUIRED")

        images: list[tuple[EvidenceRef, EvidenceImage]] = []
        for ref in refs:
            try:
                image = self._evidence_loader(ref)
            except Exception as exc:
                raise ModelRunnerError("ANALYSIS_EVIDENCE_LOAD_FAILED") from exc
            if image.artifact_id != ref.artifact_id:
                raise ModelRunnerError("ANALYSIS_EVIDENCE_ID_MISMATCH")
            if image.media_type not in _ALLOWED_IMAGE_MEDIA_TYPES or not image.data:
                raise ModelRunnerError("ANALYSIS_EVIDENCE_IMAGE_INVALID")
            images.append((ref, image))

        schema = self._response_schema(request.output_schema_json)
        payload = {
            "model": self._config.model,
            "messages": [
                {
                    "role": "user",
                    "content": self._message_content(request, images),
                }
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "p16_analysis_result",
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        headers = {"Content-Type": "application/json"}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"

        try:
            response = self._client.post(
                f"{self._config.base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
                timeout=self._config.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise ModelRunnerError("ANALYSIS_PROVIDER_TIMEOUT") from exc
        except httpx.HTTPError as exc:
            raise ModelRunnerError("ANALYSIS_PROVIDER_TRANSPORT_FAILED") from exc

        if response.status_code >= 400:
            raise ModelRunnerError(self._http_error_code(response.status_code))

        content = self._extract_content(response)
        result_payload = self._parse_result_payload(content)
        supplied_ids = {ref.artifact_id for ref, _ in images}
        events = self._events_from_payload(result_payload, supplied_ids)
        return AnalysisResult(run_id=request.run_id, asset_id=request.asset_id, events=events)

    @staticmethod
    def _response_schema(raw: str | None) -> dict[str, object]:
        if raw is None:
            return _DEFAULT_RESPONSE_SCHEMA
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ModelRunnerError("ANALYSIS_OUTPUT_SCHEMA_INVALID") from exc
        if not isinstance(parsed, dict):
            raise ModelRunnerError("ANALYSIS_OUTPUT_SCHEMA_INVALID")
        return parsed

    @staticmethod
    def _message_content(
        request: AnalysisRequest,
        images: list[tuple[EvidenceRef, EvidenceImage]],
    ) -> list[dict[str, object]]:
        intent = request.semantic_intent or "Describe only visually grounded events."
        text = (
            f"P16 asset: {request.asset_id}\n"
            f"Analysis intent: {intent}\n"
            "Return only events grounded in the supplied images. Every event must "
            "include one or more evidence_artifact_ids chosen from the labels below."
        )
        content: list[dict[str, object]] = [{"type": "text", "text": text}]
        for ref, image in images:
            label = (
                f"Evidence artifact_id={ref.artifact_id}; "
                f"start_offset_ms={ref.start_offset_ms}; end_offset_ms={ref.end_offset_ms}"
            )
            content.append({"type": "text", "text": label})
            encoded = base64.b64encode(image.data).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{image.media_type};base64,{encoded}",
                    },
                }
            )
        return content

    @staticmethod
    def _extract_content(response: httpx.Response) -> str:
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelRunnerError("ANALYSIS_PROVIDER_RESPONSE_INVALID") from exc
        if not isinstance(content, str) or not content.strip():
            raise ModelRunnerError("ANALYSIS_PROVIDER_RESPONSE_INVALID")
        return content

    @staticmethod
    def _parse_result_payload(content: str) -> dict[str, object]:
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ModelRunnerError("ANALYSIS_PROVIDER_JSON_INVALID") from exc
        if not isinstance(parsed, dict):
            raise ModelRunnerError("ANALYSIS_PROVIDER_JSON_INVALID")
        return parsed

    @staticmethod
    def _events_from_payload(
        payload: dict[str, object], supplied_ids: set[str]
    ) -> tuple[EventResult, ...]:
        raw_events = payload.get("events")
        if not isinstance(raw_events, list):
            raise ModelRunnerError("ANALYSIS_PROVIDER_EVENTS_INVALID")

        events: list[EventResult] = []
        for item in raw_events:
            if not isinstance(item, dict):
                raise ModelRunnerError("ANALYSIS_PROVIDER_EVENTS_INVALID")
            start = item.get("start_offset_ms")
            end = item.get("end_offset_ms")
            event_type = item.get("event_type")
            summary = item.get("summary")
            confidence = item.get("confidence")
            evidence_ids = item.get("evidence_artifact_ids")

            if isinstance(start, bool) or not isinstance(start, int) or start < 0:
                raise ModelRunnerError("ANALYSIS_PROVIDER_EVENT_TIME_INVALID")
            if end is not None and (
                isinstance(end, bool) or not isinstance(end, int) or end < start
            ):
                raise ModelRunnerError("ANALYSIS_PROVIDER_EVENT_TIME_INVALID")
            if not isinstance(event_type, str) or not event_type.strip():
                raise ModelRunnerError("ANALYSIS_PROVIDER_EVENT_INVALID")
            if not isinstance(summary, str) or not summary.strip():
                raise ModelRunnerError("ANALYSIS_PROVIDER_EVENT_INVALID")
            if confidence is not None and (
                isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not 0 <= float(confidence) <= 1
            ):
                raise ModelRunnerError("ANALYSIS_PROVIDER_EVENT_INVALID")
            if (
                not isinstance(evidence_ids, list)
                or not evidence_ids
                or not all(isinstance(value, str) and value for value in evidence_ids)
            ):
                raise ModelRunnerError("ANALYSIS_PROVIDER_UNGROUNDED_EVENT")
            if not set(evidence_ids).issubset(supplied_ids):
                raise ModelRunnerError("ANALYSIS_PROVIDER_UNGROUNDED_EVIDENCE")

            events.append(
                EventResult(
                    start_offset_ms=start,
                    end_offset_ms=end,
                    event_type=event_type,
                    summary=summary,
                    confidence=float(confidence) if confidence is not None else None,
                    evidence_artifact_ids=tuple(evidence_ids),
                )
            )
        return tuple(events)

    @staticmethod
    def _http_error_code(status_code: int) -> str:
        if status_code in {401, 403}:
            return "ANALYSIS_PROVIDER_AUTH_FAILED"
        if status_code == 429:
            return "ANALYSIS_PROVIDER_RATE_LIMITED"
        if status_code >= 500:
            return "ANALYSIS_PROVIDER_UNAVAILABLE"
        return "ANALYSIS_PROVIDER_HTTP_FAILED"
