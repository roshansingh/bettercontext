#!/bin/bash
# Title: Build a Knowledge Graph from Multiple Repositories
# Time: 5-10 minutes (depends on repo sizes)
# Prerequisites: Python 3.11+, git, supercontext installed
#
# This script demonstrates how to build a knowledge graph snapshot that spans
# multiple repositories. It enables cross-repository analysis such as which
# services depend on others and how changes propagate across the fleet.
#
# By default, it clones two example repositories (requests and urllib3).
# You can provide your own paths via command-line arguments.
#
# Usage:
#   bash build-kg-multi-repo.sh                    # Use default repos
#   bash build-kg-multi-repo.sh /repo1 /repo2 ...  # Use your own repos

set -e

# Color codes for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Configuration
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
KG_OUTPUT_DIR="${PROJECT_ROOT}/data/kg_runs"

# Default repositories for multi-repo example
declare -a DEFAULT_REPOS=(
    "https://github.com/psf/requests.git:requests"
    "https://github.com/urllib3/urllib3.git:urllib3"
)

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

# Prepare output directory
mkdir -p "$KG_OUTPUT_DIR"

# Determine which repos to analyze
if [ $# -eq 0 ]; then
    # Use default repos
    echo_step "Using default repositories for multi-repo analysis..."
    declare -a REPOS_TO_ANALYZE
    declare -a REPO_NAMES

    for repo_spec in "${DEFAULT_REPOS[@]}"; do
        IFS=':' read -r url name <<< "$repo_spec"
        repo_path="/tmp/${name}-clone"

        if [ ! -d "$repo_path" ]; then
            echo_step "Cloning ${name}..."
            git clone --depth 1 "$url" "$repo_path" 2>&1 | grep -E "(Cloning|Resolving|Receiving|done)" || true
            echo_success "${name} cloned"
        else
            echo_step "Using existing ${name} clone"
        fi

        REPOS_TO_ANALYZE+=("$repo_path")
        REPO_NAMES+=("$name")
    done
else
    # Use provided repos
    declare -a REPOS_TO_ANALYZE
    declare -a REPO_NAMES

    for repo_path in "$@"; do
        if [ ! -d "$repo_path" ]; then
            echo "Error: Repository not found: ${repo_path}"
            exit 1
        fi

        repo_name=$(basename "$repo_path")
        REPOS_TO_ANALYZE+=("$repo_path")
        REPO_NAMES+=("$repo_name")
        echo_success "Will analyze: ${repo_name}"
    done
fi

# Generate output directory name based on repos
if [ ${#REPO_NAMES[@]} -eq 2 ]; then
    snapshot_name="${REPO_NAMES[0]}-${REPO_NAMES[1]}-multi"
else
    snapshot_name="multi-repo-fleet"
fi

SNAPSHOT_DIR="${KG_OUTPUT_DIR}/${snapshot_name}"

echo ""
echo_step "Building multi-repository knowledge graph..."
echo "  Repositories (${#REPOS_TO_ANALYZE[@]}):"
for repo_path in "${REPOS_TO_ANALYZE[@]}"; do
    echo "    - $(basename "$repo_path")"
done
echo "  Output: ${SNAPSHOT_DIR}"
echo ""

# Build the multi-repo snapshot
BUILD_ARGS="--out ${SNAPSHOT_DIR}"
for repo_path in "${REPOS_TO_ANALYZE[@]}"; do
    BUILD_ARGS="${BUILD_ARGS} --repo ${repo_path}"
done

if supercontext-build-multi-kg $BUILD_ARGS 2>&1 | while IFS= read -r line; do
    if [[ "$line" == "{"* ]]; then
        echo "$line" | python3 -m json.tool 2>/dev/null | head -20
    else
        echo "$line"
    fi
done; then
    echo ""
    echo_success "Multi-repository knowledge graph built successfully!"
else
    echo "Error: Failed to build multi-repository knowledge graph"
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
        echo_warning "${file} not found"
    fi
done

# Show snapshot statistics
echo ""
echo_step "Multi-repository snapshot analysis:"

python3 << 'EOF'
import json
from pathlib import Path

snapshot_dir = Path("${SNAPSHOT_DIR}")

if (snapshot_dir / "manifest.json").exists():
    with open(snapshot_dir / "manifest.json") as f:
        manifest = json.load(f)

    print(f"  Repositories analyzed: {len(manifest.get('repo_paths', []))}")
    for repo in manifest.get('repo_paths', []):
        print(f"    - {repo}")

    print(f"\n  Languages: {', '.join(manifest.get('languages', []))}")
    print(f"  Total entities: {manifest.get('entity_count', 'unknown')}")
    print(f"  Total facts: {manifest.get('fact_count', 'unknown')}")

# Count cross-repo links
if (snapshot_dir / "facts.jsonl").exists():
    cross_repo_count = 0
    with open(snapshot_dir / "facts.jsonl") as f:
        for line in f:
            fact = json.loads(line)
            if fact.get('fact_type') == 'CROSS_REPO_LINK':
                cross_repo_count += 1

    if cross_repo_count > 0:
        print(f"\n  Cross-repository links discovered: {cross_repo_count}")
        print("  This means one repo has dependencies or references in another repo.")

print(f"\n  Build location: {snapshot_dir}")
EOF
