"""P2 #126: authenticated WeKnora-native UAT resource + derived handoff client.

This adapter does not own a second knowledge DB or scheduler. It refuses to
claim a completed handoff unless both API calls return confirmed identities.
"""
from __future__ import annotations

import json
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


class _RejectRedirects(HTTPRedirectHandler):
    """Never forward scoped keys or bearer tokens to a different origin."""
    def redirect_request(self, *args, **kwargs):
        return None


_NO_REDIRECT_OPENER = build_opener(_RejectRedirects())


class HandoffError(RuntimeError):
    """A remote or local durable handoff precondition was not satisfied."""


@dataclass(frozen=True)
class HandoffReceipt:
    knowledge_id: str
    resource_ref: str
    source_key: str
    source_revision_id: str


class WeKnoraHandoff:
    def __init__(self, *, api_url: str, bearer: str = "", kb_id: str, api_key: str = "",
                 timeout: int = 30, transport=None) -> None:
        url = api_url.rstrip('/')
        if not (url.startswith('http://127.0.0.1:') or url.startswith('http://172.17.0.1:')
                or url.startswith('https://')):
            raise ValueError('API must use private/loopback HTTP or HTTPS')
        if bool(bearer) == bool(api_key) or not kb_id:
            raise ValueError("Exactly one JWT bearer or scoped API key and KB id are required")
        self.base = url
        self.bearer = bearer
        self.api_key = api_key
        self.kb_id = kb_id
        self.timeout = timeout
        self._transport = transport or self._http

    def _http(self, route: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            f'{self.base}/knowledge-bases/{self.kb_id}/knowledge/{route}',
            data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
            headers={'Content-Type': 'application/json', **(
                {'X-API-Key': self.api_key} if self.api_key else
                {'Authorization': 'Bearer ' + self.bearer})},
            method='POST',
        )
        try:
            with _NO_REDIRECT_OPENER.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise HandoffError(f'WeKnora {route} HTTP {response.status}')
                result = json.load(response)
        except HTTPError as exc:
            raise HandoffError(f'WeKnora {route} HTTP {exc.code}') from exc
        except (URLError, OSError, ValueError) as exc:
            raise HandoffError(f'WeKnora {route} unavailable or invalid response') from exc
        if not isinstance(result, dict) or result.get('success') is not True:
            raise HandoffError(f'WeKnora {route} rejected handoff')
        return result

    def register_external(self, *, resource_path: str, size: int, sha256: str,
                          filename: str, mime: str) -> str:
        if (not resource_path.startswith('local://') or size < 0 or len(sha256) != 64
                or any(ch not in string.hexdigits for ch in sha256)):
            raise HandoffError('Invalid stable external source descriptor')
        result = self._transport('external-resource', {
            'resource_path': resource_path, 'size': size, 'sha256': sha256,
            'name': filename, 'mime': mime,
        })
        data = result.get('data') or {}
        ref = data.get('resource_ref', '')
        if not ref.startswith('resource://') or data.get('ownership') != 'external':
            raise HandoffError('WeKnora returned no durable external Resource ref')
        return ref

    def upsert_derived(self, envelope: dict[str, Any], *, resource_ref: str) -> HandoffReceipt:
        if not resource_ref.startswith('resource://'):
            raise HandoffError('External Resource registration must precede handoff')
        request = dict(envelope)
        request['source_resource_ref'] = resource_ref
        for k in ('source_key', 'source_revision_id', 'idempotency_key'):
            if not request.get(k):
                raise HandoffError(f'Missing {k}')
        result = self._transport('derived', request)
        data = result.get('data') or {}
        knowledge_id = data.get('id', '')
        if not knowledge_id:
            raise HandoffError('WeKnora returned no durable Knowledge id')
        return HandoffReceipt(knowledge_id, resource_ref,
                              request['source_key'], request['source_revision_id'])

    @staticmethod
    def save_receipt(path: Path, receipt: HandoffReceipt) -> None:
        # Caller must verify Knowledge + SourceRevision on the server before
        # marking a Transit session complete. This local receipt is provisional.
        payload = dict(knowledge_id=receipt.knowledge_id, resource_ref=receipt.resource_ref,
                       source_key=receipt.source_key, source_revision_id=receipt.source_revision_id,
                       verified_by_weknora=False)
        if path.exists():
            old = json.loads(path.read_text(encoding='utf-8'))
            if old != payload:
                raise HandoffError('Existing receipt conflicts with new handoff')
            return
        path.write_text(json.dumps(payload, indent=2), encoding='utf-8')
        path.chmod(0o600)
