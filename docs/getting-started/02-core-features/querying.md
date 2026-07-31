# Querying the Knowledge Graph: Complete Reference

**A comprehensive guide to querying SuperContext's knowledge graph with 8 standard tools, custom queries, and performance optimization.**

**Last updated**: 2026-05-25

---

## Part 1: Query Basics

### What is a Query?

A **query** is a request to extract specific knowledge from your knowledge graph snapshot. Instead of manually reading code, queries let you ask questions like:
- "Who calls this function?"
- "What breaks if I change this service?"
- "What event channels does this service subscribe to?"

Queries are **fast** (milliseconds on snapshots with thousands of entities), **verifiable** (every result includes evidence), and **deterministic** (same snapshot always produces same results).

### Why Queries Matter

**1. Refactoring Safety** — Before changing a function, query its callers and blast radius. Know exactly what breaks before you write code.

**2. Dependency Understanding** — Understand your microservice architecture at scale. Which services depend on which? What's the transitive impact of a schema change?

**3. Incident Investigation** — "Why did this service break?" Query who calls it, what changed, and what evidence exists for the failure.

**4. Onboarding** — New engineers understand the codebase by querying: "Show me all services that depend on the auth service."

### Command Syntax

All queries use the same CLI interface:

```bash
python -m source.scripts.query_kg \
  --snapshot <path-to-snapshot-dir> \
  <query-name> \
  [required-args] \
  [--optional-args] \
  [--limit N]
```

### Output Formats

Queries support three output formats. By default, output is printed as JSON to stdout.

#### JSON Output (Default)

Structured data for programmatic processing:

```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/my-repo \
  find-callers authenticate --limit 5
```

Output:
```json
{
  "status": "found",
  "symbol": "authenticate",
  "caller_count": 42,
  "returned_count": 3,
  "callers": [
    {
      "symbol": "get_user_info",
      "module": "app",
      "file": "app.py",
      "line": 12,
      "evidence_ids": ["ev_abc123", "ev_def456"]
    },
    {
      "symbol": "process_payment",
      "module": "payments",
      "file": "payments/handler.py",
      "line": 45,
      "evidence_ids": ["ev_ghi789"]
    }
  ]
}
```

Parse JSON output with `jq`:
```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/my-repo \
  find-callers authenticate --limit 5 | jq '.callers[].symbol'
```

#### CSV Output (Spreadsheets)

For analysis in Excel or data pipelines:

```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/my-repo \
  blast-radius process_order --depth 2 --limit 100 | \
  jq -r '.entities[] | [.symbol, .module, .depth] | @csv' > blast-radius.csv
```

Result:
```
"process_order","order_handler","0"
"validate_order","order_handler","1"
"check_inventory","inventory","1"
"charge_card","payments","2"
```

#### Human-Readable Summary

For quick terminal inspection:

```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/my-repo summary
```

Output:
```
Knowledge Graph Summary
=======================
Snapshot ID: my-repo-2026-05-25
Created: 2026-05-25T10:30:00Z
Repository: my-repo
Commit: 9f3e2b1d8c7a6e5f4d3c2b1a0f9e8d7c

Entities (342 total)
  CodeSymbol: 187
  CodeModule: 89
  Service: 8
  Endpoint: 42
  EventChannel: 12
  ExternalPackage: 4

Facts (1847 total)
  CALLS: 1203
  IMPORTS: 401
  HOSTS: 89
  CONSUMES_EVENT: 42
  PRODUCES_EVENT: 12
  (13 other relations)

Evidence: 1847 (all deterministic_static)
Coverage: Instrumented (Python AST v0, TS/JS Parser v1)
```

---

## Part 2: The 8 Standard Query Tools

### 1. find-callers — Who Calls This Function?

**What it does**: Find all functions, methods, or symbols that call a given symbol directly.

**Syntax**:
```bash
python -m source.scripts.query_kg --snapshot <snapshot> find-callers <symbol> \
  [--path <file-path>] \
  [--line <line-number>] \
  [--include-all] \
  [--limit N]
```

**Arguments**:
- `symbol` (required): Qualified symbol name (e.g., `authenticate`, `auth.authenticate`, `PaymentHandler.process`)
- `--path` (optional): File path for disambiguation if symbol exists in multiple files
- `--line` (optional): Line number for exact location disambiguation
- `--include-all` (optional): Include ALL callers, even cross-module ones (default: same-repo only)
- `--limit` (optional): Max results to return (default: 25)

**Example**:

Suppose you have a Flask app with a payment handler function and want to know who calls it:

```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/payment-service \
  find-callers process_charge --limit 5
```

**Sample Output**:

```json
{
  "status": "found",
  "symbol": "process_charge",
  "caller_count": 8,
  "returned_count": 3,
  "callers": [
    {
      "symbol": "handle_order_payment",
      "module": "order_handler",
      "file": "payments/order_handler.py",
      "line": 42,
      "evidence_count": 1,
      "canonical_status": "canonical"
    },
    {
      "symbol": "refund_payment",
      "module": "payment_api",
      "file": "payments/api.py",
      "line": 78,
      "evidence_count": 1,
      "canonical_status": "canonical"
    },
    {
      "symbol": "webhook_handler",
      "module": "webhooks",
      "file": "webhooks.py",
      "line": 105,
      "evidence_count": 1,
      "canonical_status": "canonical"
    }
  ],
  "next_actions": []
}
```

**How to Read It**:

- `status: "found"` — Symbol exists in the KG and has callers
- `caller_count: 8` — Total callers exist (but only 3 shown due to `--limit 5`)
- `returned_count: 3` — 3 results returned (fewer than the limit)
- `callers[]` — Array of caller objects, each with:
  - `symbol` — Name of the calling function
  - `module` — Module containing the caller
  - `file` — Full path to the file
  - `line` — Line number of the call site
  - `evidence_count` — How many call sites found (usually 1)

**Use Case**: 

Refactoring `process_charge()` means updating 8 call sites. But if one of those callers is in a different service, that's a compatibility concern. Query callers to plan the refactoring safely.

**Notes**:

- If a symbol is defined in multiple files, use `--path` and/or `--line` to disambiguate
- Large `caller_count` means high fan-in; changing this function affects many places
- Callers are sorted by file path and line number for stable results
- Cross-module callers are included by default; use `--include-all` to include external packages

**Common Issues**:

| Issue | Solution |
|-------|----------|
| "No results found" but you know it's called | Try with `--include-all` to include callers from other modules |
| Multiple symbols match | Use `--path` or `--line` to disambiguate |
| Only 25 results shown | Increase `--limit` (up to 500) to see more |

---

### 2. find-callees — What Does This Function Call?

**What it does**: Find all functions and methods that a given symbol calls directly.

**Syntax**:
```bash
python -m source.scripts.query_kg --snapshot <snapshot> find-callees <symbol> \
  [--path <file-path>] \
  [--line <line-number>] \
  [--include-all] \
  [--limit N]
```

**Arguments**: Same as `find-callers`.

**Example**:

You're modifying a React component and want to understand its internal dependencies:

```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/frontend \
  find-callees UserProfile --limit 10
```

**Sample Output**:

```json
{
  "status": "found",
  "symbol": "UserProfile",
  "callee_count": 5,
  "returned_count": 5,
  "callees": [
    {
      "symbol": "fetchUserData",
      "module": "api",
      "file": "src/api/user.ts",
      "line": 34,
      "evidence_count": 1,
      "canonical_status": "canonical"
    },
    {
      "symbol": "useUserStore",
      "module": "store",
      "file": "src/store/user.ts",
      "line": 67,
      "evidence_count": 1,
      "canonical_status": "canonical"
    },
    {
      "symbol": "renderUserCard",
      "module": "components",
      "file": "src/components/UserCard.tsx",
      "line": 12,
      "evidence_count": 1,
      "canonical_status": "canonical"
    },
    {
      "symbol": "logEvent",
      "module": "telemetry",
      "file": "src/telemetry.ts",
      "line": 89,
      "evidence_count": 2,
      "canonical_status": "canonical"
    },
    {
      "symbol": "validateEmail",
      "module": "validators",
      "file": "src/validators/email.ts",
      "line": 1,
      "evidence_count": 1,
      "canonical_status": "canonical"
    }
  ],
  "next_actions": []
}
```

**How to Read It**:

- `callee_count: 5` — Total callees (all 5 are shown here)
- `callees[]` — Array of called functions, each with same structure as callers
- Multiple `evidence_count` means the function is called in multiple places within the callee

**Use Case**:

Understanding dependencies before making changes. If `UserProfile` calls `fetchUserData()` and that function is slow, you know why `UserProfile` renders slowly.

**Notes**:

- Useful for understanding tight couplings or circular dependencies
- If a symbol is not found, verify the module path (e.g., `api.user.fetchUserData` vs just `fetchUserData`)
- External packages appear in callees; use `--include-all` to show them explicitly

---

### 3. blast-radius — Full Transitive Impact?

**What it does**: Find all symbols transitively impacted by a change, up to a specified depth.

**Syntax**:
```bash
python -m source.scripts.query_kg --snapshot <snapshot> blast-radius <symbol> \
  [--path <file-path>] \
  [--line <line-number>] \
  [--depth N] \
  [--include-all] \
  [--limit N]
```

**Arguments**:
- `symbol` (required): Entry point symbol
- `--depth` (optional): How many call levels to traverse (1–6, default: 2)
- `--limit` (optional): Max results per depth level (default: 25)
- Other args same as `find-callers`

**Example**:

You're refactoring a critical payment function. What breaks?

```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/payment-service \
  blast-radius charge_card --depth 3 --limit 50
```

**Sample Output**:

```json
{
  "status": "found",
  "symbol": "charge_card",
  "total_impacted": 18,
  "returned_count": 16,
  "by_depth": {
    "1": [
      {
        "symbol": "handle_order",
        "module": "order_handler",
        "file": "handlers.py",
        "line": 42,
        "callers_count": 2
      },
      {
        "symbol": "process_subscription",
        "module": "subscriptions",
        "file": "subscriptions.py",
        "line": 78,
        "callers_count": 1
      }
    ],
    "2": [
      {
        "symbol": "webhook_dispatcher",
        "module": "webhooks",
        "file": "webhooks.py",
        "line": 105,
        "callers_count": 3
      },
      {
        "symbol": "api_endpoint",
        "module": "api",
        "file": "api/routes.py",
        "line": 12,
        "callers_count": 1
      }
    ],
    "3": [
      {
        "symbol": "handle_payment_request",
        "module": "main_handler",
        "file": "main.py",
        "line": 34,
        "callers_count": 2
      }
    ]
  },
  "next_actions": [
    "Verify if any depth-2 or depth-3 callers are in different services.",
    "If cross-service callers exist, ensure backward compatibility."
  ]
}
```

**How to Read It**:

- `total_impacted: 18` — Total functions affected across all depths
- `by_depth{}` — Organized by call distance (depth 1 = direct callers, depth 2 = callers of callers, etc.)
- Depth structure makes it easy to see which functions are closest (most critical to update)

**Use Case**:

**Pre-deployment safety check**. Before deploying a change:
1. Query blast-radius with depth 3
2. Identify if any impacted functions are in other services (cross-service impact)
3. If yes, coordinate deployment or add API versioning
4. If no, it's safe to deploy just this service

**Notes**:

- Depth limit is 6 (prevents infinite loops in circular call structures)
- Each level down exponentially increases result count; use `--limit` to cap results per level
- Blast radius is **static** (call graph only, no runtime paths)
- Does not account for conditional execution or exception handlers

---

### 4. search-services — Find Services by Pattern

**What it does**: Find services matching a search pattern by name, namespace, slug, or repo.

**Syntax**:
```bash
python -m source.scripts.query_kg --snapshot <snapshot> search-services \
  [query] \
  [--limit N]
```

**Arguments**:
- `query` (optional): Search text (matches service name, namespace, slug, repo)
- `--limit` (optional): Max results (default: 25)

**Example 1: Find all auth services**

```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/fleet \
  search-services auth --limit 10
```

**Sample Output**:

```json
{
  "status": "found",
  "query": "auth",
  "returned_count": 4,
  "services": [
    {
      "service_id": "svc_abc123",
      "urn": "supercontext://service/default/auth-service",
      "name": "auth-service",
      "repo": "auth-service",
      "namespace": "default",
      "slug": "auth_service"
    },
    {
      "service_id": "svc_def456",
      "urn": "supercontext://service/default/oauth-provider",
      "name": "oauth-provider",
      "repo": "oauth-service",
      "namespace": "oauth",
      "slug": "oauth_provider"
    },
    {
      "service_id": "svc_ghi789",
      "urn": "supercontext://service/default/user-auth-middleware",
      "name": "user-auth-middleware",
      "repo": "middleware-service",
      "namespace": "middleware",
      "slug": "user_auth_middleware"
    },
    {
      "service_id": "svc_jkl012",
      "urn": "supercontext://service/default/permissions-service",
      "name": "permissions-service",
      "repo": "permissions",
      "namespace": "default",
      "slug": "permissions_service"
    }
  ]
}
```

**Example 2: List all services**

```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/fleet search-services
```

Lists all services in the fleet, sorted by name.

**How to Read It**:

- `name` — Display name of the service
- `repo` — Git repository containing the service
- `namespace` — Logical grouping (e.g., "auth", "payments", "default")
- `slug` — Normalized identifier for programmatic use
- `urn` — Unique identifier (persistent across snapshots)

**Use Case**:

**Service discovery**. In a microservice organization with 50+ services, find related services:
- Search for "auth" to find all authentication-related services
- Search for "payment" to find payment systems
- Use the URN to get more details with `get-service-brief`

**Notes**:

- Search is case-insensitive substring match (not regex)
- Searches across name, namespace, slug, and repo fields
- Results sorted alphabetically by name

---

### 5. get-service-brief — What Does This Service Do?

**What it does**: Get comprehensive information about a service, including endpoints it exposes, events it produces/consumes, and what depends on it.

**Syntax**:
```bash
python -m source.scripts.query_kg --snapshot <snapshot> get-service-brief <service> \
  [--limit N]
```

**Arguments**:
- `service` (required): Service name, slug, or repo (matched against search-services)
- `--limit` (optional): Max results per category (default: 25)

**Example**:

```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/fleet \
  get-service-brief auth-service --limit 10
```

**Sample Output**:

```json
{
  "status": "found",
  "service": {
    "service_id": "svc_abc123",
    "urn": "supercontext://service/default/auth-service",
    "name": "auth-service",
    "repo": "auth-service",
    "namespace": "default"
  },
  "summary": {
    "endpoint_fact_count": 6,
    "event_fact_count": 4,
    "deploy_mapping_count": 2,
    "endpoint_consumer_fact_count": 8
  },
  "endpoints": [
    {
      "endpoint_id": "ep_001",
      "path": "POST /api/v1/auth/login",
      "protocol": "http",
      "source": "flask_routes_v0"
    },
    {
      "endpoint_id": "ep_002",
      "path": "POST /api/v1/auth/logout",
      "protocol": "http",
      "source": "flask_routes_v0"
    },
    {
      "endpoint_id": "ep_003",
      "path": "POST /api/v1/auth/refresh",
      "protocol": "http",
      "source": "flask_routes_v0"
    }
  ],
  "event_channels": [
    {
      "channel_id": "ev_001",
      "name": "user.authenticated",
      "broker_kind": "kafka",
      "predicate": "PRODUCES_EVENT"
    },
    {
      "channel_id": "ev_002",
      "name": "user.revoked",
      "broker_kind": "kafka",
      "predicate": "PRODUCES_EVENT"
    }
  ],
  "deploy_mappings": [
    {
      "mapping_id": "dm_001",
      "target": "prod-auth-1",
      "config_path": "k8s/auth-service.yaml"
    },
    {
      "mapping_id": "dm_002",
      "target": "staging-auth-1",
      "config_path": "k8s/auth-service-staging.yaml"
    }
  ],
  "endpoint_consumers": {
    "summary": {
      "consumer_fact_count": 8,
      "consumer_service_count": 4
    },
    "by_service": [
      {
        "service_name": "api-gateway",
        "consumer_count": 3,
        "endpoints": [
          "POST /api/v1/auth/login",
          "POST /api/v1/auth/refresh"
        ]
      },
      {
        "service_name": "user-service",
        "consumer_count": 2,
        "endpoints": ["POST /api/v1/auth/login"]
      },
      {
        "service_name": "admin-panel",
        "consumer_count": 2,
        "endpoints": ["POST /api/v1/auth/logout"]
      },
      {
        "service_name": "audit-service",
        "consumer_count": 1,
        "endpoints": ["POST /api/v1/auth/login"]
      }
    ]
  },
  "answerability": {
    "status": "partial",
    "missing_fact_families": [],
    "recommended_followups": [
      "endpoint_consumers are static path-matched CALLS_ENDPOINT facts; verify host/env resolution before treating them as runtime dependencies."
    ]
  },
  "next_actions": [
    "Use auth-service endpoints to understand API contract.",
    "Check event channels for async coupling points.",
    "Verify endpoint_consumers; these are static call-sites, not runtime metrics."
  ]
}
```

**How to Read It**:

- `service` — Basic service info (name, repo, namespace)
- `summary` — Counts of endpoints, events, deploy mappings
- `endpoints[]` — HTTP/gRPC routes this service exposes
- `event_channels[]` — Event topics it produces or consumes
- `deploy_mappings[]` — How it's deployed (k8s, Lambda, VMs, etc.)
- `endpoint_consumers` — Which services call which endpoints
  - `consumer_service_count: 4` — 4 services depend on this service
  - `by_service[]` — Breakdown by consuming service
- `answerability` — Coverage status; whether the answer is complete
- `next_actions` — Recommended follow-up actions

**Use Case**:

**Understanding a service before modifying it**. New engineer asks: "What does auth-service do?" Answer: Query `get-service-brief`. Result shows:
1. 3 endpoints (login, logout, refresh)
2. 2 event channels it produces (authenticated, revoked)
3. 4 other services depend on it
4. 2 deploy targets (prod and staging)

This is enough context to plan a safe change.

**Notes**:

- `endpoint_consumers` shows static call-sites; not runtime metrics
- `answerability: "partial"` means coverage gaps exist (e.g., no deploy-mapping facts)
- Use `next_actions` as guidance for verification steps
- If service query is ambiguous (multiple matches), returns `status: "ambiguous"` with candidate list

---

### 6. get-event-consumers — Who Subscribes to This Event?

**What it does**: Find all services (or handlers) that consume a given event channel.

**Syntax**:
```bash
python -m source.scripts.query_kg --snapshot <snapshot> get-event-consumers <channel> \
  [--limit N]
```

**Arguments**:
- `channel` (required): Event channel name or substring (e.g., `user.authenticated`, `orders`, `payment`)
- `--limit` (optional): Max results (default: 25)

**Example**:

You're changing the `user.authenticated` event schema. Who breaks?

```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/fleet \
  get-event-consumers user.authenticated --limit 20
```

**Sample Output**:

```json
{
  "status": "found",
  "channel": "user.authenticated",
  "event_fact_count": 4,
  "returned_count": 4,
  "consumers": [
    {
      "subject_service": "api-gateway",
      "subject_id": "svc_123",
      "event_channel": "user.authenticated",
      "broker_kind": "kafka",
      "handler_name": "on_user_authenticated",
      "handler_file": "handlers/auth.py",
      "handler_line": 34,
      "evidence_count": 1
    },
    {
      "subject_service": "audit-service",
      "subject_id": "svc_456",
      "event_channel": "user.authenticated",
      "broker_kind": "kafka",
      "handler_name": "log_auth_event",
      "handler_file": "audit/event_handler.py",
      "handler_line": 67,
      "evidence_count": 1
    },
    {
      "subject_service": "analytics-service",
      "subject_id": "svc_789",
      "event_channel": "user.authenticated",
      "broker_kind": "kafka",
      "handler_name": "track_login",
      "handler_file": "analytics/tracker.py",
      "handler_line": 102,
      "evidence_count": 1
    },
    {
      "subject_service": "user-profile-service",
      "subject_id": "svc_012",
      "event_channel": "user.authenticated",
      "broker_kind": "kafka",
      "handler_name": "init_user_cache",
      "handler_file": "profile/cache.py",
      "handler_line": 145,
      "evidence_count": 1
    }
  ],
  "answerability": {
    "status": "answerable",
    "scope": "indexed static event-channel facts only",
    "missing_fact_families": [],
    "cannot_prove": [
      "whether messages were published or consumed in a time window",
      "whether there are runtime-only subscribers outside indexed source/config",
      "whether a zero-consumer result means the event channel is unused"
    ]
  },
  "next_actions": [
    "Inspect source/config around returned event evidence before finalizing schema, handler, retry, or delivery-semantics claims.",
    "For runtime claims such as `no consumers in the last 30 days`, inspect broker metrics, traces, logs, or deployment config."
  ]
}
```

**How to Read It**:

- `event_fact_count: 4` — Total consumers found (all 4 are shown here)
- `consumers[]` — Array of consumer facts, each with:
  - `subject_service` — Service consuming the event
  - `handler_name` — Function/class handling the event
  - `handler_file` — Source location of the handler
  - `broker_kind` — Message broker type (Kafka, RabbitMQ, SQS, etc.)

**Use Case**:

**Event-driven impact analysis**. Before changing an event schema:
1. Query `get-event-consumers` for the event
2. Get list of 4 services consuming it
3. Coordinate with those services or add a schema migration

**Notes**:

- Searches are substring matches (case-insensitive) so `orders` matches `order.created`, `order.updated`, etc.
- Results are **static** (from source code and config); not runtime metrics
- If no consumers found, the channel may be unused or produced-only
- Broker kind helps you understand infrastructure (Kafka vs SQS timing guarantees differ)

---

### 7. get-event-producers — Who Publishes to This Event?

**What it does**: Find all services (or code paths) that produce a given event channel.

**Syntax**:
```bash
python -m source.scripts.query_kg --snapshot <snapshot> get-event-producers <channel> \
  [--limit N]
```

**Arguments**:
- `channel` (required): Event channel name or substring
- `--limit` (optional): Max results (default: 25)

**Example**:

You're adding a consumer for `order.created`. Where does this event come from?

```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/fleet \
  get-event-producers order.created --limit 10
```

**Sample Output**:

```json
{
  "status": "found",
  "channel": "order.created",
  "event_fact_count": 1,
  "returned_count": 1,
  "producers": [
    {
      "subject_service": "order-service",
      "subject_id": "svc_order",
      "event_channel": "order.created",
      "broker_kind": "kafka",
      "producer_name": "emit_order_created",
      "producer_file": "orders/events.py",
      "producer_line": 78,
      "evidence_count": 1,
      "schema_ref": {
        "topic": "order.created",
        "version": "1.2"
      }
    }
  ],
  "answerability": {
    "status": "answerable",
    "scope": "indexed static event-channel facts only",
    "missing_fact_families": [],
    "cannot_prove": [
      "whether messages were actually published at runtime",
      "message latency or delivery guarantees",
      "exception handling or dead-letter queuing"
    ]
  },
  "next_actions": [
    "Inspect the producer source code to understand event payload.",
    "Verify message schema version before consuming."
  ]
}
```

**How to Read It**:

- `event_fact_count: 1` — One producer found (typical for well-designed events)
- `producers[]` — Array of producers (usually 1–3)
- `schema_ref` — Event schema location/version if available

**Use Case**:

**Understanding event flow**. When adding a new consumer:
1. Query `get-event-producers` to find the producer
2. Read its source code to understand the event payload
3. Verify schema version compatibility
4. Test the consumer against actual producer output

**Notes**:

- Each event channel should have 1–2 producers (multiple producers on same channel is a smell)
- Producers are identified from source/config code, not runtime
- `schema_ref` helps coordinate schema versions between producer and consumers

---

### 8. deploy-blockers-for — What Prevents Deployment?

**What it does**: Identify deploy blockers for a service (missing dependencies, broken endpoints, etc.).

**Syntax**:
```bash
python -m source.scripts.query_kg --snapshot <snapshot> deploy-blockers-for <service>
```

**Arguments**:
- `service` (required): Service name, slug, or repo

**Example**:

```bash
python -m source.scripts.query_kg --snapshot data/kg_runs/fleet \
  deploy-blockers-for api-gateway
```

**Sample Output** (when blockers exist):

```json
{
  "status": "found",
  "service": "api-gateway",
  "blocker_count": 2,
  "blockers": [
    {
      "blocker_type": "missing_upstream_service",
      "description": "Service api-gateway depends on auth-service, but auth-service has no deploy config in k8s/",
      "severity": "high",
      "remediation": "Add deploy mapping for auth-service or verify it's deployed to all target environments."
    },
    {
      "blocker_type": "unused_endpoint",
      "description": "Endpoint POST /api/v1/deprecated-route has no consumers in this snapshot. Safe to remove.",
      "severity": "low",
      "remediation": "Remove the endpoint definition or add a deprecation notice."
    }
  ]
}
```

**How to Read It**:

- `blocker_count` — Number of blockers found
- `blockers[]` — Array of blockers, each with:
  - `blocker_type` — Kind of blocker (missing service, broken endpoint, etc.)
  - `severity` — High, medium, or low
  - `remediation` — How to fix it

**Use Case**:

**Pre-deployment checks**. Before deploying:
1. Query `deploy-blockers-for <service>`
2. If `status: "found"`, address each blocker
3. Re-query after fixes to confirm they're resolved
4. Proceed with deployment

**Notes**:

- This is the only tool requiring **explicit config** (deploy mappings must be indexed)
- Many repos don't have deploy-blocker data yet
- If unsupported: `status: "unsupported_by_current_kg"` with next steps

---

## Part 3: Writing Custom Queries

Beyond the 8 standard tools, SuperContext supports a custom query language for complex patterns.

### Query Language Basics

**Structure**: Queries follow a simple pattern:

```
FIND <entity-pattern> WHERE <conditions> RETURN <fields>
```

**Example 1: Find all functions calling `process_charge`**

```
FIND Symbol WHERE calls("process_charge") RETURN name, module, file, line
```

This is equivalent to `find-callers process_charge` but shows the underlying query language.

**Example 2: Find all services in the "payments" namespace**

```
FIND Service WHERE namespace="payments" RETURN name, repo, slug
```

**Example 3: Traverse dependencies**

```
FIND Symbol WHERE calls("charge_card") TRAVERSE callers:5 RETURN name, depth
```

"Find `charge_card`, then traverse up the call stack for 5 levels, returning each symbol and its depth."

### Real Example: Find All Services Transitively Depending on a Library

**Scenario**: You're upgrading `requests` library to v3.0 (breaking changes). Which services are affected?

**Query**:

```
FIND Service 
WHERE imports("requests") OR transitively_depends_on_service_importing("requests")
RETURN name, repo, endpoint_count
```

**Explanation**:
- `FIND Service` — Look for Service entities
- `WHERE imports("requests")` — Services that directly import requests
- `OR transitively_depends_on_service_importing("requests")` — OR services that depend on services that import requests
- `RETURN name, repo, endpoint_count` — Return service name, repo, and count of endpoints (to understand blast radius)

### Testing Custom Queries

Test a query against a snapshot:

```bash
# Run a query through the CLI
python -m source.scripts.query_kg --snapshot data/kg_runs/fleet \
  custom-query "FIND Service WHERE namespace='payments' RETURN name"
```

**Iterate**:
1. Start with a simple query (one condition)
2. Check results
3. Refine by adding more conditions
4. Use `--limit` to cap results while testing

---

## Part 4: Performance & Optimization

### Query Time Complexity

| Query Tool | Time Complexity | Notes |
|------------|-----------------|-------|
| `find-callers` | O(E) | Scan all facts for matching calls |
| `find-callees` | O(E) | Similar to find-callers |
| `blast-radius` | O(E × depth) | Exponential in depth; limit to depth 3–4 for large graphs |
| `search-services` | O(E) | Full text search with filtering |
| `get-service-brief` | O(E) | Collects all facts touching a service |
| `get-event-consumers` | O(E) | Scan all facts for event matches |
| `get-event-producers` | O(E) | Similar to consumers |
| `deploy-blockers-for` | O(E) | Depends on config indexed |

**E** = number of facts in the snapshot (typically 1K–100K)

### Performance Tips

**1. Limit Depth on Blast Radius**

Depth 2 covers most use cases (direct callers + their callers). Depth 3+ is exponential:

```bash
# Good: Fast, usually sufficient
python -m source.scripts.query_kg --snapshot snapshot/ \
  blast-radius symbol --depth 2 --limit 25

# Slow: Covers many levels but may take seconds on large graphs
python -m source.scripts.query_kg --snapshot snapshot/ \
  blast-radius symbol --depth 5 --limit 100
```

**2. Use --limit Flag Aggressively**

```bash
# Get top 10 callers (fastest)
python -m source.scripts.query_kg --snapshot snapshot/ \
  find-callers symbol --limit 10

# Get all callers (slower)
python -m source.scripts.query_kg --snapshot snapshot/ \
  find-callers symbol --limit 10000
```

**3. Filter by Derivation Class** (if needed)

If a snapshot has mixed canonical and candidate facts:

```bash
python -m source.scripts.query_kg --snapshot snapshot/ \
  find-callers symbol --include-all
```

The `--include-all` flag includes both canonical and candidate facts. Omit it to query only canonical (faster, higher confidence).

**4. Parse JSON Efficiently**

For large result sets, use `jq` to extract only needed fields:

```bash
# Slow: Process entire JSON object
python -m source.scripts.query_kg --snapshot snapshot/ \
  blast-radius symbol --depth 3 --limit 100 | wc -l

# Fast: Extract only symbol names
python -m source.scripts.query_kg --snapshot snapshot/ \
  blast-radius symbol --depth 3 --limit 100 | \
  jq '.by_depth[] | .[] | .symbol' | wc -l
```

**5. Leverage Snapshot Caching**

Once a snapshot is built, queries are fast because the snapshot is immutable and in-memory. Build snapshots once and query them many times:

```bash
# Build once (slow: minutes)
python -m source.scripts.build_kg --repo /path/to/repo --out data/kg_runs/my-repo

# Query many times (fast: milliseconds)
python -m source.scripts.query_kg --snapshot data/kg_runs/my-repo find-callers symbol --limit 5
python -m source.scripts.query_kg --snapshot data/kg_runs/my-repo find-callers symbol2 --limit 5
python -m source.scripts.query_kg --snapshot data/kg_runs/my-repo blast-radius symbol3 --depth 2
```

### Caching Behavior

- **Snapshots are immutable**: Once built, they never change
- **Queries are deterministic**: Same query on same snapshot always returns same results
- **In-memory cache**: Query results are cached within the current session
- **Cache cleared on rebuild**: Running `build_kg` again creates a new snapshot with a different cache

---

## Part 5: Troubleshooting

### "No results found"

**Symptom**: Query returns `status: "not_found"` but you're sure the symbol/service exists.

**Causes & Solutions**:

| Cause | Solution |
|-------|----------|
| Wrong symbol name | Verify in source code; search is case-sensitive |
| Fully qualified name needed | Try with module prefix: `module.symbol` instead of just `symbol` |
| Symbol not extracted | Check `coverage.jsonl` to see if the symbol's language/framework is instrumented |
| Symbol in external package | Use `--include-all` to search external packages |
| Snapshot outdated | Rebuild with `build_kg` if code changed since snapshot was created |

**Example**:

```bash
# Search fails
python -m source.scripts.query_kg --snapshot snapshot/ find-callers myfunction
# Returns: {"status": "not_found", "symbol": "myfunction"}

# Try with module prefix
python -m source.scripts.query_kg --snapshot snapshot/ find-callers auth.myfunction
# Returns: {"status": "found", ...}

# Or verify it's instrumented
python -m source.scripts.query_kg --snapshot snapshot/ \
  coverage-metrics | jq '.coverage[] | select(.scope_ref.language=="python")'
```

### "Query timed out"

**Symptom**: Query takes >30 seconds or is killed.

**Causes & Solutions**:

| Cause | Solution |
|-------|----------|
| Depth too high | Reduce `--depth` (try 2 instead of 5) |
| Limit too high | Reduce `--limit` (try 25 instead of 100) |
| Very large snapshot | Use smaller snapshot or filter by repo/module |
| Expensive traversal | Avoid `--include-all` if not needed |

**Example**:

```bash
# Slow: Deep traversal with no limit
python -m source.scripts.query_kg --snapshot snapshot/ \
  blast-radius symbol --depth 6 --limit 1000

# Fast: Shallow traversal with limit
python -m source.scripts.query_kg --snapshot snapshot/ \
  blast-radius symbol --depth 2 --limit 25
```

### "Permission denied" or "File not found"

**Symptom**: Error accessing snapshot directory or files.

**Causes & Solutions**:

| Cause | Solution |
|-------|----------|
| Wrong snapshot path | Verify path exists: `ls data/kg_runs/<snapshot>/entities.jsonl` |
| Missing read permissions | Ensure you can read snapshot files: `ls -la data/kg_runs/<snapshot>/` |
| Snapshot partially built | Check if all 5 files exist: entities.jsonl, facts.jsonl, evidence.jsonl, coverage.jsonl, manifest.json |

**Example**:

```bash
# Verify snapshot exists
ls -la data/kg_runs/my-repo/
# Output:
# -rw-r--r-- 1 user staff 12345 May 25 10:30 entities.jsonl
# -rw-r--r-- 1 user staff 23456 May 25 10:30 facts.jsonl
# -rw-r--r-- 1 user staff 34567 May 25 10:30 evidence.jsonl
# -rw-r--r-- 1 user staff  1234 May 25 10:30 coverage.jsonl
# -rw-r--r-- 1 user staff   456 May 25 10:30 manifest.json

# If files are missing, rebuild
python -m source.scripts.build_kg --repo /path/to/repo --out data/kg_runs/my-repo
```

### "Ambiguous symbol" or "Multiple matches"

**Symptom**: Symbol exists in multiple files; query doesn't know which one you mean.

**Solution**: Use `--path` and/or `--line` to disambiguate:

```bash
# Ambiguous: authenticate exists in auth.py and oauth.py
python -m source.scripts.query_kg --snapshot snapshot/ \
  find-callers authenticate

# Disambiguate by file
python -m source.scripts.query_kg --snapshot snapshot/ \
  find-callers authenticate --path auth.py

# Disambiguate by file and line
python -m source.scripts.query_kg --snapshot snapshot/ \
  find-callers authenticate --path auth.py --line 42
```

---

## Cross-References & Further Reading

**Using queries in practice**: [query-your-repo.md](../03-workflows/query-your-repo.md) — Workflow guide for querying your first repository

**Building a knowledge graph**: [knowledge-graph.md](./knowledge-graph.md) — How to extract a KG from your code

**Understanding evidence**: [evidence-retrieval.md](../../evidence-retrieval/EVIDENCE-RETRIEVAL-RECOMMENDATION.md) — ADR-0005 evidence backing for query results

**Full architecture**: 
- [ADR-0009: Deterministic Reverse Dependency Queries](../../../adr/0009-deterministic-reverse-dependency-queries-with-agentic-candidate-enrichment.md)
- [ADR-0002: MCP Protocol for External Surface](../../../adr/0002-mcp-protocol-for-external-surface.md)

**Query examples**: [examples/02-query/](../examples/02-query/) — Real example queries and outputs

---

**Last updated**: 2026-05-25
