from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from source.kg.query.contract_diff import contract_diff_packet
from source.kg.query.graph_diff import (
    call_edge_delta_for_paths,
    diff_snapshots,
    removed_symbols_with_surviving_referrers,
    removed_test_references,
)
from source.kg.query.snapshot import KgSnapshot


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Diff two KG snapshots and query the structural delta."
    )
    parser.add_argument("--base-snapshot", required=True, help="Base KG snapshot directory")
    parser.add_argument("--head-snapshot", required=True, help="Head KG snapshot directory")

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("summary")
    subparsers.add_parser("removed-still-referenced")
    subparsers.add_parser("removed-test-references")

    call_edge = subparsers.add_parser("call-edge-delta")
    call_edge.add_argument("--path", action="append", default=[], help="File path filter (repeatable)")

    review_packet = subparsers.add_parser(
        "review-packet",
        help="Emit hypothesis-shaped contract-diff packet (guard_call_removed, responsibility_moved, test_reference_removed).",
    )
    review_packet.add_argument(
        "--path",
        action="append",
        default=[],
        dest="changed_paths",
        help="Changed file path (repeatable); used by guard_call_removed family.",
    )

    args = parser.parse_args()

    base_dir = Path(args.base_snapshot)
    head_dir = Path(args.head_snapshot)

    try:
        base = KgSnapshot(base_dir)
    except (FileNotFoundError, OSError, ValueError) as exc:
        parser.error(f"Cannot open base snapshot {base_dir}: {exc}")

    try:
        head = KgSnapshot(head_dir)
    except (FileNotFoundError, OSError, ValueError) as exc:
        parser.error(f"Cannot open head snapshot {head_dir}: {exc}")

    if args.command == "review-packet":
        # review-packet bypasses the shared delta path and loads snapshots internally
        # via contract_diff_packet (which re-opens them) to keep the packet builder
        # self-contained.  Changed paths come from --path flags on the subcommand.
        try:
            result = contract_diff_packet(
                str(base_dir),
                str(head_dir),
                changed_paths=getattr(args, "changed_paths", []) or [],
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            raise SystemExit(1)
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    try:
        delta = diff_snapshots(base, head)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)

    if args.command == "summary":
        result = delta.summary()
    elif args.command == "removed-still-referenced":
        result = removed_symbols_with_surviving_referrers(delta, base, head)
    elif args.command == "removed-test-references":
        result = removed_test_references(delta, base, head)
    elif args.command == "call-edge-delta":
        result = call_edge_delta_for_paths(delta, base, head, args.path)
    else:
        raise ValueError(f"Unsupported command: {args.command}")

    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
