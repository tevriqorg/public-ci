import asyncio
from pathlib import Path

from mcp import Client

from transfer_hub.api import create_app
from transfer_hub.config import Settings
from transfer_hub.mcp_server import mcp


REPO = Path(__file__).resolve().parents[2]


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        bind="127.0.0.1",
        port=0,
        api_token_file=tmp_path / "api.token",
        secrets_dir=tmp_path / "secrets",
        qqxfer_url="http://127.0.0.1:9",
        qqxfer_token_file=tmp_path / "missing-token",
    )


def test_api_docs_cover_registered_contract(tmp_path: Path):
    app = create_app(_settings(tmp_path), start_worker=False)
    api_text = (REPO / "docs" / "API.md").read_text()
    paths = set(app.openapi()["paths"])

    missing = sorted(path for path in paths if path not in api_text)
    assert missing == []
    assert "/api/v1/session" not in paths
    assert not any(
        any(retired in path for retired in ("sidechat", "supervisor", "codex"))
        for path in paths
    )


def test_browser_receiver_suppresses_qq_cdn_referer():
    source = (REPO / "transfer_hub" / "web" / "src" / "browser_receiver.ts").read_text()
    assert 'referrerPolicy: "no-referrer"' in source
    assert "class QqRangeReader" in source
    assert "this.size = artifact.size" in source
    assert 'validator_policy: "ticket_size+range_length+zip_auth"' in source
    assert "HttpReader" not in source


def test_mcp_docs_cover_exposed_tools():
    async def run():
        async with Client(mcp) as client:
            result = await client.list_tools()
            names = {tool.name for tool in result.tools}
        text = (REPO / "docs" / "MCP.md").read_text()
        missing = sorted(name for name in names if f"`{name}`" not in text)
        assert missing == []

    asyncio.run(run())


def test_health_probe_does_not_count_as_user_activity():
    compose = (REPO / "deployments" / "tokyo" / "transfer-hub" / "compose.yaml").read_text()
    api_source = (REPO / "transfer_hub" / "transfer_hub" / "api.py").read_text()
    assert "X-P20-Healthcheck: 1" in compose
    assert 'request.headers.get("X-P20-Healthcheck") == "1"' in api_source


def test_runtime_tokens_are_not_passed_in_curl_argv():
    client = (REPO / "ops" / "p20-runtime-client.sh").read_text()
    ctl = (REPO / "ops" / "p20-runtime-ctl.sh").read_text()
    assert '--config -' in client and 'Authorization: Bearer $token' not in client
    assert '--config -' in ctl and 'Authorization: Bearer $tok' not in ctl


def test_browser_resolver_has_non_global_network_guard():
    source = (REPO / "transfer_hub" / "browser_resolver" / "server.mjs").read_text()
    assert "dns.lookup" in source
    assert "100 && b >= 64 && b <= 127" in source
    assert "context.route('**/*'" in source


def test_runtime_start_refreshes_idle_lease_when_already_running():
    source = (REPO / "ops" / "p20-runtime-ctl.sh").read_text()
    block = source[source.index("cmd_start() {"):source.index("cmd_stop() {")]
    assert 'touch "$STATE"' in block
    assert "idle lease refreshed" in block
