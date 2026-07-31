#!/bin/bash

###############################################################################
# Start SuperContext MCP Server
#
# This script starts the local MCP (Model Context Protocol) server so you can
# connect an IDE or client to query the knowledge graph.
#
# Usage:
#   bash start-mcp-server.sh                    # Start with default snapshot
#   bash start-mcp-server.sh data/kg_runs/myrepo # Start with specific snapshot
#   bash start-mcp-server.sh --build ./flask    # Build Flask snapshot then start
#
# The server listens on localhost:3845 by default.
#
# To connect from Claude Code:
#   1. Run this script
#   2. Note the server URL
#   3. Add to Claude Code MCP settings:
#      "supercontext": { "url": "http://localhost:3845" }
#   4. Use SuperContext tools in Claude: find-callers, get_service_brief, etc.
###############################################################################

set -e

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
DEFAULT_SNAPSHOT="${PROJECT_ROOT}/data/kg_runs/flask"
BUILD_REPO=""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# ─────────────────────────────────────────────────────────────────────────────
# Functions
# ─────────────────────────────────────────────────────────────────────────────

log_info() {
    echo -e "${BLUE}ℹ${NC} $1"
}

log_success() {
    echo -e "${GREEN}✓${NC} $1"
}

log_error() {
    echo -e "${RED}✗${NC} $1" >&2
}

log_step() {
    echo -e "\n${BLUE}─────────────────────────────────────────${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}─────────────────────────────────────────${NC}\n"
}

check_python() {
    if ! command -v python3 &> /dev/null; then
        log_error "Python 3 not found. Please install Python 3.11+"
        exit 1
    fi

    PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
    log_info "Python version: $PYTHON_VERSION"
}

check_project() {
    if [[ ! -f "${PROJECT_ROOT}/pyproject.toml" ]]; then
        log_error "Project root not found at ${PROJECT_ROOT}"
        log_error "This script expects to run from within the SuperContext repo"
        exit 1
    fi
    log_success "Project root: ${PROJECT_ROOT}"
}

build_snapshot() {
    local repo_path="$1"
    local snapshot_name=$(basename "$repo_path")
    local output_dir="${PROJECT_ROOT}/data/kg_runs/${snapshot_name}"

    log_step "Building knowledge graph snapshot"
    log_info "Repository: $repo_path"
    log_info "Output: $output_dir"

    mkdir -p "${output_dir}"

    if ! python3 -m source.scripts.build_kg \
        --repo "$repo_path" \
        --out "$output_dir"; then
        log_error "Failed to build knowledge graph"
        exit 1
    fi

    log_success "Snapshot built: $output_dir"
    echo "$output_dir"
}

verify_snapshot() {
    local snapshot_path="$1"

    if [[ ! -d "$snapshot_path" ]]; then
        log_error "Snapshot not found at ${snapshot_path}"
        return 1
    fi

    if [[ ! -f "${snapshot_path}/entities.jsonl" ]]; then
        log_error "Missing entities.jsonl in snapshot"
        return 1
    fi

    if [[ ! -f "${snapshot_path}/facts.jsonl" ]]; then
        log_error "Missing facts.jsonl in snapshot"
        return 1
    fi

    # Count entities and facts
    local entity_count=$(wc -l < "${snapshot_path}/entities.jsonl")
    local fact_count=$(wc -l < "${snapshot_path}/facts.jsonl")

    log_success "Snapshot verified"
    log_info "  Entities: $entity_count"
    log_info "  Facts: $fact_count"

    return 0
}

start_server() {
    local snapshot_path="$1"
    local port="${2:-3845}"

    log_step "Starting MCP Server"
    log_info "Snapshot: $snapshot_path"
    log_info "Port: $port"

    # Start the MCP server process
    if command -v supercontext-mcp-server &> /dev/null; then
        log_info "Starting MCP server..."
        supercontext-mcp-server --snapshot "$snapshot_path" --port "$port"
    else
        log_error "supercontext-mcp-server not found. Please install SuperContext first:"
        log_error "  pip install -e ."
        return 1
    fi
}

print_usage() {
    cat <<EOF
Usage: $0 [OPTIONS]

Options:
    --build REPO    Build snapshot from REPO before starting
    --snapshot DIR  Use snapshot at DIR (default: ${DEFAULT_SNAPSHOT})
    --port N        Listen on port N (default: 3845)
    --help          Show this help message

Examples:
    # Start with default Flask snapshot
    bash start-mcp-server.sh

    # Build snapshot from a repo then start
    bash start-mcp-server.sh --build ./flask

    # Use a specific snapshot and custom port
    bash start-mcp-server.sh --snapshot data/kg_runs/myrepo --port 9000

    # Build multiple repos into one snapshot
    # (First run these commands separately:)
    # python3 -m source.scripts.build_kg --repo ./flask --out data/kg_runs/multi
    # python3 -m source.scripts.build_kg --repo ./react --out data/kg_runs/multi --append
    # Then start:
    bash start-mcp-server.sh --snapshot data/kg_runs/multi
EOF
}

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

main() {
    local snapshot_path="$DEFAULT_SNAPSHOT"
    local port="3845"

    # Parse arguments
    while [[ $# -gt 0 ]]; do
        case $1 in
            --build)
                BUILD_REPO="$2"
                shift 2
                ;;
            --snapshot)
                snapshot_path="$2"
                shift 2
                ;;
            --port)
                port="$2"
                shift 2
                ;;
            --help|-h)
                print_usage
                exit 0
                ;;
            *)
                log_error "Unknown option: $1"
                print_usage
                exit 1
                ;;
        esac
    done

    # Preflight checks
    log_step "Preflight Checks"
    check_python
    check_project

    # Build snapshot if requested
    if [[ -n "$BUILD_REPO" ]]; then
        snapshot_path=$(build_snapshot "$BUILD_REPO")
    fi

    # Verify snapshot exists
    if ! verify_snapshot "$snapshot_path"; then
        log_error "Snapshot verification failed"
        log_info "Build a snapshot first:"
        log_info "  bash examples/01-build/build-kg-single-repo.sh"
        exit 1
    fi

    # Start the server
    start_server "$snapshot_path" "$port"

    log_step "Server Ready"
    log_success "MCP server started on http://localhost:${port}"
    echo ""
    echo "Next steps:"
    echo "  1. In Claude Code, open Settings → MCP Servers"
    echo "  2. Add SuperContext server at http://localhost:${port}"
    echo "  3. Use SuperContext tools to query the knowledge graph"
    echo ""
    echo "Available tools:"
    echo "  • search_services - Find services by name"
    echo "  • get_service_brief - Service details and relations"
    echo "  • find_callers - Reverse dependency impact"
    echo "  • find_callees - Downstream dependencies"
    echo "  • get_event_consumers - Who consumes an event"
    echo "  • get_event_producers - Who publishes an event"
    echo "  • blast_radius - Full transitive call impact"
    echo "  • deploy_blockers_for - Deployment dependencies"
    echo ""
    echo "To stop the server, press Ctrl+C"
}

# ─────────────────────────────────────────────────────────────────────────────

main "$@"
