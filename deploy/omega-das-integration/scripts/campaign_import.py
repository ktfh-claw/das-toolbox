#!/usr/bin/env python3
"""Strict, operator-only campaign history extractor and DAS loader wrapper."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any, Sequence


MAX_SOURCE_BYTES = 1_048_576
MAX_ASSERTIONS = 2_000
MAX_LINE_BYTES = 2_048
MAX_TOKENS = 32
MAX_DEPTH = 4
MAX_VALUE_CHARS = 256
MAX_HISTORY_LINES = 10_000
MAX_HISTORY_ENTRIES = 5_000
MAX_HISTORY_ENTRY_BYTES = 131_072
MAX_HISTORY_TOKENS = 4_096
MAX_HISTORY_DEPTH = 32
MAX_HISTORY_STRING_CHARS = 65_536
IMPORT_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{2,63}\Z")
HISTORY_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\Z")
HISTORY_RECORD_START_RE = re.compile(r'\("\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"')
HISTORY_STV_VALUE_RE = re.compile(r"(?:0(?:\.\d+)?|1(?:\.0+)?)\Z")
VALUE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.,:/#@+'-]{0,255}\Z")
FORBIDDEN_RE = re.compile(
    r"(?i)(?:^|[^a-z0-9_])(?:shell|remember|pin|websearch|query|metta|exec|eval|"
    r"system|spawn|include|import|load|assert|retract|delete|match|bind|let|do|"
    r"quote|unquote|superpose|collapse|pragma|random)(?:$|[^a-z0-9_])"
)
LOADER_FAILURE_RE = re.compile(
    r"(?im)^.*\b(?:error|fatal|failed|panic|traceback|exception)\b"
)

# Every accepted relation has an exact arity and exact typed-node constructors.
RELATION_SCHEMA: dict[str, tuple[str, ...]] = {
    "Inheritance": ("Concept", "Concept"),
    "Similarity": ("Concept", "Concept"),
    "Member": ("Concept", "Concept"),
    "Evaluation": ("Predicate", "Concept"),
    "Implication": ("Predicate", "Predicate"),
    "TemporalPrecedence": ("Event", "Event"),
    "CausalImplication": ("Event", "Event"),
}
PROVENANCE_TYPES = ("CampaignImport", "CampaignSource", "CampaignFact", "CampaignLine")
PROVENANCE_RELATION = "CampaignProvenance"


class ValidationError(ValueError):
    """The source does not belong to the accepted campaign assertion subset."""


class HistorySkip(ValueError):
    """A history candidate is not in the conservatively extractable subset."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def loader_output_has_failure(output: str) -> bool:
    return LOADER_FAILURE_RE.search(output) is not None


def _tokenize(line: str, line_number: int) -> list[object]:
    tokens: list[object] = []
    index = 0
    while index < len(line):
        char = line[index]
        if char.isspace():
            index += 1
            continue
        if char in "()":
            tokens.append(char)
            index += 1
            continue
        if char == '"':
            start = index
            index += 1
            escaped = False
            while index < len(line):
                current = line[index]
                if current == '"' and not escaped:
                    index += 1
                    break
                if current == "\\" and not escaped:
                    escaped = True
                else:
                    escaped = False
                index += 1
            else:
                raise ValidationError(f"line {line_number}: unterminated string")
            encoded = line[start:index]
            try:
                value = json.loads(encoded)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise ValidationError(f"line {line_number}: invalid string escape") from error
            tokens.append(("string", value))
            continue
        start = index
        while index < len(line) and not line[index].isspace() and line[index] not in "()":
            index += 1
        tokens.append(("symbol", line[start:index]))
    if len(tokens) > MAX_TOKENS:
        raise ValidationError(f"line {line_number}: exceeds {MAX_TOKENS} tokens")
    return tokens


def _parse(tokens: Sequence[object], line_number: int) -> list[Any]:
    position = 0

    def expression(depth: int) -> Any:
        nonlocal position
        if depth > MAX_DEPTH:
            raise ValidationError(f"line {line_number}: exceeds nesting depth {MAX_DEPTH}")
        if position >= len(tokens):
            raise ValidationError(f"line {line_number}: incomplete expression")
        token = tokens[position]
        position += 1
        if token == "(":
            result = []
            while position < len(tokens) and tokens[position] != ")":
                result.append(expression(depth + 1))
            if position >= len(tokens):
                raise ValidationError(f"line {line_number}: missing closing parenthesis")
            position += 1
            if not result:
                raise ValidationError(f"line {line_number}: empty expression")
            return result
        if token == ")":
            raise ValidationError(f"line {line_number}: unexpected closing parenthesis")
        return token

    parsed = expression(0)
    if position != len(tokens):
        raise ValidationError(f"line {line_number}: more than one assertion")
    if not isinstance(parsed, list):
        raise ValidationError(f"line {line_number}: assertion must be a list")
    return parsed


def _symbol(value: Any, line_number: int) -> str:
    if not (isinstance(value, tuple) and value[0] == "symbol"):
        raise ValidationError(f"line {line_number}: expected a schema symbol")
    symbol = value[1]
    if not symbol or symbol.startswith(("$", "%", "?")):
        raise ValidationError(f"line {line_number}: variables are forbidden")
    return symbol


def _string(value: Any, line_number: int) -> str:
    if not (isinstance(value, tuple) and value[0] == "string"):
        raise ValidationError(f"line {line_number}: values must be quoted strings")
    text = value[1]
    if not isinstance(text, str) or len(text) > MAX_VALUE_CHARS or not VALUE_RE.fullmatch(text):
        raise ValidationError(f"line {line_number}: malformed or oversized value")
    if FORBIDDEN_RE.search(text):
        raise ValidationError(f"line {line_number}: forbidden term in value")
    return text


def normalize_assertion(line: str, line_number: int) -> str:
    if FORBIDDEN_RE.search(line) or any(marker in line for marker in ("$", "%", "?", "!", "`")):
        raise ValidationError(f"line {line_number}: wrapper, operator, or variable is forbidden")
    parsed = _parse(_tokenize(line, line_number), line_number)
    relation = _symbol(parsed[0], line_number)
    expected = RELATION_SCHEMA.get(relation)
    if expected is None or len(parsed) != len(expected) + 1:
        raise ValidationError(f"line {line_number}: relation or arity is not allowed")
    normalized_arguments = []
    for argument, constructor in zip(parsed[1:], expected, strict=True):
        if not isinstance(argument, list) or len(argument) != 2:
            raise ValidationError(f"line {line_number}: expected ({constructor} \"value\")")
        if _symbol(argument[0], line_number) != constructor:
            raise ValidationError(f"line {line_number}: unexpected node constructor")
        value = json.dumps(_string(argument[1], line_number), ensure_ascii=True)
        normalized_arguments.append(f"({constructor} {value})")
    return f"({relation} {' '.join(normalized_arguments)})"


def _read_source(source: Path) -> bytes:
    if source.name != "history.metta":
        raise ValidationError("source basename must be history.metta")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValidationError("source must be a regular file")
        if metadata.st_size == 0 or metadata.st_size > MAX_SOURCE_BYTES:
            raise ValidationError(f"source size must be 1..{MAX_SOURCE_BYTES} bytes")
        data = b""
        while len(data) <= MAX_SOURCE_BYTES:
            chunk = os.read(descriptor, min(65_536, MAX_SOURCE_BYTES + 1 - len(data)))
            if not chunk:
                break
            data += chunk
        if len(data) > MAX_SOURCE_BYTES:
            raise ValidationError(f"source exceeds {MAX_SOURCE_BYTES} bytes")
        return data
    finally:
        os.close(descriptor)


def _canonical_document(
    records: list[dict[str, Any]], import_id: str, source_hash: str
) -> str:
    output: list[str] = []
    for type_name in sorted({item for schema in RELATION_SCHEMA.values() for item in schema}):
        output.append(f"(: {type_name} Type)")
    for relation in RELATION_SCHEMA:
        output.append(f"(: {relation} Type)")
    for type_name in PROVENANCE_TYPES:
        output.append(f"(: {type_name} Type)")
    output.append(f"(: {PROVENANCE_RELATION} Type)")
    import_value = f"campaign-import:{import_id}"
    source_value = f"sha256:{source_hash}"
    output.extend(
        (
            f"(: {json.dumps(import_value)} CampaignImport)",
            f"(: {json.dumps(source_value)} CampaignSource)",
        )
    )
    for record in records:
        fact_value = f"sha256:{record['canonical_sha256']}"
        line_value = record.get("source_locator", f"line:{record['source_line']}")
        output.extend(
            (
                record["canonical_assertion"],
                f"(: {json.dumps(fact_value)} CampaignFact)",
                f"(: {json.dumps(line_value)} CampaignLine)",
                f"({PROVENANCE_RELATION} {json.dumps(fact_value)} {json.dumps(source_value)} "
                f"{json.dumps(line_value)} {json.dumps(import_value)})",
            )
        )
    return "\n".join(output) + "\n"


def extract(source: Path, import_id: str) -> tuple[str, dict[str, Any]]:
    if not IMPORT_ID_RE.fullmatch(import_id) or FORBIDDEN_RE.search(import_id):
        raise ValidationError("import ID must be 3..64 lowercase letters, digits, or hyphens")
    data = _read_source(source)
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ValidationError("source must be UTF-8") from error
    if not text.endswith("\n"):
        raise ValidationError("source must end with a newline")
    raw_lines = text.splitlines()
    if not raw_lines or len(raw_lines) > MAX_ASSERTIONS:
        raise ValidationError(f"source must contain 1..{MAX_ASSERTIONS} assertions")

    source_hash = sha256_bytes(data)
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    relation_counts = {name: 0 for name in RELATION_SCHEMA}
    for number, line in enumerate(raw_lines, 1):
        if not line or line != line.strip():
            raise ValidationError(
                f"line {number}: blank lines and surrounding whitespace are forbidden"
            )
        if len(line.encode("utf-8")) > MAX_LINE_BYTES:
            raise ValidationError(f"line {number}: exceeds {MAX_LINE_BYTES} bytes")
        canonical = normalize_assertion(line, number)
        if canonical in seen:
            raise ValidationError(f"line {number}: duplicate assertion")
        seen.add(canonical)
        relation = canonical[1:].split(" ", 1)[0]
        relation_counts[relation] += 1
        fact_hash = sha256_bytes(canonical.encode("utf-8"))
        records.append(
            {
                "source_line": number,
                "source_line_sha256": sha256_bytes(line.encode("utf-8")),
                "canonical_assertion": canonical,
                "canonical_sha256": fact_hash,
            }
        )

    canonical_text = _canonical_document(records, import_id, source_hash)
    manifest = {
        "schema_version": 1,
        "import_id": import_id,
        "source": {
            "basename": source.name,
            "sha256": source_hash,
            "bytes": len(data),
            "assertion_count": len(records),
        },
        "limits": {
            "source_bytes": MAX_SOURCE_BYTES,
            "assertions": MAX_ASSERTIONS,
            "line_bytes": MAX_LINE_BYTES,
            "tokens_per_line": MAX_TOKENS,
            "depth": MAX_DEPTH,
            "value_chars": MAX_VALUE_CHARS,
        },
        "allowed_relations": {key: list(value) for key, value in RELATION_SCHEMA.items()},
        "relation_counts": relation_counts,
        "facts": records,
        "canonical": {
            "sha256": sha256_bytes(canonical_text.encode("utf-8")),
            "bytes": len(canonical_text.encode("utf-8")),
            "line_count": len(canonical_text.splitlines()),
        },
        "verification_plan": {
            "method": (
                "read-only POST /v1/query from an approved container on "
                "omega-das-integration-client"
            ),
            "checks": [
                "query one imported semantic assertion per nonzero relation count",
                "query CampaignProvenance by source hash and import ID",
                "confirm pre-existing representative read query still succeeds",
                "inspect captured loader output and backend service logs for errors",
            ],
        },
    }
    return canonical_text, manifest


def _history_entries(text: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    index = 0
    line = 1
    while index < len(text):
        if text[index].isspace():
            line += text[index] == "\n"
            index += 1
            continue
        if len(entries) >= MAX_HISTORY_ENTRIES:
            raise ValidationError(f"history exceeds {MAX_HISTORY_ENTRIES} entries")
        start = index
        start_line = line
        if text[index] != "(":
            newline = text.find("\n", index)
            index = len(text) if newline < 0 else newline
            raw = text[start:index]
            entries.append(
                {
                    "kind": "plain_text",
                    "start_line": start_line,
                    "end_line": start_line,
                    "raw": raw,
                }
            )
            continue

        depth = 0
        in_string = False
        escaped = False
        complete = False
        recovered_at_record_boundary = False
        while index < len(text):
            char = text[index]
            if (
                char == "\n"
                and not in_string
                and depth > 0
                and HISTORY_RECORD_START_RE.match(text, index + 1)
            ):
                line += 1
                index += 1
                recovered_at_record_boundary = True
                break
            if in_string:
                if char == '"' and not escaped:
                    in_string = False
                if char == "\\" and not escaped:
                    escaped = True
                else:
                    escaped = False
            elif char == '"':
                in_string = True
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth < 0:
                    break
                if depth == 0:
                    index += 1
                    complete = True
                    break
            line += char == "\n"
            index += 1
        raw = text[start:index]
        if len(raw.encode("utf-8")) > MAX_HISTORY_ENTRY_BYTES:
            kind = "oversized_form"
        elif not complete or in_string or depth != 0:
            kind = "malformed_form"
        else:
            kind = "form"
        entries.append(
            {
                "kind": kind,
                "start_line": start_line,
                "end_line": start_line + raw.count("\n"),
                "raw": raw,
            }
        )
        if not complete and not recovered_at_record_boundary:
            break
    return entries


def _decode_history_string(encoded: str, line_number: int) -> str:
    normalized = (
        encoded.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    )
    try:
        value = json.loads(normalized)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValidationError(f"line {line_number}: invalid history string") from error
    if not isinstance(value, str) or len(value) > MAX_HISTORY_STRING_CHARS:
        raise ValidationError(f"line {line_number}: oversized history string")
    return value


def _tokenize_history(text: str, line_number: int) -> list[object]:
    tokens: list[object] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if char in "()":
            tokens.append(char)
            index += 1
        elif char == '"':
            start = index
            index += 1
            escaped = False
            while index < len(text):
                current = text[index]
                if current == '"' and not escaped:
                    index += 1
                    break
                if current == "\\" and not escaped:
                    escaped = True
                else:
                    escaped = False
                index += 1
            else:
                raise ValidationError(f"line {line_number}: unterminated history string")
            tokens.append(("string", _decode_history_string(text[start:index], line_number)))
        else:
            start = index
            while index < len(text) and not text[index].isspace() and text[index] not in "()":
                index += 1
            tokens.append(("symbol", text[start:index]))
        if len(tokens) > MAX_HISTORY_TOKENS:
            raise ValidationError(
                f"line {line_number}: history entry exceeds {MAX_HISTORY_TOKENS} tokens"
            )
    return tokens


def _parse_history(tokens: Sequence[object], line_number: int) -> Any:
    position = 0

    def expression(depth: int) -> Any:
        nonlocal position
        if depth > MAX_HISTORY_DEPTH:
            raise ValidationError(
                f"line {line_number}: history entry exceeds nesting depth {MAX_HISTORY_DEPTH}"
            )
        if position >= len(tokens):
            raise ValidationError(f"line {line_number}: incomplete history expression")
        token = tokens[position]
        position += 1
        if token == "(":
            result = []
            while position < len(tokens) and tokens[position] != ")":
                result.append(expression(depth + 1))
            if position >= len(tokens):
                raise ValidationError(f"line {line_number}: missing history parenthesis")
            position += 1
            return result
        if token == ")":
            raise ValidationError(f"line {line_number}: unexpected history parenthesis")
        return token

    parsed = expression(0)
    if position != len(tokens):
        raise ValidationError(f"line {line_number}: multiple history expressions")
    return parsed


def _history_symbol(value: Any) -> str | None:
    if isinstance(value, tuple) and len(value) == 2 and value[0] == "symbol":
        return value[1]
    return None


def _render_history_expression(value: Any) -> str:
    if isinstance(value, tuple) and value[0] == "symbol":
        return value[1]
    if isinstance(value, tuple) and value[0] == "string":
        return json.dumps(value[1], ensure_ascii=True)
    if isinstance(value, list):
        return "(" + " ".join(_render_history_expression(item) for item in value) + ")"
    raise HistorySkip("unrenderable_candidate")


def _pln_concept(value: Any) -> str:
    symbol = _history_symbol(value)
    if symbol is not None:
        if not symbol or symbol.startswith(("$", "%", "?")):
            raise HistorySkip("variable_operand")
        return symbol
    if isinstance(value, list) and len(value) == 2:
        constructor = _history_symbol(value[0])
        item = _history_symbol(value[1])
        if constructor == "Concept" and item is not None:
            if not item or item.startswith(("$", "%", "?")):
                raise HistorySkip("variable_operand")
            return item
        if constructor in {"IntSet", "ExtSet"} and item is not None:
            if not item or item.startswith(("$", "%", "?")):
                raise HistorySkip("variable_operand")
            return f"{constructor}:{item}"
    raise HistorySkip("unsupported_operand")


def _normalize_history_candidate(candidate: list[Any], line_number: int) -> str:
    relation = _history_symbol(candidate[0]) if candidate else None
    if relation not in RELATION_SCHEMA:
        raise HistorySkip("not_allowlisted_relation")
    rendered = _render_history_expression(candidate)
    try:
        return normalize_assertion(rendered, line_number)
    except ValidationError:
        pass
    if relation != "Inheritance":
        raise HistorySkip("not_canonical_typed_shape")
    if len(candidate) != 3:
        raise HistorySkip("wrong_arity")
    left = _pln_concept(candidate[1])
    right = _pln_concept(candidate[2])
    converted = (
        f"(Inheritance (Concept {json.dumps(left, ensure_ascii=True)}) "
        f"(Concept {json.dumps(right, ensure_ascii=True)}))"
    )
    try:
        return normalize_assertion(converted, line_number)
    except ValidationError as error:
        raise HistorySkip("canonical_validation_rejected") from error


def _history_command_name(command: Any) -> str:
    if not isinstance(command, list) or not command:
        return "malformed"
    name = _history_symbol(command[0])
    if name is None or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", name):
        return "other"
    return name


def _valid_history_timestamp(value: str) -> bool:
    if HISTORY_TIMESTAMP_RE.fullmatch(value) is None:
        return False
    try:
        dt.datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return True


def _is_history_stv(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 3
        and _history_symbol(value[0]) == "stv"
        and all(
            (symbol := _history_symbol(item)) is not None
            and HISTORY_STV_VALUE_RE.fullmatch(symbol) is not None
            for item in value[1:]
        )
    )


def _history_relation_nodes(value: Any) -> list[list[Any]]:
    """Find semantic-looking nodes for rejection accounting, never extraction."""
    found: list[list[Any]] = []
    if not isinstance(value, list):
        return found
    if value and _history_symbol(value[0]) in RELATION_SCHEMA:
        found.append(value)
    for child in value:
        found.extend(_history_relation_nodes(child))
    return found


def _increment(counter: dict[str, int], key: str) -> None:
    counter[key] = counter.get(key, 0) + 1


def extract_history(source: Path, import_id: str) -> tuple[str, dict[str, Any]]:
    """Lexically extract safe facts from inert Omega campaign history."""
    if not IMPORT_ID_RE.fullmatch(import_id) or FORBIDDEN_RE.search(import_id):
        raise ValidationError("import ID must be 3..64 lowercase letters, digits, or hyphens")
    data = _read_source(source)
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ValidationError("source must be UTF-8") from error
    if not text.endswith("\n"):
        raise ValidationError("source must end with a newline")
    line_count = len(text.splitlines())
    if not 1 <= line_count <= MAX_HISTORY_LINES:
        raise ValidationError(f"history must contain 1..{MAX_HISTORY_LINES} lines")

    entries = _history_entries(text)
    source_hash = sha256_bytes(data)
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    relation_counts = {name: 0 for name in RELATION_SCHEMA}
    entry_counts: dict[str, int] = {}
    record_counts: dict[str, int] = {}
    command_counts: dict[str, int] = {}
    payload_reasons: dict[str, int] = {}
    candidate_reasons: dict[str, int] = {}
    payload_count = 0
    parsed_payload_count = 0
    candidate_count = 0

    for entry_number, entry in enumerate(entries, 1):
        _increment(entry_counts, entry["kind"])
        if entry["kind"] != "form":
            continue
        try:
            parsed = _parse_history(
                _tokenize_history(entry["raw"], entry["start_line"]), entry["start_line"]
            )
        except ValidationError:
            _increment(record_counts, "unparseable_form")
            continue
        if not (
            isinstance(parsed, list)
            and len(parsed) == 2
            and isinstance(parsed[0], tuple)
            and parsed[0][0] == "string"
            and _valid_history_timestamp(parsed[0][1])
            and isinstance(parsed[1], list)
        ):
            _increment(record_counts, "non_campaign_form")
            continue
        _increment(record_counts, "campaign_record")
        for command in parsed[1]:
            command_name = _history_command_name(command)
            _increment(command_counts, command_name)
            if command_name != "metta":
                continue
            payload_count += 1
            if not (
                isinstance(command, list)
                and len(command) == 2
                and isinstance(command[1], tuple)
                and command[1][0] == "string"
            ):
                _increment(payload_reasons, "malformed_metta_command")
                continue
            payload = command[1][1]
            try:
                payload_ast = _parse_history(
                    _tokenize_history(payload, entry["start_line"]), entry["start_line"]
                )
            except ValidationError:
                _increment(payload_reasons, "unparseable_payload")
                continue
            parsed_payload_count += 1
            payload_hash = sha256_bytes(payload.encode("utf-8"))
            entry_hash = sha256_bytes(entry["raw"].encode("utf-8"))

            def consider_candidate(
                node: list[Any],
                context: str,
                rejection_reason: str | None = None,
            ) -> None:
                nonlocal candidate_count
                if not node or _history_symbol(node[0]) not in RELATION_SCHEMA:
                    return
                candidate_count += 1
                if rejection_reason is not None:
                    _increment(candidate_reasons, rejection_reason)
                    return
                try:
                    candidate_text = _render_history_expression(node)
                    canonical = _normalize_history_candidate(
                        node, entry["start_line"]
                    )
                except HistorySkip as error:
                    _increment(candidate_reasons, str(error))
                else:
                    if canonical in seen:
                        _increment(candidate_reasons, "duplicate_canonical_fact")
                    elif len(records) >= MAX_ASSERTIONS:
                        raise ValidationError(
                            f"history extraction exceeds {MAX_ASSERTIONS} facts"
                        )
                    else:
                        seen.add(canonical)
                        relation_name = canonical[1:].split(" ", 1)[0]
                        relation_counts[relation_name] += 1
                        canonical_hash = sha256_bytes(canonical.encode("utf-8"))
                        locator = (
                            f"line:{entry['start_line']}"
                            if entry["start_line"] == entry["end_line"]
                            else f"lines:{entry['start_line']}-{entry['end_line']}"
                        )
                        records.append(
                            {
                                "source_entry": entry_number,
                                "source_line": entry["start_line"],
                                "source_end_line": entry["end_line"],
                                "source_locator": locator,
                                "source_entry_sha256": entry_hash,
                                "metta_payload_sha256": payload_hash,
                                "candidate_sha256": sha256_bytes(
                                    candidate_text.encode("utf-8")
                                ),
                                "canonical_assertion": canonical,
                                "canonical_sha256": canonical_hash,
                                "extraction_context": context,
                            }
                        )

            def reject_nested(value: Any, reason: str) -> None:
                for candidate in _history_relation_nodes(value):
                    consider_candidate(candidate, "rejected", reason)

            payload_relation = (
                _history_symbol(payload_ast[0])
                if isinstance(payload_ast, list) and payload_ast
                else None
            )
            if payload_relation in RELATION_SCHEMA:
                consider_candidate(payload_ast, "payload_root")
            elif payload_relation == "|~":
                for evidence in payload_ast[1:]:
                    if not (
                        isinstance(evidence, list)
                        and len(evidence) == 2
                        and isinstance(evidence[0], list)
                        and _is_history_stv(evidence[1])
                    ):
                        reject_nested(evidence, "malformed_evidence_group")
                        continue
                    assertion = evidence[0]
                    if _history_symbol(assertion[0]) in RELATION_SCHEMA:
                        consider_candidate(assertion, "pln_evidence")
                    else:
                        reject_nested(assertion, "unsupported_evidence_assertion")
            elif payload_relation == "add-atom":
                if len(payload_ast) not in {3, 4}:
                    reject_nested(payload_ast[1:], "malformed_persistent_wrapper")
                elif _history_symbol(payload_ast[1]) != "&persistent":
                    reject_nested(payload_ast[2:], "wrong_persistent_space")
                elif not isinstance(payload_ast[2], list) or not payload_ast[2]:
                    reject_nested(payload_ast[2:], "unsupported_persistent_assertion")
                elif len(payload_ast) == 4 and not _is_history_stv(payload_ast[3]):
                    reject_nested(payload_ast[2:], "malformed_truth_value")
                elif _history_symbol(payload_ast[2][0]) in RELATION_SCHEMA:
                    consider_candidate(payload_ast[2], "persistent_add_atom")
                else:
                    reject_nested(
                        payload_ast[2], "unsupported_persistent_assertion"
                    )
            else:
                reject_nested(payload_ast, "unsafe_parent_context")

    if not records:
        raise ValidationError("history contains no safely extractable semantic facts")
    canonical_text = _canonical_document(records, import_id, source_hash)
    skipped_candidates = sum(candidate_reasons.values())
    command_count = sum(command_counts.values())
    manifest = {
        "schema_version": 2,
        "import_id": import_id,
        "source": {
            "basename": source.name,
            "sha256": source_hash,
            "bytes": len(data),
            "line_count": line_count,
            "entry_count": len(entries),
            "assertion_count": len(records),
        },
        "extraction": {
            "mode": "omega-history-lexical-v1",
            "entry_counts": entry_counts,
            "record_counts": record_counts,
            "command_counts": command_counts,
            "commands": {
                "seen": command_count,
                "metta_seen": payload_count,
                "ignored": command_count - payload_count,
                "ignore_reasons": {
                    "non_metta_command": command_count - payload_count
                },
            },
            "metta_payloads": {
                "seen": payload_count,
                "parsed": parsed_payload_count,
                "skipped": payload_count - parsed_payload_count,
                "skip_reasons": payload_reasons,
            },
            "semantic_candidates": {
                "seen": candidate_count,
                "accepted_unique": len(records),
                "skipped": skipped_candidates,
                "skip_reasons": candidate_reasons,
            },
        },
        "limits": {
            "source_bytes": MAX_SOURCE_BYTES,
            "history_lines": MAX_HISTORY_LINES,
            "history_entries": MAX_HISTORY_ENTRIES,
            "history_entry_bytes": MAX_HISTORY_ENTRY_BYTES,
            "history_tokens_per_entry": MAX_HISTORY_TOKENS,
            "history_depth": MAX_HISTORY_DEPTH,
            "history_string_chars": MAX_HISTORY_STRING_CHARS,
            "extracted_assertions": MAX_ASSERTIONS,
            "canonical_value_chars": MAX_VALUE_CHARS,
        },
        "allowed_relations": {key: list(value) for key, value in RELATION_SCHEMA.items()},
        "relation_counts": relation_counts,
        "facts": records,
        "canonical": {
            "sha256": sha256_bytes(canonical_text.encode("utf-8")),
            "bytes": len(canonical_text.encode("utf-8")),
            "line_count": len(canonical_text.splitlines()),
        },
        "verification_plan": {
            "method": (
                "read-only POST /v1/query from an approved container on "
                "omega-das-integration-client"
            ),
            "checks": [
                "query one imported semantic assertion per nonzero relation count",
                "query CampaignProvenance by source hash and import ID",
                "confirm manifest extraction counts and hashes against retained source",
                "confirm pre-existing representative read query still succeeds",
                "inspect captured loader output and backend service logs for errors",
            ],
        },
    }
    incomplete = history_parse_incompleteness(manifest)
    manifest["extraction"]["parse_incompleteness"] = {
        "count": sum(incomplete.values()),
        "reasons": incomplete,
    }
    return canonical_text, manifest


def history_parse_incompleteness(manifest: dict[str, Any]) -> dict[str, int]:
    """Return parse failures that can make a history import incomplete."""
    extraction = manifest.get("extraction", {})
    entry_counts = extraction.get("entry_counts", {})
    record_counts = extraction.get("record_counts", {})
    payload_reasons = extraction.get("metta_payloads", {}).get("skip_reasons", {})
    counts = {
        "malformed_form": entry_counts.get("malformed_form", 0),
        "oversized_form": entry_counts.get("oversized_form", 0),
        "unparseable_form": record_counts.get("unparseable_form", 0),
        "malformed_metta_command": payload_reasons.get("malformed_metta_command", 0),
        "unparseable_payload": payload_reasons.get("unparseable_payload", 0),
    }
    return {key: value for key, value in counts.items() if value}


def _write_json(path: Path, document: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _run(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=check,
        env=env,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        timeout=1_800,
    )


def _archive_metadata(backup_dir: Path) -> list[dict[str, Any]]:
    result = []
    for name in ("mongodb.tar.gz", "redis.tar.gz"):
        path = backup_dir / name
        size = path.stat().st_size
        if not size:
            raise RuntimeError(f"empty backup archive: {name}")
        result.append({"file": name, "bytes": size, "sha256": sha256_file(path)})
    return result


def apply_import(
    base_dir: Path,
    env_file: Path,
    source: Path,
    import_id: str,
    *,
    history_extraction: bool = False,
    allow_partial_history: bool = False,
) -> Path:
    # Validate the complete source and generate canonical assertions before
    # reserving state or invoking Docker. The loader never receives the source.
    extractor = extract_history if history_extraction else extract
    canonical, manifest = extractor(source, import_id)
    if history_extraction:
        incomplete = history_parse_incompleteness(manifest)
        if incomplete and not allow_partial_history:
            raise ValidationError(
                "history contains malformed or unparseable records; inspect the dry-run "
                "manifest and pass --allow-partial-history to acknowledge a partial import"
            )
        manifest["extraction"]["partial_history_acknowledged"] = bool(
            incomplete and allow_partial_history
        )
    state_root = base_dir / "state" / "campaign-imports"
    state_root.mkdir(parents=True, exist_ok=True)
    import_dir = state_root / import_id
    try:
        import_dir.mkdir(mode=0o700)
    except FileExistsError as error:
        raise RuntimeError(f"duplicate import ID: {import_id}") from error

    canonical_path = import_dir / "campaign.metta"
    canonical_path.write_text(canonical, encoding="utf-8")
    canonical_path.chmod(0o444)
    backup_dir = import_dir / "backup"
    backup_dir.mkdir(mode=0o700)
    manifest.update(
        {"status": "prepared", "created_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    )
    manifest_path = import_dir / "manifest.json"
    _write_json(manifest_path, manifest)

    compose = [
        "docker",
        "compose",
        "--env-file",
        str(env_file),
        "-f",
        str(base_dir / "compose.yaml"),
    ]
    environment = os.environ.copy()
    environment["CAMPAIGN_IMPORT_FILE"] = str(canonical_path.resolve())
    environment["CAMPAIGN_BACKUP_DIR"] = str(backup_dir.resolve())
    try:
        _run([str(base_dir / "scripts" / "preflight.sh"), str(env_file)])
        running = set(
            _run(
                compose + ["ps", "--status", "running", "--services"], capture=True
            ).stdout.splitlines()
        )
        required = {"mongodb", "redis", "attention-broker", "query-engine", "read-proxy"}
        if not required.issubset(running):
            missing = sorted(required - running)
            raise RuntimeError(f"deployment is not fully running; missing: {missing}")

        try:
            # Close every query path before touching datastore state. Keep both
            # query services down until the loader has completed (or failed).
            _run(compose + ["stop", "read-proxy"])
            _run(compose + ["stop", "query-engine"])
            manifest["status"] = "backing-up"
            _write_json(manifest_path, manifest)
            _run(compose + ["stop", "mongodb", "redis"])
            _run(
                compose
                + ["--profile", "operator", "run", "--rm", "--no-deps", "campaign-backup"],
                env=environment,
            )
            manifest["backup"] = {"offline_volume_archives": _archive_metadata(backup_dir)}
            _run(compose + ["up", "-d", "--wait", "mongodb", "redis"])
            manifest["status"] = "loading"
            _write_json(manifest_path, manifest)
            loader = _run(
                compose + ["--profile", "operator", "run", "--rm", "--no-deps", "campaign-loader"],
                env=environment,
                capture=True,
                check=False,
            )
            loader_output = loader.stdout or ""
            sys.stdout.write(loader_output)
            loader_output_path = import_dir / "loader-output.log"
            loader_output_path.write_text(loader_output, encoding="utf-8")
            loader_output_path.chmod(0o600)
            manifest["loader_exit_code"] = loader.returncode
            manifest["loader_output"] = {
                "file": loader_output_path.name,
                "bytes": len(loader_output.encode("utf-8")),
                "sha256": sha256_bytes(loader_output.encode("utf-8")),
            }
            if loader.returncode != 0:
                raise RuntimeError(f"loader exited with status {loader.returncode}")
            if loader_output_has_failure(loader_output):
                raise RuntimeError("loader output contains a terminal failure marker")
        finally:
            # The fully-running precondition makes this restoration safe even
            # when stopping, backup, datastore startup, or loading failed.
            _run(
                compose
                + [
                    "up", "-d", "--wait", "mongodb", "redis", "attention-broker",
                    "query-engine", "read-proxy",
                ]
            )
        manifest["status"] = "loaded-unverified"
        manifest["completed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        _write_json(manifest_path, manifest)
        return manifest_path
    except Exception as error:
        manifest["status"] = "failed"
        manifest["failed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        manifest["failure_type"] = type(error).__name__
        manifest["failure"] = str(error)[:1_024]
        _write_json(manifest_path, manifest)
        raise


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and print a manifest without Docker or persistent files",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="back up the datastore and run the fixed one-shot loader",
    )
    parser.add_argument("import_id")
    parser.add_argument("source", type=Path)
    parser.add_argument(
        "--extract-omega-history",
        action="store_true",
        help="lexically extract safe facts from timestamped Omega campaign history",
    )
    parser.add_argument(
        "--allow-partial-history",
        action="store_true",
        help=(
            "with --apply --extract-omega-history, acknowledge malformed or "
            "unparseable history records and import only safely extracted facts"
        ),
    )
    parser.add_argument("--env-file", type=Path, help="deployment environment file (apply only)")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    base_dir = Path(__file__).resolve().parent.parent
    source = args.source.absolute()
    if source.is_symlink():
        raise ValidationError("source must not be a symbolic link")
    if args.allow_partial_history and not (args.apply and args.extract_omega_history):
        raise ValidationError(
            "--allow-partial-history requires --apply --extract-omega-history"
        )
    if args.dry_run:
        if args.env_file is not None:
            raise ValidationError("--env-file is valid only with --apply")
        extractor = extract_history if args.extract_omega_history else extract
        canonical, manifest = extractor(source, args.import_id)
        manifest["status"] = "dry-run"
        manifest["canonical_preview"] = canonical.splitlines()[:20]
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return 0
    env_file = (args.env_file or (base_dir / ".env")).resolve(strict=True)
    manifest_path = apply_import(
        base_dir,
        env_file,
        source,
        args.import_id,
        history_extraction=args.extract_omega_history,
        allow_partial_history=args.allow_partial_history,
    )
    print(f"load completed but requires read-only verification; manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValidationError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"campaign import failed: {error}", file=sys.stderr)
        raise SystemExit(1)
