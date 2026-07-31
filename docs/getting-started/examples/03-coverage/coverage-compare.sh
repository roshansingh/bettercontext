#!/bin/bash
# Title: Compare Coverage Metrics Across Two Snapshots
# Time: 1-2 minutes
# Prerequisites: Python 3.11+, supercontext installed, two KG snapshots
#
# This script compares coverage metrics between two knowledge graph snapshots.
# It shows what improved, regressed, or stayed the same, helping you track
# how coverage changes over time or across different instrumentation versions.
#
# Usage:
#   bash coverage-compare.sh /path/to/snapshot1 /path/to/snapshot2
#
# Example:
#   bash coverage-compare.sh data/kg_runs/flask-v1 data/kg_runs/flask-v2

set -e

# Color codes for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Colors & formatting functions
echo_step() {
    echo -e "${BLUE}==>${NC} $*"
}

echo_success() {
    echo -e "${GREEN}✓${NC} $*"
}

echo_improvement() {
    echo -e "${GREEN}↑${NC} $*"
}

echo_regression() {
    echo -e "${RED}↓${NC} $*"
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

# Check arguments
if [ $# -lt 2 ]; then
    echo "Usage: bash coverage-compare.sh <snapshot1> <snapshot2>"
    echo ""
    echo "Example:"
    echo "  bash coverage-compare.sh data/kg_runs/flask data/kg_runs/flask-enhanced"
    echo ""
    echo "This compares coverage metrics between two snapshots and shows:"
    echo "  - Changes in entity counts"
    echo "  - Coverage improvements or regressions"
    echo "  - Language instrumentation differences"
    exit 1
fi

SNAPSHOT1="$1"
SNAPSHOT2="$2"

# Validate snapshots
if [ ! -d "$SNAPSHOT1" ]; then
    echo "Error: Snapshot 1 not found at ${SNAPSHOT1}"
    exit 1
fi

if [ ! -d "$SNAPSHOT2" ]; then
    echo "Error: Snapshot 2 not found at ${SNAPSHOT2}"
    exit 1
fi

echo_step "Comparing coverage metrics"
echo "  Before: $SNAPSHOT1"
echo "  After:  $SNAPSHOT2"

# Check if metrics files exist; compute if not
if [ ! -f "${SNAPSHOT1}/metrics.jsonl" ]; then
    echo_warning "Computing metrics for snapshot 1..."
    supercontext-coverage-metrics --snapshot "$SNAPSHOT1" >/dev/null 2>&1
fi

if [ ! -f "${SNAPSHOT2}/metrics.jsonl" ]; then
    echo_warning "Computing metrics for snapshot 2..."
    supercontext-coverage-metrics --snapshot "$SNAPSHOT2" >/dev/null 2>&1
fi

echo_success "Metrics loaded"

# Run comparison using coverage-metrics --compare
echo_section "Coverage Delta Analysis"

python3 << 'EOF'
import json
import sys
from pathlib import Path
from collections import defaultdict

def load_metrics_jsonl(path):
    """Load metrics from metrics.jsonl and return as dict by cell_id."""
    metrics = {}
    if path.exists():
        with open(path) as f:
            for line in f:
                data = json.loads(line)
                cell_id = data.get('cell_id')
                if cell_id:
                    metrics[cell_id] = data
    return metrics

def get_summary_metric(metrics, key, default=0):
    """Extract a summary-level metric."""
    for cell_id, data in metrics.items():
        if key in data:
            return data[key]
    return default

def format_percent(value):
    """Format as percentage."""
    if isinstance(value, (int, float)):
        return f"{value:.1%}" if value <= 1 else f"{value:.1f}%"
    return str(value)

snap1 = Path("${SNAPSHOT1}")
snap2 = Path("${SNAPSHOT2}")

metrics1 = load_metrics_jsonl(snap1 / "metrics.jsonl")
metrics2 = load_metrics_jsonl(snap2 / "metrics.jsonl")

# Load manifest files for entity counts
def get_manifest(snap):
    manifest_path = snap / "manifest.json"
    if manifest_path.exists():
        with open(manifest_path) as f:
            return json.load(f)
    return {}

manifest1 = get_manifest(snap1)
manifest2 = get_manifest(snap2)

print("Entity Counts:")
print("─" * 60)

entity_changes = {
    'entity_count': ('Total Entities', 'count'),
    'fact_count': ('Total Facts', 'count'),
}

for key, (label, fmt) in entity_changes.items():
    val1 = manifest1.get(key, 0)
    val2 = manifest2.get(key, 0)
    delta = val2 - val1
    delta_pct = (delta / val1 * 100) if val1 > 0 else 0

    symbol = "↑" if delta > 0 else ("=" if delta == 0 else "↓")
    color = "\033[0;32m" if delta > 0 else ("\033[0;36m" if delta == 0 else "\033[0;31m")
    reset = "\033[0m"

    print(f"  {label:20} {val1:8} → {val2:8} {color}{symbol:>3}{reset} {delta_pct:+.1f}%")

print()
print("Language Coverage:")
print("─" * 60)

lang1 = set(manifest1.get('languages', []))
lang2 = set(manifest2.get('languages', []))

all_langs = sorted(lang1 | lang2)
for lang in all_langs:
    in1 = lang in lang1
    in2 = lang in lang2
    status = "✓" if in2 else "✗"
    change = "→" if (in1 and not in2) else ("← new" if (not in1 and in2) else "  ")
    print(f"  {lang:15} {status:>1} {change}")

print()
print("Key Observations:")
print("─" * 60)

# Analyze changes
e_delta = manifest2.get('entity_count', 0) - manifest1.get('entity_count', 0)
f_delta = manifest2.get('fact_count', 0) - manifest1.get('fact_count', 0)

if e_delta > 0:
    print(f"  ✓ Discovered {e_delta} new entities")
if f_delta > 0:
    print(f"  ✓ Identified {f_delta} new relationships")

new_langs = lang2 - lang1
if new_langs:
    print(f"  ✓ Added support for: {', '.join(new_langs)}")

removed_langs = lang1 - lang2
if removed_langs:
    print(f"  ✗ Removed support for: {', '.join(removed_langs)}")

if not e_delta and not f_delta and lang1 == lang2:
    print("  = No significant changes detected")

EOF

echo ""
echo_step "Comparison complete"
echo ""
echo "Notes:"
echo "  • Improvements are indicated with ↑ (green)"
echo "  • Regressions are indicated with ↓ (red)"
echo "  • Percentages show relative change"
echo ""
echo "For detailed metrics analysis:"
echo "  supercontext-coverage-metrics --snapshot $SNAPSHOT1 --snapshot $SNAPSHOT2 --compare"
echo ""
