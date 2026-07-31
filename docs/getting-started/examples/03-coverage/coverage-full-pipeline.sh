#!/bin/bash
# Title: Complete Coverage Metrics and Reporting Pipeline
# Time: 5-10 minutes (includes building snapshot if needed)
# Prerequisites: Python 3.11+, git, supercontext installed
#
# This script runs the complete coverage pipeline end-to-end:
#   1. Check if a snapshot exists; build one if not
#   2. Compute coverage metrics for the snapshot
#   3. Generate a coverage report (JSON + Markdown)
#   4. Display summary in the terminal
#
# Coverage metrics evaluate how much of your codebase SuperContext can analyze.
# They help identify missing extractors and instrumentation gaps.
#
# Usage:
#   bash coverage-full-pipeline.sh                 # Use default Flask snapshot
#   bash coverage-full-pipeline.sh /path/to/repo   # Build and analyze custom repo

set -e

# Color codes for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
KG_OUTPUT_DIR="${PROJECT_ROOT}/data/kg_runs"
COVERAGE_REPORT_DIR="${PROJECT_ROOT}/docs/evaluation/runs"

# Configuration
DEFAULT_REPO_URL="https://github.com/pallets/flask.git"
DEFAULT_REPO_NAME="flask"

# Colors & formatting functions
echo_step() {
    echo -e "${BLUE}==>${NC} $*"
}

echo_success() {
    echo -e "${GREEN}✓${NC} $*"
}

echo_warning() {
    echo -e "${YELLOW}⚠${NC} $*"
}

echo_section() {
    echo ""
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}${NC} $*"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
}

# Prerequisite checks
echo_step "Checking prerequisites..."

if ! command -v python3 &> /dev/null; then
    echo "Error: Python 3 not found. Install Python 3.11+ first."
    exit 1
fi

echo_success "Python 3 found"

if ! command -v git &> /dev/null; then
    echo "Error: git not found. Install git first."
    exit 1
fi

echo_success "git available"

if ! python3 -c "import source" 2>/dev/null; then
    echo "Error: SuperContext not installed."
    exit 1
fi

echo_success "SuperContext is installed"

# Step 1: Determine or build snapshot
echo_section "Step 1: Prepare KG Snapshot"

if [ $# -eq 0 ]; then
    # Use default Flask snapshot
    SNAPSHOT_DIR="${KG_OUTPUT_DIR}/${DEFAULT_REPO_NAME}"
    REPO_PATH="/tmp/${DEFAULT_REPO_NAME}-clone"

    if [ ! -d "$SNAPSHOT_DIR" ] || [ ! -f "${SNAPSHOT_DIR}/manifest.json" ]; then
        echo_step "No snapshot found at ${SNAPSHOT_DIR}"
        echo_step "Building snapshot from ${DEFAULT_REPO_NAME}..."

        if [ ! -d "$REPO_PATH" ]; then
            echo_step "Cloning ${DEFAULT_REPO_NAME}..."
            git clone --depth 1 "$DEFAULT_REPO_URL" "$REPO_PATH" 2>&1 | tail -3
        fi

        mkdir -p "$KG_OUTPUT_DIR"
        supercontext-build-kg --repo "$REPO_PATH" --out "$SNAPSHOT_DIR" >/dev/null 2>&1
        echo_success "Snapshot built"
    else
        echo_success "Using existing snapshot at ${SNAPSHOT_DIR}"
    fi
else
    # Use provided repo path
    REPO_PATH="$1"
    REPO_NAME=$(basename "$REPO_PATH")

    if [ ! -d "$REPO_PATH" ]; then
        echo "Error: Repository path not found: ${REPO_PATH}"
        exit 1
    fi

    SNAPSHOT_DIR="${KG_OUTPUT_DIR}/${REPO_NAME}"

    if [ ! -d "$SNAPSHOT_DIR" ] || [ ! -f "${SNAPSHOT_DIR}/manifest.json" ]; then
        echo_step "Building snapshot from ${REPO_PATH}..."
        mkdir -p "$KG_OUTPUT_DIR"
        supercontext-build-kg --repo "$REPO_PATH" --out "$SNAPSHOT_DIR" >/dev/null 2>&1
        echo_success "Snapshot built at ${SNAPSHOT_DIR}"
    else
        echo_success "Using existing snapshot at ${SNAPSHOT_DIR}"
    fi
fi

echo "Snapshot location: ${SNAPSHOT_DIR}"

# Step 2: Compute coverage metrics
echo_section "Step 2: Compute Coverage Metrics"

echo_step "Running coverage metrics computation..."

if supercontext-coverage-metrics --snapshot "$SNAPSHOT_DIR" 2>&1 | head -10; then
    echo_success "Coverage metrics computed"
else
    echo_warning "Coverage metrics computation had warnings (continuing anyway)"
fi

if [ ! -f "${SNAPSHOT_DIR}/metrics.jsonl" ]; then
    echo "Warning: metrics.jsonl not found after computation"
fi

# Step 3: Generate coverage report
echo_section "Step 3: Generate Coverage Report"

REPORT_ID="coverage-$(date +%s)"
REPORT_OUTPUT_DIR="${COVERAGE_REPORT_DIR}/${REPORT_ID}"

echo_step "Writing coverage report to ${REPORT_OUTPUT_DIR}..."

mkdir -p "$REPORT_OUTPUT_DIR"

supercontext-coverage-report \
    --snapshot "$SNAPSHOT_DIR" \
    --out "$REPORT_OUTPUT_DIR" \
    --run-id "$REPORT_ID" \
    --expected-repos 1 \
    2>&1 | head -10

echo_success "Report generated"

# Step 4: Display report summary
echo_section "Step 4: Coverage Report Summary"

if [ -f "${REPORT_OUTPUT_DIR}/coverage-run.md" ]; then
    echo_step "Report contents:"
    echo ""
    head -50 "${REPORT_OUTPUT_DIR}/coverage-run.md"
    echo ""
    echo "..."
    echo ""
    echo_success "Full report saved to: ${REPORT_OUTPUT_DIR}/coverage-run.md"
else
    echo_warning "coverage-run.md not found"
fi

# Display JSON metrics if available
if [ -f "${REPORT_OUTPUT_DIR}/coverage-run.json" ]; then
    echo ""
    echo_step "Coverage metrics (JSON):"
    python3 << 'EOF'
import json
from pathlib import Path

report_path = Path("${REPORT_OUTPUT_DIR}/coverage-run.json")
if report_path.exists():
    with open(report_path) as f:
        report = json.load(f)

    print("\nMetrics Summary:")
    print("───────────────")

    if "metrics" in report:
        for metric_name, metric_value in list(report["metrics"].items())[:5]:
            if isinstance(metric_value, (int, float)):
                print(f"  {metric_name}: {metric_value:.2f}")
            else:
                print(f"  {metric_name}: {metric_value}")

    if "completeness" in report:
        print(f"\n  Completeness: {report['completeness']:.1%}")

    if "timestamp" in report:
        print(f"  Generated: {report['timestamp']}")
EOF
fi

# Step 5: Show next steps
echo_section "Coverage Pipeline Complete!"

echo "Next steps:"
echo ""
echo "  1. View the full report:"
echo "     cat ${REPORT_OUTPUT_DIR}/coverage-run.md"
echo ""
echo "  2. Check JSON metrics programmatically:"
echo "     jq '.metrics' ${REPORT_OUTPUT_DIR}/coverage-run.json"
echo ""
echo "  3. Compare with another snapshot:"
echo "     bash examples/03-coverage/coverage-compare.sh ${SNAPSHOT_DIR} <other-snapshot>"
echo ""
echo "  4. Use coverage data in your pipeline:"
echo "     python3 << 'SCRIPT'"
echo "     import json"
echo "     with open('${REPORT_OUTPUT_DIR}/coverage-run.json') as f:"
echo "         report = json.load(f)"
echo "         # Your custom analysis here"
echo "     SCRIPT"
echo ""
echo "Report saved to: ${REPORT_OUTPUT_DIR}"
echo ""
