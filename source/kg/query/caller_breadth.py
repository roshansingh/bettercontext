from __future__ import annotations

from dataclasses import dataclass

from source.kg.core.models import JsonObject
from source.kg.query.snapshot import KgSnapshot


_CONSUMER_ENUMERATION_LIMIT = 200
_RAW_CONSUMER_FACT_LIMIT = _CONSUMER_ENUMERATION_LIMIT * 5
_TOP_OUT_OF_DIFF_LIMIT = 3


@dataclass(frozen=True)
class ConsumerCoordinate:
    path: str
    line: int | None = None
    qualname: str | None = None

    def to_json(self) -> JsonObject:
        row: JsonObject = {"path": self.path}
        if self.line is not None:
            row["line"] = self.line
        if self.qualname:
            row["qualname"] = self.qualname
        return row


@dataclass(frozen=True)
class ConsumerBreadth:
    total_consumers: int
    in_diff_count: int
    out_of_diff_count: int
    top_out_of_diff: tuple[ConsumerCoordinate, ...]
    enumeration_status: str
    enumerated_limit: int | None = None
    raw_fact_limit: int | None = None
    unknown_coordinate_count: int = 0

    def to_json(self) -> JsonObject:
        row: JsonObject = {
            "total_consumers": self.total_consumers,
            "consumer_unit": "file",
            "in_diff_count": self.in_diff_count,
            "out_of_diff_count": self.out_of_diff_count,
            "top_out_of_diff": [row.to_json() for row in self.top_out_of_diff],
            "enumeration_status": self.enumeration_status,
        }
        if self.enumerated_limit is not None:
            row["enumerated_limit"] = self.enumerated_limit
        if self.raw_fact_limit is not None:
            row["raw_fact_limit"] = self.raw_fact_limit
        if self.unknown_coordinate_count:
            row["unknown_coordinate_count"] = self.unknown_coordinate_count
        return row


def compute_consumer_breadth(
    kg: KgSnapshot,
    *,
    symbol_urn: str | None = None,
    qualname: str | None = None,
    path: str | None = None,
    changed_files: list[str] | tuple[str, ...] = (),
) -> ConsumerBreadth:
    """Enumerate known static CALLS/IMPORTS consumers for one local symbol.

    The status is honest about the static KG scope: ``complete`` means all matching
    facts currently present in the snapshot were enumerated without hitting the local
    cap, not that dynamic/runtime dispatch is impossible. ``capped`` means counts are
    lower bounds over known static consumer files.
    """

    symbol = _symbol_by_urn(kg, symbol_urn) if symbol_urn else None
    if symbol_urn and symbol is None and not qualname:
        return _empty_breadth("unresolved")
    if symbol is not None:
        identity = symbol.get("identity") or {}
        qualname = qualname or _string(identity.get("qualname"))
        props = symbol.get("properties") or {}
        path = path or _string(props.get("path"))

    if not qualname:
        return _empty_breadth("unresolved")

    callers_payload = kg.find_callers(
        qualname,
        limit=_RAW_CONSUMER_FACT_LIMIT + 1,
        path=path,
        include_all=False,
    )
    caller_rows = [row for row in callers_payload.get("callers") or [] if isinstance(row, dict)]
    raw_fact_capped = len(caller_rows) > _RAW_CONSUMER_FACT_LIMIT
    consumers: list[ConsumerCoordinate] = []
    unknown_coordinate_count = 0
    for row in caller_rows[:_RAW_CONSUMER_FACT_LIMIT]:
        coordinate = _coordinate_from_call_row(row)
        if coordinate is not None:
            consumers.append(coordinate)
        else:
            unknown_coordinate_count += 1

    resolution = callers_payload.get("target")
    resolution_status = str(resolution.get("status") or "") if isinstance(resolution, dict) else ""
    if isinstance(resolution, dict) and resolution_status == "resolved":
        import_payload = kg.symbol_import_consumer_leads(
            resolution,
            limit=_RAW_CONSUMER_FACT_LIMIT + 1,
        )
        import_leads = [lead for lead in import_payload.get("leads") or [] if isinstance(lead, dict)]
        raw_fact_capped = raw_fact_capped or len(import_leads) > _RAW_CONSUMER_FACT_LIMIT
        for lead in import_leads[:_RAW_CONSUMER_FACT_LIMIT]:
            coordinate = _coordinate_from_import_lead(lead)
            if coordinate is not None:
                consumers.append(coordinate)
            else:
                unknown_coordinate_count += 1

    deduped = _dedupe_consumers(consumers)
    file_capped = len(deduped) > _CONSUMER_ENUMERATION_LIMIT
    if len(deduped) > _CONSUMER_ENUMERATION_LIMIT:
        deduped = deduped[:_CONSUMER_ENUMERATION_LIMIT]
    if not deduped:
        if unknown_coordinate_count:
            return ConsumerBreadth(
                total_consumers=0,
                in_diff_count=0,
                out_of_diff_count=0,
                top_out_of_diff=(),
                enumeration_status="partial",
                raw_fact_limit=_RAW_CONSUMER_FACT_LIMIT if raw_fact_capped else None,
                unknown_coordinate_count=unknown_coordinate_count,
            )
        if resolution_status and resolution_status != "resolved":
            return _empty_breadth("unresolved")
        return _empty_breadth("no_facts")

    changed = {_normalize_path(path) for path in changed_files if isinstance(path, str) and path}
    in_diff = [row for row in deduped if _normalize_path(row.path) in changed]
    out_of_diff = [row for row in deduped if _normalize_path(row.path) not in changed]
    status = "capped" if file_capped else ("fact_capped" if raw_fact_capped else "complete")
    if unknown_coordinate_count and status == "complete":
        status = "partial"
    return ConsumerBreadth(
        total_consumers=len(deduped),
        in_diff_count=len(in_diff),
        out_of_diff_count=len(out_of_diff),
        top_out_of_diff=tuple(out_of_diff[:_TOP_OUT_OF_DIFF_LIMIT]),
        enumeration_status=status,
        enumerated_limit=_CONSUMER_ENUMERATION_LIMIT if file_capped else None,
        raw_fact_limit=_RAW_CONSUMER_FACT_LIMIT if raw_fact_capped and not file_capped else None,
        unknown_coordinate_count=unknown_coordinate_count,
    )


def _empty_breadth(status: str) -> ConsumerBreadth:
    return ConsumerBreadth(
        total_consumers=0,
        in_diff_count=0,
        out_of_diff_count=0,
        top_out_of_diff=(),
        enumeration_status=status,
    )


def _symbol_by_urn(kg: KgSnapshot, urn: str | None) -> JsonObject | None:
    if not urn:
        return None
    for entity in kg.entities:
        if entity.get("kind") == "CodeSymbol" and entity.get("urn") == urn:
            return entity
    return None


def _coordinate_from_call_row(row: JsonObject) -> ConsumerCoordinate | None:
    path, line = _first_evidence_coordinate(row)
    if not path:
        return None
    qualname = _string(row.get("subject"))
    return ConsumerCoordinate(path=path, line=line, qualname=qualname)


def _coordinate_from_import_lead(lead: JsonObject) -> ConsumerCoordinate | None:
    importer = lead.get("importer")
    path = _string(importer.get("path")) if isinstance(importer, dict) else None
    line: int | None = None
    if not path:
        fact = lead.get("fact")
        if isinstance(fact, dict):
            path, line = _first_evidence_coordinate(fact)
    else:
        fact = lead.get("fact")
        if isinstance(fact, dict):
            fact_path, fact_line = _first_evidence_coordinate(fact)
            if _normalize_path(fact_path or "") == _normalize_path(path):
                line = fact_line
    if not path:
        return None
    qualname = _string(importer.get("display_name")) if isinstance(importer, dict) else None
    return ConsumerCoordinate(path=path, line=line, qualname=qualname)


def _first_evidence_coordinate(row: JsonObject) -> tuple[str | None, int | None]:
    evidence = row.get("evidence")
    if not isinstance(evidence, list):
        return None, None
    for item in evidence:
        if not isinstance(item, dict):
            continue
        ref = item.get("bytes_ref")
        if not isinstance(ref, dict):
            continue
        path = _string(ref.get("path"))
        if not path:
            continue
        line = _int(ref.get("line_start"))
        return path, line
    return None, None


def _dedupe_consumers(rows: list[ConsumerCoordinate]) -> list[ConsumerCoordinate]:
    deduped: list[ConsumerCoordinate] = []
    seen: set[str] = set()
    for row in rows:
        key = _normalize_path(row.path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    deduped.sort(key=lambda row: (_normalize_path(row.path), row.line or 0, row.qualname or ""))
    return deduped


def _normalize_path(path: str) -> str:
    value = path.replace("\\", "/").strip()
    while value.startswith("./"):
        value = value[2:]
    return value


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
