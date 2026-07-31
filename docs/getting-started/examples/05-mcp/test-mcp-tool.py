#!/usr/bin/env python3
"""
Test MCP Tools Programmatically

This script demonstrates how to connect to a running SuperContext MCP server
and invoke its tools programmatically. Instead of using Claude Code's UI, you
can call tools directly from Python to integrate SuperContext into your own
applications, scripts, or automation.

Usage:
    1. Start the MCP server in another terminal:
       bash examples/05-mcp/start-mcp-server.sh

    2. Run this script:
       python test-mcp-tool.py

    3. Or call specific tools:
       python test-mcp-tool.py --tool search_services --query "api"
       python test-mcp_tool.py --tool find_callers --symbol "authenticate" --limit 5
       python test-mcp-tool.py --tool blast_radius --symbol "main" --depth 2

The script:
  - Connects to localhost:8000 (or custom URL)
  - Lists available tools
  - Calls a sample tool
  - Shows formatted results
  - Handles connection failures gracefully
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any, NamedTuple

try:
    import requests
except ImportError:
    print("Error: requests library not found. Install with: pip install requests")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Data Models
# ─────────────────────────────────────────────────────────────────────────────


class ToolCall(NamedTuple):
    """A tool invocation with arguments."""

    name: str
    params: dict[str, Any]


@dataclass
class ToolResult:
    """Result from a tool invocation."""

    success: bool
    data: Any
    error: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# MCP Client
# ─────────────────────────────────────────────────────────────────────────────


class MCPClient:
    """
    Client for calling SuperContext MCP tools.

    Communicates with a running MCP server via HTTP REST API.
    """

    def __init__(self, server_url: str = "http://localhost:8000"):
        self.server_url = server_url.rstrip("/")
        self.session = requests.Session()

    def health_check(self) -> bool:
        """Check if the MCP server is running and healthy."""
        try:
            response = self.session.get(f"{self.server_url}/health", timeout=2)
            return response.status_code == 200
        except (requests.ConnectionError, requests.Timeout):
            return False

    def list_tools(self) -> list[dict[str, Any]]:
        """List available tools on the MCP server."""
        try:
            response = self.session.get(f"{self.server_url}/tools", timeout=5)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            print(f"Error listing tools: {e}")
            return []

    def call_tool(self, name: str, **params) -> ToolResult:
        """
        Call a tool on the MCP server.

        Args:
            name: Tool name (e.g., "search_services")
            **params: Tool parameters

        Returns:
            ToolResult with success flag and data or error
        """
        try:
            payload = {
                "jsonrpc": "2.0",
                "method": name,
                "params": params,
                "id": 1,
            }

            response = self.session.post(
                f"{self.server_url}/call",
                json=payload,
                timeout=10,
            )

            response.raise_for_status()
            result = response.json()

            if "error" in result:
                return ToolResult(
                    success=False,
                    data=None,
                    error=result["error"].get("message", "Unknown error"),
                )

            return ToolResult(
                success=True,
                data=result.get("result"),
                error=None,
            )

        except requests.RequestException as e:
            return ToolResult(
                success=False,
                data=None,
                error=f"Request failed: {e}",
            )

    def close(self) -> None:
        """Close the session."""
        self.session.close()


# ─────────────────────────────────────────────────────────────────────────────
# Formatting
# ─────────────────────────────────────────────────────────────────────────────


def print_header(text: str) -> None:
    """Print a formatted section header."""
    print(f"\n{'─' * 80}")
    print(f"  {text}")
    print(f"{'─' * 80}\n")


def print_tool_info(tool: dict[str, Any]) -> None:
    """Print information about a tool."""
    name = tool.get("name", "?")
    description = tool.get("description", "No description")
    input_schema = tool.get("inputSchema", {})
    properties = input_schema.get("properties", {})

    print(f"  {name}")
    if description:
        print(f"    {description}")
    if properties:
        print(f"    Parameters:")
        for param_name, param_info in properties.items():
            required = "required" if param_name in input_schema.get("required", []) else "optional"
            param_type = param_info.get("type", "unknown")
            print(f"      - {param_name} ({param_type}, {required})")


def print_result_table(data: list[dict[str, Any]], headers: list[str] | None = None) -> None:
    """Print results as a formatted table."""
    if not data:
        print("  (no results)")
        return

    if not headers:
        headers = list(data[0].keys()) if data else []

    # Calculate column widths
    col_widths = {}
    for header in headers:
        col_widths[header] = len(header)

    for row in data:
        for header in headers:
            value_str = str(row.get(header, ""))
            col_widths[header] = max(col_widths[header], len(value_str))

    # Print header
    header_row = "  " + "  ".join(f"{h:<{col_widths[h]}}" for h in headers)
    print(header_row)
    print("  " + "─" * (len(header_row) - 2))

    # Print rows
    for row in data:
        row_values = [str(row.get(h, "")) for h in headers]
        row_str = "  " + "  ".join(f"{v:<{col_widths[h]}}" for h, v in zip(headers, row_values))
        print(row_str)


def print_result_json(data: Any, indent: int = 2) -> None:
    """Print result as formatted JSON."""
    if isinstance(data, str):
        print(f"  {data}")
    else:
        json_str = json.dumps(data, indent=indent)
        for line in json_str.split("\n"):
            print(f"  {line}")


# ─────────────────────────────────────────────────────────────────────────────
# Tool Calls (Examples)
# ─────────────────────────────────────────────────────────────────────────────


def demo_search_services(client: MCPClient) -> None:
    """Demo: Search for services by name."""
    print_header("Demo: search_services")

    result = client.call_tool("search_services", query="api", limit=5)

    if result.success and result.data:
        print("Found services matching 'api':")
        if isinstance(result.data, list):
            print_result_table(result.data, headers=["name", "slug", "repo"])
        else:
            print_result_json(result.data)
    else:
        print(f"Error: {result.error}")


def demo_find_callers(client: MCPClient) -> None:
    """Demo: Find callers of a symbol."""
    print_header("Demo: find_callers")

    # This would need a real symbol from the loaded snapshot
    result = client.call_tool("find_callers", symbol="authenticate", limit=5)

    if result.success and result.data:
        print("Functions that call 'authenticate':")
        if isinstance(result.data, list):
            print_result_table(result.data, headers=["caller", "file", "line"])
        else:
            print_result_json(result.data)
    else:
        print(f"Error: {result.error}")


def demo_get_service_brief(client: MCPClient) -> None:
    """Demo: Get service details."""
    print_header("Demo: get_service_brief")

    result = client.call_tool("get_service_brief", service="api-service", limit=10)

    if result.success and result.data:
        print("Service details:")
        print_result_json(result.data)
    else:
        print(f"Error: {result.error}")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Test SuperContext MCP tools programmatically"
    )
    parser.add_argument(
        "--url",
        default="http://localhost:8000",
        help="MCP server URL (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--tool",
        help="Tool to call (e.g., search_services, find_callers)",
    )
    parser.add_argument(
        "--query",
        help="Query parameter (for search_services)",
    )
    parser.add_argument(
        "--symbol",
        help="Symbol parameter (for find_callers, find_callees, etc.)",
    )
    parser.add_argument(
        "--service",
        help="Service parameter (for get_service_brief)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Result limit",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=2,
        help="Depth for blast_radius",
    )
    parser.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="Output format",
    )

    args = parser.parse_args()

    print(f"\nSuperContext MCP Tool Tester")
    print(f"Server: {args.url}\n")

    # Connect to MCP server
    client = MCPClient(args.url)

    # Check health
    print("Checking server health...", end=" ", flush=True)
    if not client.health_check():
        print("\n")
        print("ERROR: MCP server not responding")
        print(f"Is the server running at {args.url}?")
        print(f"Start it with: bash examples/05-mcp/start-mcp-server.sh")
        return 1

    print("OK")

    # List available tools
    print("\nFetching available tools...", end=" ", flush=True)
    tools = client.list_tools()
    print(f"Found {len(tools)}")

    if tools:
        print_header("Available Tools")
        for tool in tools:
            print_tool_info(tool)

    # Call a specific tool if requested
    if args.tool:
        print_header(f"Calling Tool: {args.tool}")

        params = {}
        if args.query:
            params["query"] = args.query
        if args.symbol:
            params["symbol"] = args.symbol
        if args.service:
            params["service"] = args.service
        params["limit"] = args.limit
        if args.depth:
            params["depth"] = args.depth

        result = client.call_tool(args.tool, **params)

        if result.success:
            if args.format == "json":
                print_result_json(result.data)
            else:
                if isinstance(result.data, list):
                    print_result_table(result.data)
                else:
                    print_result_json(result.data)
        else:
            print(f"Error: {result.error}")
            return 1

        return 0

    # Run demo calls
    print_header("Running Demo Calls")

    demo_search_services(client)
    demo_find_callers(client)
    demo_get_service_brief(client)

    client.close()

    print_header("Demo Complete")
    print("\nTo call a specific tool, use:")
    print("  python test-mcp-tool.py --tool TOOL_NAME [--param value]")
    print("\nExamples:")
    print("  python test-mcp-tool.py --tool search_services --query 'api'")
    print("  python test-mcp-tool.py --tool find_callers --symbol 'authenticate'")
    print("  python test-mcp-tool.py --tool get_service_brief --service 'api-service'")
    print("  python test-mcp-tool.py --tool blast_radius --symbol 'main' --depth 2")

    return 0


if __name__ == "__main__":
    sys.exit(main())
