import pytest

from urllib.request import Request

from transfer_hub.security import PublicHTTPRedirectHandler, validate_public_http_url


def test_rejects_loopback():
    with pytest.raises(ValueError):
        validate_public_http_url("http://127.0.0.1/file")



def test_rejects_tailnet_cgnat_range():
    with pytest.raises(ValueError):
        validate_public_http_url("http://100.84.55.11/file")

def test_rejects_non_http():
    with pytest.raises(ValueError):
        validate_public_http_url("file:///etc/passwd")


def test_redirect_handler_rejects_private_target():
    handler = PublicHTTPRedirectHandler()
    with pytest.raises(ValueError):
        handler.redirect_request(
            Request("https://example.com/start"), None, 302, "Found", {},
            "http://100.64.0.1/private",
        )
