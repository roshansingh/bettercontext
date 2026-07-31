#!/bin/bash

###############################################################################
# Setup React Example Repository
#
# This script clones the React repository (if needed) and builds a SuperContext
# knowledge graph from it. React is a good example for JavaScript/TypeScript
# analysis because it has:
#   - Complex module structure
#   - Hook patterns
#   - Component composition
#   - Export/import relationships
#
# Usage:
#   bash setup-react.sh
#   bash setup-react.sh --force     # Re-clone even if exists
#   bash setup-react.sh --skip-build # Clone only, don't build KG
#
# Output:
#   ./react/                      - React repository clone
#   data/kg_runs/react/           - SuperContext snapshot
#
# Time: ~30-60 seconds (including network download)
###############################################################################

set -e

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
EXAMPLES_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REACT_REPO_URL="https://github.com/facebook/react.git"
REACT_DIR="${EXAMPLES_ROOT}/real-repos/react"
SNAPSHOT_DIR="${PROJECT_ROOT}/data/kg_runs/react"

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

clone_react() {
    log_step "Cloning React Repository"

    if [[ -d "$REACT_DIR" ]] && [[ $FORCE_CLONE -eq 0 ]]; then
        log_info "React already cloned at $REACT_DIR"
        return
    fi

    if [[ -d "$REACT_DIR" ]] && [[ $FORCE_CLONE -eq 1 ]]; then
        log_info "Removing existing React directory..."
        rm -rf "$REACT_DIR"
    fi

    log_info "Cloning React repository..."
    git clone --depth 1 "$REACT_REPO_URL" "$REACT_DIR"

    log_success "React cloned to $REACT_DIR"
    log_info "Repository info:"
    log_info "  URL: $REACT_REPO_URL"
    log_info "  Location: $REACT_DIR"

    if [[ -f "$REACT_DIR/package.json" ]]; then
        log_success "Node.js/TypeScript project detected"
        # Show first few lines of package.json
        if command -v head &> /dev/null; then
            log_info "  Package info:"
            head -3 "$REACT_DIR/package.json" | sed 's/^/    /'
        fi
    fi
}

build_snapshot() {
    log_step "Building SuperContext Knowledge Graph"

    if [[ ! -d "$REACT_DIR" ]]; then
        log_error "React directory not found at $REACT_DIR"
        return 1
    fi

    log_info "Input: $REACT_DIR"
    log_info "Output: $SNAPSHOT_DIR"

    mkdir -p "$SNAPSHOT_DIR"

    # Run the KG builder
    if ! python3 -m source.scripts.build_kg \
        --repo "$REACT_DIR" \
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

React repository: ${REACT_DIR}
Knowledge graph: ${SNAPSHOT_DIR}

${BLUE}Next Steps:${NC}

1. ${YELLOW}Query the snapshot:${NC}
   python3 -m source.scripts.query_kg \\
     --snapshot ${SNAPSHOT_DIR} \\
     summary

2. ${YELLOW}Find specific patterns (JavaScript/TypeScript):${NC}
   python3 -m source.scripts.query_kg \\
     --snapshot ${SNAPSHOT_DIR} \\
     find-callers "useState" --limit 5

3. ${YELLOW}Start the MCP server:${NC}
   bash examples/05-mcp/start-mcp-server.sh \\
     --snapshot ${SNAPSHOT_DIR}

4. ${YELLOW}Analyze code impact:${NC}
   python3 examples/02-query/find-impact.py \\
     "Component" ${SNAPSHOT_DIR}

5. ${YELLOW}Build a multi-repo snapshot:${NC}
   python3 -m source.scripts.build_multi_kg \\
     --repo ${FLASK_DIR} \\
     --repo ${REACT_DIR} \\
     --out data/kg_runs/multi

${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}

JavaScript/TypeScript Analysis:
  The React snapshot demonstrates SuperContext's ability to analyze
  JavaScript and TypeScript codebases. You can:
    - Track component composition and props flow
    - Find hook usage patterns
    - Analyze module imports and exports
    - Identify code sharing between packages
    - Visualize the component dependency tree

Supported languages: Python, JavaScript, TypeScript, JSX, TSX
EOF
}

print_usage() {
    cat <<EOF
Usage: $0 [OPTIONS]

Options:
    --force         Re-clone React (overwrite existing)
    --skip-build    Clone only, don't build knowledge graph
    --help          Show this help

Examples:
    bash setup-react.sh              # Normal setup
    bash setup-react.sh --force      # Force re-clone
    bash setup-react.sh --skip-build # Just clone, no KG build
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

    log_step "React Example Setup"

    # Preflight checks
    log_info "Running preflight checks..."
    check_python
    check_git

    # Clone React
    clone_react

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
