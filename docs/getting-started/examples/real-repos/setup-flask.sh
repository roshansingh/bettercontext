#!/bin/bash

###############################################################################
# Setup Flask Example Repository
#
# This script clones the Flask repository (if needed) and builds a SuperContext
# knowledge graph from it. Flask is a good example because it has:
#   - Clear module structure
#   - Well-defined routes
#   - Decorators and patterns
#
# Usage:
#   bash setup-flask.sh
#   bash setup-flask.sh --force     # Re-clone even if exists
#   bash setup-flask.sh --skip-build # Clone only, don't build KG
#
# Output:
#   ./flask/                      - Flask repository clone
#   data/kg_runs/flask/           - SuperContext snapshot
#
# Time: ~30-60 seconds (including network download)
###############################################################################

set -e

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
EXAMPLES_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FLASK_REPO_URL="https://github.com/pallets/flask.git"
FLASK_DIR="${EXAMPLES_ROOT}/real-repos/flask"
SNAPSHOT_DIR="${PROJECT_ROOT}/data/kg_runs/flask"

FORCE_CLONE=0
SKIP_BUILD=0

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

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
        log_error "Python 3 not found"
        exit 1
    fi
    log_success "Python 3 available"
}

check_git() {
    if ! command -v git &> /dev/null; then
        log_error "Git not found"
        exit 1
    fi
    log_success "Git available"
}

clone_flask() {
    log_step "Cloning Flask Repository"

    if [[ -d "$FLASK_DIR" ]] && [[ $FORCE_CLONE -eq 0 ]]; then
        log_info "Flask already cloned at $FLASK_DIR"
        return
    fi

    if [[ -d "$FLASK_DIR" ]] && [[ $FORCE_CLONE -eq 1 ]]; then
        log_info "Removing existing Flask directory..."
        rm -rf "$FLASK_DIR"
    fi

    log_info "Cloning Flask repository..."
    git clone --depth 1 "$FLASK_REPO_URL" "$FLASK_DIR"

    log_success "Flask cloned to $FLASK_DIR"
    log_info "Repository info:"
    log_info "  URL: $FLASK_REPO_URL"
    log_info "  Location: $FLASK_DIR"

    if [[ -f "$FLASK_DIR/setup.py" ]] || [[ -f "$FLASK_DIR/pyproject.toml" ]]; then
        log_success "Project setup files found"
    fi
}

build_snapshot() {
    log_step "Building SuperContext Knowledge Graph"

    if [[ ! -d "$FLASK_DIR" ]]; then
        log_error "Flask directory not found at $FLASK_DIR"
        return 1
    fi

    log_info "Input: $FLASK_DIR"
    log_info "Output: $SNAPSHOT_DIR"

    mkdir -p "$SNAPSHOT_DIR"

    # Run the KG builder
    if ! python3 -m source.scripts.build_kg \
        --repo "$FLASK_DIR" \
        --out "$SNAPSHOT_DIR"; then
        log_error "Failed to build knowledge graph"
        return 1
    fi

    log_success "Knowledge graph built"
    return 0
}

verify_snapshot() {
    log_step "Verifying Snapshot"

    if [[ ! -f "${SNAPSHOT_DIR}/entities.jsonl" ]]; then
        log_error "Missing entities.jsonl"
        return 1
    fi

    if [[ ! -f "${SNAPSHOT_DIR}/facts.jsonl" ]]; then
        log_error "Missing facts.jsonl"
        return 1
    fi

    # Count records
    local entity_count=$(wc -l < "${SNAPSHOT_DIR}/entities.jsonl")
    local fact_count=$(wc -l < "${SNAPSHOT_DIR}/facts.jsonl")

    log_success "Snapshot verified"
    log_info "  Entities: $entity_count"
    log_info "  Facts: $fact_count"

    if [[ -f "${SNAPSHOT_DIR}/evidence.jsonl" ]]; then
        local evidence_count=$(wc -l < "${SNAPSHOT_DIR}/evidence.jsonl")
        log_info "  Evidence: $evidence_count"
    fi

    return 0
}

print_next_steps() {
    cat <<EOF

${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}
${BLUE}Setup Complete!${NC}
${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}

Flask repository: ${FLASK_DIR}
Knowledge graph: ${SNAPSHOT_DIR}

${BLUE}Next Steps:${NC}

1. ${YELLOW}Query the snapshot:${NC}
   python3 -m source.scripts.query_kg \\
     --snapshot ${SNAPSHOT_DIR} \\
     summary

2. ${YELLOW}Find specific patterns:${NC}
   python3 -m source.scripts.query_kg \\
     --snapshot ${SNAPSHOT_DIR} \\
     find-callers "request" --limit 5

3. ${YELLOW}Start the MCP server:${NC}
   bash examples/05-mcp/start-mcp-server.sh \\
     --snapshot ${SNAPSHOT_DIR}

4. ${YELLOW}Analyze code impact:${NC}
   python3 examples/02-query/find-impact.py \\
     "Flask.request" ${SNAPSHOT_DIR}

5. ${YELLOW}Run a custom extractor:${NC}
   python3 examples/04-extend/flask-routes-extractor.py \\
     ${FLASK_DIR}

${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}
EOF
}

print_usage() {
    cat <<EOF
Usage: $0 [OPTIONS]

Options:
    --force         Re-clone Flask (overwrite existing)
    --skip-build    Clone only, don't build knowledge graph
    --help          Show this help

Examples:
    bash setup-flask.sh              # Normal setup
    bash setup-flask.sh --force      # Force re-clone
    bash setup-flask.sh --skip-build # Just clone, no KG build
EOF
}

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

main() {
    # Parse arguments
    while [[ $# -gt 0 ]]; do
        case $1 in
            --force)
                FORCE_CLONE=1
                shift
                ;;
            --skip-build)
                SKIP_BUILD=1
                shift
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

    log_step "Flask Example Setup"

    # Preflight checks
    log_info "Running preflight checks..."
    check_python
    check_git

    # Clone Flask
    clone_flask

    # Build snapshot
    if [[ $SKIP_BUILD -eq 0 ]]; then
        if build_snapshot; then
            verify_snapshot
        else
            log_error "Snapshot build failed"
            exit 1
        fi
    fi

    # Show next steps
    print_next_steps

    return 0
}

main "$@"
