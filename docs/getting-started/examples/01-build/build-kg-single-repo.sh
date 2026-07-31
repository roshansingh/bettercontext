#!/bin/bash
# Title: Build a Knowledge Graph from a Single Repository
# Time: 3-5 minutes (depends on repo size)
# Prerequisites: Python 3.11+, git, supercontext installed
#
# This script demonstrates how to build a complete knowledge graph snapshot
# from a single Python or TypeScript repository. It uses Flask as the default
# example, but you can provide your own repository path.
#
# Usage:
#   bash build-kg-single-repo.sh              # Use Flask as default
#   bash build-kg-single-repo.sh /path/to/repo  # Use your own repo

set -e

# Color codes for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
KG_OUTPUT_DIR="${PROJECT_ROOT}/data/kg_runs"
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

# Prerequisite checks
echo_step "Checking prerequisites..."

if ! command -v python3 &> /dev/null; then
    echo "Error: Python 3 not found. Please install Python 3.11+ first."
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo_success "Python ${PYTHON_VERSION} found"

if ! command -v git &> /dev/null; then
    echo "Error: git not found. Please install git first."
    exit 1
fi

echo_success "git is available"

# Check if supercontext is installed
if ! python3 -c "import source" 2>/dev/null; then
    echo "Error: SuperContext not installed. Run 'pip install -e .' from the project root."
    exit 1
fi

echo_success "SuperContext is installed"

# Determine which repo to analyze
if [ $# -eq 0 ]; then
    REPO_PATH="/tmp/${DEFAULT_REPO_NAME}-clone"
    REPO_NAME="${DEFAULT_REPO_NAME}"

    if [ ! -d "$REPO_PATH" ]; then
        echo_step "Cloning ${DEFAULT_REPO_NAME} repository..."
        git clone --depth 1 "$DEFAULT_REPO_URL" "$REPO_PATH" 2>&1 | grep -E "(Cloning|Resolving|Receiving|done)" || true
        echo_success "Repository cloned to ${REPO_PATH}"
    else
        echo_step "Using existing ${DEFAULT_REPO_NAME} clone at ${REPO_PATH}"
    fi
else
    REPO_PATH="$1"
    REPO_NAME=$(basename "$REPO_PATH")

    if [ ! -d "$REPO_PATH" ]; then
        echo "Error: Repository path not found: ${REPO_PATH}"
        exit 1
    fi

    echo_success "Using provided repository: ${REPO_PATH}"
fi

# Validate that the path is a git repository
if [ ! -d "${REPO_PATH}/.git" ]; then
    echo "Warning: ${REPO_PATH} does not appear to be a git repository"
fi

# Prepare output directory
mkdir -p "$KG_OUTPUT_DIR"
SNAPSHOT_DIR="${KG_OUTPUT_DIR}/${REPO_NAME}"

echo ""
echo_step "Building knowledge graph for: ${REPO_NAME}"
echo "  Repository: ${REPO_PATH}"
echo "  Output: ${SNAPSHOT_DIR}"
echo ""

# Run the build
if supercontext-build-kg --repo "$REPO_PATH" --out "$SNAPSHOT_DIR" 2>&1 | while IFS= read -r line; do
    # Filter the JSON output from the command and show it nicely
    if [[ "$line" == "{"* ]]; then
        echo "$line" | python3 -m json.tool 2>/dev/null | head -20
    else
        echo "$line"
    fi
done; then
    echo ""
    echo_success "Knowledge graph built successfully!"
else
    echo "Error: Failed to build knowledge graph"
    exit 1
fi

# Verify the snapshot
echo ""
echo_step "Verifying snapshot files..."

required_files=("entities.jsonl" "facts.jsonl" "evidence.jsonl" "manifest.json")
for file in "${required_files[@]}"; do
    if [ -f "${SNAPSHOT_DIR}/${file}" ]; then
        file_size=$(du -h "${SNAPSHOT_DIR}/${file}" | cut -f1)
        echo_success "${file} (${file_size})"
    else
        echo "Warning: ${file} not found"
    fi
done

# Show snapshot summary
echo ""
echo_step "Snapshot summary:"
if [ -f "${SNAPSHOT_DIR}/manifest.json" ]; then
    python3 -c "
import json
with open('${SNAPSHOT_DIR}/manifest.json') as f:
    m = json.load(f)
    print(f\"  Repository: {m.get('repo_paths', ['unknown'])[0]}\")
    print(f\"  Languages: {', '.join(m.get('languages', []))}\")
    print(f\"  Entity count: {m.get('entity_count', 'unknown')}\")
    print(f\"  Fact count: {m.get('fact_count', 'unknown')}\")
    print(f\"  Build time: {m.get('timestamp', 'unknown')}\")
" 2>/dev/null || echo "  (Could not parse manifest)"
fi

echo ""
echo_success "Knowledge graph ready!"
echo ""
echo "Next steps:"
echo "  1. Query the snapshot:"
echo "     bash examples/02-query/query-common-patterns.sh ${SNAPSHOT_DIR}"
echo ""
echo "  2. Or run with jq to filter results:"
echo "     bash examples/02-query/query-with-jq.sh ${SNAPSHOT_DIR}"
echo ""
echo "  3. Snapshot location:"
echo "     ${SNAPSHOT_DIR}/"
echo ""
