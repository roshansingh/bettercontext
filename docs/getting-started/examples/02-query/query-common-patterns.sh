#!/bin/bash
# Title: Query a Knowledge Graph Using All Standard Patterns
# Time: 2-3 minutes
# Prerequisites: Python 3.11+, supercontext installed, built KG snapshot
#
# This script demonstrates all 8 main query patterns available in SuperContext:
#   1. summary - Overview of the knowledge graph
#   2. find-callers - Functions that call a given function
#   3. find-callees - Functions called by a given function
#   4. blast-radius - All functions affected by a change to a symbol
#   5. top-dependencies - External packages this repo depends on most
#   6. modules-importing - Modules that import a given package
#   7. dependency-info - Detailed information about a package
#   8. cross-repo-links - Links between multiple repositories
#
# Usage:
#   bash query-common-patterns.sh                    # Use default snapshot
#   bash query-common-patterns.sh /path/to/snapshot  # Use specific snapshot

set -e

# Color codes for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
DEFAULT_SNAPSHOT="${PROJECT_ROOT}/data/kg_runs/flask"

# Colors & formatting functions
echo_step() {
    echo -e "${BLUE}==>${NC} $*"
}

echo_query() {
    echo -e "${CYAN}➜${NC} $*"
}

echo_success() {
    echo -e "${GREEN}✓${NC} $*"
}

echo_section() {
    echo ""
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}${NC} $*"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
}

# Determine snapshot path
if [ $# -eq 0 ]; then
    SNAPSHOT="$DEFAULT_SNAPSHOT"
    if [ ! -d "$SNAPSHOT" ]; then
        echo "Error: Default snapshot not found at ${SNAPSHOT}"
        echo "Build one first: bash examples/01-build/build-kg-single-repo.sh"
        exit 1
    fi
else
    SNAPSHOT="$1"
    if [ ! -d "$SNAPSHOT" ]; then
        echo "Error: Snapshot not found at ${SNAPSHOT}"
        exit 1
    fi
fi

echo_step "Using snapshot: ${SNAPSHOT}"
echo ""

# Query 1: Summary
echo_section "Query 1: Snapshot Summary"
echo_query "supercontext-query-kg --snapshot $SNAPSHOT summary"
supercontext-query-kg --snapshot "$SNAPSHOT" summary | head -30

# Query 2: Find Callers
echo_section "Query 2: Find Callers (Who calls this function?)"
echo_query "supercontext-query-kg --snapshot $SNAPSHOT find-callers 'Flask.__init__' --limit 5"
echo ""
echo "This shows which functions call Flask.__init__:"
echo ""
if supercontext-query-kg --snapshot "$SNAPSHOT" find-callers "Flask.__init__" --limit 5 2>/dev/null | head -20; then
    true
else
    echo "  (No callers found - this is normal for a top-level class)"
fi

# Query 3: Find Callees
echo_section "Query 3: Find Callees (What does this function call?)"
echo_query "supercontext-query-kg --snapshot $SNAPSHOT find-callees 'Flask.route' --limit 5"
echo ""
echo "This shows which functions are called by Flask.route:"
echo ""
if supercontext-query-kg --snapshot "$SNAPSHOT" find-callees "Flask.route" --limit 5 2>/dev/null | head -20; then
    true
else
    echo "  (Callees lookup may not apply to decorators)"
fi

# Query 4: Blast Radius
echo_section "Query 4: Blast Radius (What breaks if I change this?)"
echo_query "supercontext-query-kg --snapshot $SNAPSHOT blast-radius 'Flask.__init__' --depth 2 --limit 5"
echo ""
echo "This shows all code affected by changes to Flask.__init__:"
echo ""
if supercontext-query-kg --snapshot "$SNAPSHOT" blast-radius "Flask.__init__" --depth 2 --limit 5 2>/dev/null | head -20; then
    true
else
    echo "  (Blast radius analysis may vary by repo)"
fi

# Query 5: Top Dependencies
echo_section "Query 5: Top Dependencies (What's this repo most dependent on?)"
echo_query "supercontext-query-kg --snapshot $SNAPSHOT top-dependencies --limit 10"
echo ""
supercontext-query-kg --snapshot "$SNAPSHOT" top-dependencies --limit 10 | head -20

# Query 6: Modules Importing a Package
echo_section "Query 6: Modules Importing (Who uses this package?)"
echo_query "supercontext-query-kg --snapshot $SNAPSHOT modules-importing 'werkzeug' --limit 5"
echo ""
echo "This shows which modules import 'werkzeug':"
echo ""
if supercontext-query-kg --snapshot "$SNAPSHOT" modules-importing "werkzeug" --limit 5 2>/dev/null | head -20; then
    true
else
    echo "  (No imports found - adjust the package name)"
fi

# Query 7: Dependency Info
echo_section "Query 7: Dependency Info (Detailed package information)"
echo_query "supercontext-query-kg --snapshot $SNAPSHOT dependency-info 'werkzeug'"
echo ""
if supercontext-query-kg --snapshot "$SNAPSHOT" dependency-info "werkzeug" 2>/dev/null | head -20; then
    true
else
    echo "  (This package may not be in the snapshot)"
fi

# Query 8: Cross-Repo Links (if multi-repo)
echo_section "Query 8: Cross-Repository Links (Multi-repo analysis)"
echo_query "supercontext-query-kg --snapshot $SNAPSHOT cross-repo-links --limit 5"
echo ""
if supercontext-query-kg --snapshot "$SNAPSHOT" cross-repo-links --limit 5 2>/dev/null | head -20; then
    true
else
    echo "  (No cross-repo links found - this is expected for single-repo snapshots)"
fi

# Summary
echo_section "Query Summary"
echo "You've seen all 8 query patterns:"
echo ""
echo "  ${GREEN}✓${NC} summary         - KG overview"
echo "  ${GREEN}✓${NC} find-callers    - Inbound call graph"
echo "  ${GREEN}✓${NC} find-callees    - Outbound call graph"
echo "  ${GREEN}✓${NC} blast-radius    - Change impact analysis"
echo "  ${GREEN}✓${NC} top-dependencies - External dependency analysis"
echo "  ${GREEN}✓${NC} modules-importing - Package usage"
echo "  ${GREEN}✓${NC} dependency-info   - Package details"
echo "  ${GREEN}✓${NC} cross-repo-links  - Multi-repo dependencies"
echo ""
echo "Next steps:"
echo "  1. Filter results with jq:"
echo "     bash examples/02-query/query-with-jq.sh ${SNAPSHOT}"
echo ""
echo "  2. Run programmatic impact analysis:"
echo "     python examples/02-query/find-impact.py"
echo ""
