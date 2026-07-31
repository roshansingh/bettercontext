#!/bin/bash
# Title: Query and Filter Results with jq
# Time: 2-3 minutes
# Prerequisites: Python 3.11+, jq, supercontext installed, built KG snapshot
#
# This script shows how to use jq to filter and extract specific fields from
# SuperContext query results. It demonstrates practical patterns for working
# with JSON output in shell scripts.
#
# Usage:
#   bash query-with-jq.sh                         # Use default snapshot
#   bash query-with-jq.sh /path/to/snapshot       # Use specific snapshot
#   bash query-with-jq.sh /path/to/snapshot func  # Query specific symbol

set -e

# Color codes for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
DEFAULT_SNAPSHOT="${PROJECT_ROOT}/data/kg_runs/flask"
DEFAULT_SYMBOL="Flask.request"

# Colors & formatting functions
echo_step() {
    echo -e "${BLUE}==>${NC} $*"
}

echo_query() {
    echo -e "${CYAN}Query:${NC} $*"
}

echo_filter() {
    echo -e "${CYAN}Filter:${NC} $*"
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

# Check jq is installed
if ! command -v jq &> /dev/null; then
    echo "Error: jq not found. Install it with: brew install jq"
    exit 1
fi

echo_success "jq is available"

# Determine snapshot path
SNAPSHOT="$DEFAULT_SNAPSHOT"
SYMBOL="$DEFAULT_SYMBOL"

if [ $# -ge 1 ]; then
    SNAPSHOT="$1"
fi

if [ $# -ge 2 ]; then
    SYMBOL="$2"
fi

if [ ! -d "$SNAPSHOT" ]; then
    echo "Error: Snapshot not found at ${SNAPSHOT}"
    exit 1
fi

echo_step "Using snapshot: ${SNAPSHOT}"
echo_step "Using symbol: ${SYMBOL}"
echo ""

# Example 1: Extract just function names from find-callers
echo_section "Example 1: Extract Function Names"
echo_query "supercontext-query-kg --snapshot $SNAPSHOT find-callers '$SYMBOL' --limit 5"
echo_filter "jq -r '.[].caller_symbol'"
echo ""
echo "This extracts just the function names from the result:"
echo ""

if supercontext-query-kg --snapshot "$SNAPSHOT" find-callers "$SYMBOL" --limit 5 2>/dev/null | \
    jq -r '.[]? | "\(.caller_symbol)"' 2>/dev/null; then
    echo ""
else
    echo "  (No results found)"
    echo ""
fi

# Example 2: Count how many callers exist
echo_section "Example 2: Count Results"
echo_query "supercontext-query-kg --snapshot $SNAPSHOT find-callers '$SYMBOL' --limit 100"
echo_filter "jq 'length'"
echo ""
echo "This counts the number of results:"
echo ""

count=$(supercontext-query-kg --snapshot "$SNAPSHOT" find-callers "$SYMBOL" --limit 100 2>/dev/null | \
    jq 'length' 2>/dev/null || echo 0)

echo "  Total callers of $SYMBOL: ${count}"
echo ""

# Example 3: Extract specific fields and format as table
echo_section "Example 3: Format as CSV/Table"
echo_query "supercontext-query-kg --snapshot $SNAPSHOT find-callers '$SYMBOL' --limit 5"
echo_filter "jq -r '.[] | [.caller_symbol, .file_path, .line] | @csv'"
echo ""
echo "This formats results as CSV (symbol, file, line):"
echo ""

supercontext-query-kg --snapshot "$SNAPSHOT" find-callers "$SYMBOL" --limit 5 2>/dev/null | \
    jq -r '.[]? | [.caller_symbol // "unknown", .file_path // "unknown", .line // 0] | @csv' 2>/dev/null || \
    echo "  (No results)"

echo ""

# Example 4: Filter results by criteria
echo_section "Example 4: Filter by Field Value"
echo_query "supercontext-query-kg --snapshot $SNAPSHOT top-dependencies --limit 20"
echo_filter "jq '.[] | select(.count >= 5) | {name, count}'"
echo ""
echo "This shows only dependencies used 5+ times:"
echo ""

supercontext-query-kg --snapshot "$SNAPSHOT" top-dependencies --limit 20 2>/dev/null | \
    jq '.[]? | select(.count >= 5) | {name: .package_name, count: .count}' 2>/dev/null || \
    echo "  (No dependencies found)"

echo ""

# Example 5: Extract nested objects
echo_section "Example 5: Extract Nested Fields"
echo_query "supercontext-query-kg --snapshot $SNAPSHOT find-callees '$SYMBOL' --limit 3"
echo_filter "jq '.[] | {func: .callee_symbol, location: .file_path}'"
echo ""
echo "This extracts and renames fields from results:"
echo ""

supercontext-query-kg --snapshot "$SNAPSHOT" find-callees "$SYMBOL" --limit 3 2>/dev/null | \
    jq '.[]? | {func: .callee_symbol // "unknown", location: .file_path // "unknown"}' 2>/dev/null || \
    echo "  (No results found)"

echo ""

# Example 6: Practical shell script usage
echo_section "Example 6: Using Results in Shell Scripts"
echo ""
echo "You can pipe jq output to other commands:"
echo ""
echo "  # Extract symbol names and pass to another command"
echo "  supercontext-query-kg --snapshot $SNAPSHOT find-callers '$SYMBOL' |"
echo "    jq -r '.[] | .caller_symbol' | while read symbol; do"
echo "      echo \"Analyzing: \$symbol\""
echo "      supercontext-query-kg --snapshot $SNAPSHOT blast-radius \"\$symbol\" --depth 1"
echo "    done"
echo ""

# Summary
echo_section "jq Tips & Patterns"
echo ""
echo "Common jq filters:"
echo "  ${GREEN}✓${NC} jq '.[]'              - Iterate all array elements"
echo "  ${GREEN}✓${NC} jq '.[0]'             - Get first element"
echo "  ${GREEN}✓${NC} jq '.[] | .field'    - Extract field from all elements"
echo "  ${GREEN}✓${NC} jq 'length'          - Count array elements"
echo "  ${GREEN}✓${NC} jq 'map(.field)'     - Transform all elements"
echo "  ${GREEN}✓${NC} jq 'select(.x > 5)'  - Filter by condition"
echo "  ${GREEN}✓${NC} jq '{a, b}'          - Extract multiple fields"
echo "  ${GREEN}✓${NC} jq '@csv'            - Format as CSV"
echo "  ${GREEN}✓${NC} jq 'sort_by(.field)' - Sort by field"
echo ""
echo "For more jq help: jq --help"
echo ""
