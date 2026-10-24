from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


def validate_public_http_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("source must be a public http/https URL")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    infos = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    if not infos:
        raise ValueError("source hostname did not resolve")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise ValueError("non-global source destination is not allowed")


class PublicHTTPRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_public_http_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_public_http_opener = build_opener(PublicHTTPRedirectHandler())


def open_public_http(request: Request | str, *, timeout: float = 20):
    url = request.full_url if isinstance(request, Request) else str(request)
    validate_public_http_url(url)
    response = _public_http_opener.open(request, timeout=timeout)
    try:
        validate_public_http_url(response.geturl())
    except Exception:
        response.close()
        raise
    return response
