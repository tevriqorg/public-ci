import asyncio

from mcp import Client

from transfer_hub.mcp_server import mcp


def test_mcp_tool_surface():
    async def run():
        async with Client(mcp) as client:
            result = await client.list_tools()
            names = {tool.name for tool in result.tools}
            assert names == {
                "check_runtime", "start_runtime", "stop_runtime",
                "create_transfer", "get_transfer", "list_transfers",
                "clear_transfer", "clear_transfer_history",
                "pause_transfer", "resume_transfer", "cancel_transfer", "retry_transfer",
                "get_receiver_ticket", "get_receiver_url", "acknowledge_transfer",
                "analyze_transfer", "resolve_source", "get_transfer_logs", "get_ui_context",
                "list_skills", "get_skill", "apply_skill", "promote_skill",
                "retry_with_strategy", "diagnose_failure", "revoke_share",
            }
    asyncio.run(run())
