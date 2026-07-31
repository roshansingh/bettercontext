# MCP Integration: Connecting SuperContext to AI Agents

**A guide to SuperContext's Model Context Protocol (MCP) integration—how it works, how to use it, and why it matters for agent-driven code understanding.**

**Last updated**: 2026-05-25

---

## Part 1: What is MCP?

### The Model Context Protocol

The **Model Context Protocol (MCP)** is a standardized interface that allows AI agents to discover and call tools. Think of it as a universal language that IDEs, code editors, and AI agents speak to access external capabilities without needing IDE-specific plugins.

SuperContext uses MCP to expose its knowledge graph as a set of tools that any MCP-compatible IDE can call. This means:

- **IDE-agnostic** — Works in Claude Code, Cursor, Cody, Zed, Continue, VS Code, and JetBrains AI without per-IDE custom code
- **Agent-native** — Agents call MCP tools the same way they call any other tool (search the web, read files, etc.)
- **Standard interface** — Every IDE that supports MCP can access SuperContext immediately, no adapter layer needed

### Why SuperContext Uses MCP

Without MCP, SuperContext would need a separate extension or plugin for each IDE. Building for eight IDEs individually would be:
- **Expensive** — Eight integrations instead of one
- **Brittle** — Updates to IDE APIs break all custom extensions
- **Incompatible** — Claude Code, Cursor, Copilot, and Cody all have different plugin architectures

MCP solves this by defining a single standard. Every major IDE (as of early 2026) adopted MCP, so one SuperContext MCP server reaches all of them. See `adr/0002-mcp-protocol-for-external-surface.md` for the full architectural decision.

### Eight Tools SuperContext Exports

SuperContext exposes exactly eight MCP tools, mapped directly to the CLI queries you learned in `querying.md`:

| Tool Name | CLI Equivalent | What It Does |
|-----------|---|---|
| `search_services` | `python -m source.scripts.query_kg summary` | Find services by name, namespace, or slug |
| `get_service_brief` | `python -m source.scripts.query_kg service-detail <name>` | Get a service's endpoints, events, and dependencies |
| `find_callers` | `python -m source.scripts.query_kg find-callers <symbol>` | List all functions/methods that call a given symbol |
| `find_callees` | `python -m source.scripts.query_kg find-callees <symbol>` | List all functions a given symbol calls |
| `get_event_consumers` | `python -m source.scripts.query_kg event-consumers <channel>` | Find all services consuming a message topic or queue |
| `get_event_producers` | `python -m source.scripts.query_kg event-producers <channel>` | Find all services publishing to a topic or queue |
| `blast_radius` | `python -m source.scripts.query_kg blast-radius <symbol> --depth 2` | Show all downstream impact of a change |
| `deploy_blockers_for` | `python -m source.scripts.query_kg deploy-blockers <service>` | Find services that must deploy before this one (planned) |

### Benefits of MCP for Agents

**1. Discovered tools** — When an agent loads your SuperContext MCP server, it discovers all eight tools automatically. No hardcoding, no documentation reading required.

**2. Structured results** — Tools return JSON with predictable fields (symbol, callers, evidence, etc.). Agents can reliably parse and reason about results.

**3. Versioning and evolution** — If you add a ninth tool later, agents discover it instantly. Backward compatibility is built in.

**4. Error handling** — MCP defines how to handle missing symbols, timeouts, and unsupported queries. Agents get consistent error semantics across all tools.

**5. Cost and latency control** — Each tool specifies `--limit` (how many results to return). Agents can ask for 5 callers instead of 5,000, keeping token usage and latency predictable.

---

## Part 2: How the Local Server Works

### The MCP Server Architecture

The SuperContext MCP server is a simple JSON-RPC HTTP endpoint that runs on your machine. When you start it, it:

1. Reads a KG snapshot (JSONL files: entities, facts, evidence, coverage)
2. Starts a local HTTP server on `localhost:3845` (configurable)
3. Waits for agents to connect and call tools

```
┌─────────────────┐
│   IDE Agent     │ (Claude Code, Cursor, Cody, etc.)
│  (Claude, GPT)  │
└────────┬────────┘
         │ MCP request (HTTP JSON-RPC)
         │
┌────────▼────────┐
│  MCP Server     │ (localhost:3845/mcp)
│  (SuperContext) │
└────────┬────────┘
         │ Query
         │
┌────────▼────────────┐
│  KG Snapshot        │
│ (entities.jsonl,    │
│  facts.jsonl,       │
│  evidence.jsonl)    │
└─────────────────────┘
```

### Starting the Server

First, build a KG snapshot, then start the server:

```bash
supercontext-build-kg --repo /path/to/repo --out ./data/kg_runs/my-repo
supercontext-mcp-server --snapshot ./data/kg_runs/my-repo
```

This will:

1. **Build the snapshot** — Extracts entities, facts, and evidence from your repo
2. **Start the server** — Launches the MCP server on `localhost:3845` in the foreground
3. **Print the endpoint** — Shows you the URL to register with your IDE

Output:
```
SuperContext MCP server running at http://127.0.0.1:3845/mcp
Snapshot: /Users/you/my-repo/.supercontext/kg

Ready for tool calls. Press Ctrl+C to stop.
```

### Server Configuration

The server respects these command-line and environment options:

```bash
# Change the port (default: 3845)
supercontext-mcp-server --port 8000

# Use a specific snapshot
supercontext-mcp-server --snapshot /path/to/snapshot

# Write logs to a file for debugging
supercontext-mcp-server --log-file ~/.supercontext/mcp.log
```

### How Tool Registration Works

When an agent connects to the MCP server, it sends an `initialize` request:

```json
{
  "jsonrpc": "2.0",
  "method": "initialize",
  "params": {
    "protocolVersion": "2024-11-05",
    "capabilities": {},
    "clientInfo": {"name": "Claude Code", "version": "1.0"}
  }
}
```

The server responds with the list of eight tools:

```json
{
  "jsonrpc": "2.0",
  "result": {
    "protocolVersion": "2024-11-05",
    "capabilities": {},
    "serverInfo": {"name": "SuperContext", "version": "0.1"},
    "tools": [
      {
        "name": "search_services",
        "description": "Find services by name, namespace, or slug",
        "inputSchema": {
          "type": "object",
          "properties": {
            "query": {"type": "string", "description": "Search term"}
          },
          "required": ["query"]
        }
      },
      ...
    ]
  }
}
```

The agent caches this list and can now call any of the eight tools.

### What Happens When a Tool Is Called

When an agent calls `find_callers`:

```json
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "params": {
    "name": "find_callers",
    "arguments": {"symbol": "authenticate", "limit": 5}
  }
}
```

The server:

1. **Looks up the symbol** in `entities.jsonl` (finds the function definition)
2. **Queries facts** from `facts.jsonl` (finds all `CALLS` relations pointing to it)
3. **Fetches evidence** from `evidence.jsonl` (gets file, line, commit for each result)
4. **Returns results** as JSON to the agent

```json
{
  "jsonrpc": "2.0",
  "result": {
    "content": [
      {
        "type": "text",
        "text": "Found 5 callers of authenticate()\n\n1. get_user_info (app.py:12) - evidence: ev_abc123\n2. process_payment (payments/handler.py:45) - evidence: ev_def456\n..."
      }
    ]
  }
}
```

### Snapshot Relationship

The MCP server **reads from your snapshot**, it does not generate facts on the fly. This is important because:

- **Snapshots are static** — Built once, queried many times. You get the same results every time.
- **Snapshot age matters** — If you change code, the snapshot becomes stale. Rebuild with `supercontext-init --refresh` to pick up new functions and relationships.
- **Snapshot scope is fixed** — A snapshot of `my-repo` will not answer questions about `other-repo`. Use multi-repo snapshots if you need to query across multiple repositories.

---

## Part 3: Using the MCP Server

### Registering the Server with Your IDE

#### Claude Code

Add the MCP server to your Claude Code settings:

1. Create or edit `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "supercontext": {
      "command": "bash",
      "args": ["-c", "cd /path/to/my-repo && supercontext-mcp-server"]
    }
  }
}
```

2. Restart Claude Code
3. Open your repository. Claude Code will start the SuperContext MCP server automatically and discover the eight tools.

#### Cursor / VS Code

In `.cursor/settings` or VS Code settings:

```json
{
  "mcp": {
    "supercontext": {
      "command": "bash",
      "args": ["-c", "cd /path/to/my-repo && supercontext-mcp-server"]
    }
  }
}
```

#### Cody (Sourcegraph)

Cody reads MCP configuration from `~/.cody/mcp-config.json`:

```json
{
  "mcpServers": {
    "supercontext": {
      "command": "bash",
      "args": ["-c", "cd /path/to/my-repo && supercontext-mcp-server"]
    }
  }
}
```

### How Agents Call Tools

Once the MCP server is registered, agents can call tools naturally in conversation. Here are example prompts that trigger tool calls:

**Example 1: Find where a function is called**

> I'm thinking of refactoring the `authenticate` function. Before I change it, show me everywhere it's called.

The agent calls `find_callers("authenticate")` and gets back 42 callers.

**Example 2: Understand impact before deletion**

> I want to delete the `process_order` function. What will break? Show me the blast radius.

The agent calls `blast_radius("process_order", depth=2)` and sees two levels of downstream dependencies.

**Example 3: Service-level exploration**

> Tell me about the payment-service. What endpoints does it expose? What events does it consume?

The agent calls `get_service_brief("payment-service")` and gets back endpoints, event subscriptions, and dependencies.

**Example 4: Event-driven debugging**

> What services are listening to the `order.created` topic?

The agent calls `get_event_consumers("order.created")` and lists all subscribers.

### Tool Signatures and Argument Schema

Each tool has a well-defined schema. Here are the eight:

#### `search_services`
```json
{
  "query": "string (required) - Service name, slug, or namespace to search"
}
```

#### `get_service_brief`
```json
{
  "service": "string (required) - Service name or slug",
  "limit": "integer (optional) - Max results per category (default: 10)"
}
```

#### `find_callers`
```json
{
  "symbol": "string (required) - Function or method name",
  "limit": "integer (optional) - Max callers to return (default: 10)"
}
```

#### `find_callees`
```json
{
  "symbol": "string (required) - Function or method name",
  "limit": "integer (optional) - Max callees to return (default: 10)"
}
```

#### `get_event_consumers`
```json
{
  "channel": "string (required) - Event topic or queue name",
  "limit": "integer (optional) - Max consumers to return (default: 10)"
}
```

#### `get_event_producers`
```json
{
  "channel": "string (required) - Event topic or queue name",
  "limit": "integer (optional) - Max producers to return (default: 10)"
}
```

#### `blast_radius`
```json
{
  "symbol": "string (required) - Function or method name",
  "depth": "integer (optional) - Depth of call graph to traverse (default: 1)",
  "limit": "integer (optional) - Max results per level (default: 10)"
}
```

#### `deploy_blockers_for`
```json
{
  "service": "string (required) - Service name or slug"
}
```

### Real-World Example Workflow

Here's how an agent might use the tools together:

1. **Agent starts** — You ask: "Show me the impact of changing the auth module"

2. **Agent calls `blast_radius`** — Runs `blast_radius("authenticate", depth=2)` and gets 47 downstream symbols

3. **Agent calls `get_service_brief`** — Runs `get_service_brief("auth-service")` to understand the service's role

4. **Agent calls `get_event_consumers`** — Runs `get_event_consumers("auth.login")` to see which services react to auth events

5. **Agent synthesizes** — Summarizes: "Changing authenticate() affects 47 functions across 3 services, and 5 services listen to auth.login events. High risk change."

---

## Part 4: Extending with Custom Tools

### Adding New MCP Tools

SuperContext's eight tools are designed to be the core set. However, you can extend the MCP server with custom tools specific to your organization.

**Location:** Custom tools live in `source/kg/product/mcp_tools_custom.py`

**Template:**

```python
# source/kg/product/mcp_tools_custom.py

from dataclasses import dataclass
from typing import Optional

@dataclass
class CustomTool:
    name: str
    description: str
    input_schema: dict
    
    async def execute(self, arguments: dict) -> dict:
        """Your custom logic here."""
        pass
```

**Example: Custom tool for finding deprecated APIs**

```python
@dataclass
class FindDeprecatedCalls(CustomTool):
    name = "find_deprecated_calls"
    description = "Find all calls to deprecated APIs in your codebase"
    input_schema = {
        "api_name": {"type": "string", "description": "Name of deprecated API"},
        "limit": {"type": "integer", "default": 10}
    }
    
    async def execute(self, arguments: dict):
        api_name = arguments["api_name"]
        limit = arguments.get("limit", 10)
        
        # Query your snapshot for calls to deprecated APIs
        callers = find_callers_of(api_name, limit)
        
        return {
            "api_name": api_name,
            "call_count": len(callers),
            "callers": callers,
            "warning": f"These {len(callers)} calls should migrate to the new API."
        }
```

### Tool Implementation Pattern

All tools follow this pattern:

1. **Take structured input** — Arguments match the tool's `input_schema`
2. **Query the snapshot** — Use the KG's facts, entities, and evidence
3. **Return structured output** — Always return JSON with a consistent shape
4. **Handle errors gracefully** — Return refusal metadata if the tool cannot answer

Example:

```python
async def execute(self, arguments: dict):
    symbol = arguments.get("symbol")
    limit = arguments.get("limit", 10)
    
    if not symbol:
        return {
            "status": "error",
            "reason": "symbol is required",
            "suggestion": "Provide a function or method name"
        }
    
    # Query the snapshot
    results = query_snapshot("find_callers", symbol, limit)
    
    if not results:
        return {
            "status": "not_found",
            "symbol": symbol,
            "message": f"No callers found for {symbol}"
        }
    
    return {
        "status": "found",
        "symbol": symbol,
        "results": results,
        "count": len(results)
    }
```

### Hosting Custom Tools Alongside SuperContext

You have two options:

**Option 1: Local patch** — Modify `mcp_tools_custom.py` and rebuild locally. For testing only.

**Option 2: Custom MCP server** — Run a second MCP server on a different port that wraps SuperContext and adds your custom tools.

```bash
# Original SuperContext server
SUPERCONTEXT_MCP_PORT=3845 supercontext-mcp-server

# Your custom server (different port)
python -m my_org.custom_mcp_server --port 3846 --upstream-supercontext http://localhost:3845
```

Then register both servers in your IDE:

```json
{
  "mcpServers": {
    "supercontext": {
      "command": "bash",
      "args": ["-c", "supercontext-mcp-server"]
    },
    "my-custom-tools": {
      "command": "bash",
      "args": ["-c", "python -m my_org.custom_mcp_server --port 3846"]
    }
  }
}
```

### Security and Authentication

**Local development** — The MCP server runs on `localhost:3845` and accepts no authentication (it's local-only).

**Self-hosted** — For production self-hosted deployments, the server supports bearer token authentication:

```bash
SUPERCONTEXT_AUTH_TOKEN="secret-token" supercontext-mcp-server
```

Agents must include the token in requests:

```
Authorization: Bearer secret-token
```

**Multi-tenant cloud** — When hosted in the cloud, SuperContext uses OAuth 2.1 (planned). Users authenticate once, and the server returns a tenant-scoped token valid for all tools.

**Custom tools** — Custom tools inherit the authentication model from the host server. If the SuperContext server requires a token, your custom tools do too.

---

## Cross-References

- **`querying.md`** — CLI-based querying (what MCP tools are built from)
- **`knowledge-graph.md`** — KG structure that MCP tools query
- **`adr/0002-mcp-protocol-for-external-surface.md`** — Full architectural decision
- **`docs/getting-started/README.md`** — Getting started overview
