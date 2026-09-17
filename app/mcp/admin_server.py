"""Owner-only guarded production MCP server."""
from app.mcp import tools
from app.mcp.transport import transport_security_settings


def create_admin_server():
    from mcp.server.fastmcp import FastMCP

    security = transport_security_settings()
    security_kwargs = {"transport_security": security} if security else {}
    server = FastMCP(
        name="Versya Admin",
        instructions=(
            "Owner-only guarded operations for Versya production. "
            "Prefer read-only inspection; risky actions require dry_run and confirmation."
        ),
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        **security_kwargs,
    )

    server.tool()(tools.get_runtime_health)
    server.tool()(tools.get_deployed_version)
    server.tool()(tools.read_deployed_source)
    server.tool()(tools.list_db_tables)
    server.tool()(tools.describe_table)
    server.tool()(tools.run_readonly_sql)
    server.tool()(tools.run_maintenance_action)

    return server
