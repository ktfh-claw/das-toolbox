#!/usr/bin/env python3
"""Unit tests for the bounded campaign history extractor."""

from __future__ import annotations

import importlib.util
import contextlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock


deployment_dir = Path(__file__).resolve().parent.parent
module_path = deployment_dir / "scripts" / "campaign_import.py"
spec = importlib.util.spec_from_file_location("campaign_import", module_path)
assert spec and spec.loader
campaign_import = importlib.util.module_from_spec(spec)
spec.loader.exec_module(campaign_import)

backup_spec = importlib.util.spec_from_file_location(
    "backup_volumes", deployment_dir / "scripts" / "backup-volumes.py"
)
assert backup_spec and backup_spec.loader
backup_volumes = importlib.util.module_from_spec(backup_spec)
backup_spec.loader.exec_module(backup_volumes)


class CampaignImportTests(unittest.TestCase):
    def source(self, content: str, *, name: str = "history.metta"):
        temporary = tempfile.TemporaryDirectory()
        path = Path(temporary.name) / name
        path.write_text(content, encoding="utf-8")
        self.addCleanup(temporary.cleanup)
        return path

    @staticmethod
    def history_record(timestamp: str, commands: str) -> str:
        return f'({json.dumps(timestamp)}\n ({commands})\n)\n'

    def test_extracts_only_allowed_facts_and_adds_provenance(self):
        source = self.source(
            '(Inheritance (Concept "scout") (Concept "operative"))\n'
            '(Evaluation (Predicate "has-role") (Concept "scout"))\n'
            '(TemporalPrecedence (Event "briefing") (Event "mission"))\n'
        )
        canonical, manifest = campaign_import.extract(source, "campaign-001")
        self.assertIn('(Inheritance (Concept "scout") (Concept "operative"))\n', canonical)
        self.assertIn("CampaignProvenance", canonical)
        self.assertIn("campaign-import:campaign-001", canonical)
        self.assertEqual(manifest["source"]["assertion_count"], 3)
        self.assertEqual(manifest["relation_counts"]["Inheritance"], 1)
        self.assertEqual(manifest["facts"][0]["source_line"], 1)
        self.assertEqual(len(manifest["source"]["sha256"]), 64)
        self.assertEqual(
            manifest["canonical"]["sha256"],
            campaign_import.sha256_bytes(canonical.encode("utf-8")),
        )

    def test_normalizes_json_string_escapes(self):
        source = self.source('(Member (Concept "field\\u0020agent") (Concept "team-7"))\n')
        canonical, _ = campaign_import.extract(source, "campaign-002")
        self.assertIn('(Member (Concept "field agent") (Concept "team-7"))', canonical)

    def test_rejects_effectful_wrappers_variables_and_unknown_relations(self):
        invalid = (
            '!(match &self (Inheritance (Concept "a") (Concept "b")) $x)',
            '(query (Concept "a") (Concept "b"))',
            '(Evaluation (Predicate "remember") (Concept "a"))',
            '(Unknown (Concept "a") (Concept "b"))',
            '(Inheritance (Concept $x) (Concept "b"))',
        )
        for index, assertion in enumerate(invalid):
            with self.subTest(assertion=assertion):
                source = self.source(assertion + "\n")
                with self.assertRaises(campaign_import.ValidationError):
                    campaign_import.extract(source, f"invalid-{index}")

    def test_rejects_malformed_shape_instead_of_skipping(self):
        source = self.source(
            '(Inheritance (Concept "valid") (Concept "fact"))\n'
            '(Inheritance "raw" (Concept "not-accepted"))\n'
        )
        with self.assertRaisesRegex(campaign_import.ValidationError, "line 2"):
            campaign_import.extract(source, "campaign-003")

    def test_rejects_blank_duplicate_oversized_and_wrong_basename(self):
        cases = (
            self.source('(Inheritance (Concept "a") (Concept "b"))\n\n'),
            self.source(
                '(Inheritance (Concept "a") (Concept "b"))\n'
                '(Inheritance (Concept "a") (Concept "b"))\n'
            ),
            self.source('(Inheritance (Concept "' + ("a" * 257) + '") (Concept "b"))\n'),
            self.source('(Inheritance (Concept "a") (Concept "b"))\n', name="other.metta"),
        )
        for source in cases:
            with self.subTest(source=source):
                with self.assertRaises(campaign_import.ValidationError):
                    campaign_import.extract(source, "campaign-004")

    def test_dry_run_does_not_create_state(self):
        source = self.source('(Similarity (Concept "a") (Concept "b"))\n')
        state = deployment_dir / "state"
        before = state.exists()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(campaign_import.main(["--dry-run", "campaign-005", str(source)]), 0)
        self.assertEqual(state.exists(), before)

    def test_rejects_symlink_and_forbidden_import_id(self):
        source = self.source('(Similarity (Concept "a") (Concept "b"))\n')
        link = source.parent / "linked-history.metta"
        link.symlink_to(source)
        with self.assertRaises(campaign_import.ValidationError):
            campaign_import.main(["--dry-run", "campaign-007", str(link)])
        with self.assertRaises(campaign_import.ValidationError):
            campaign_import.extract(source, "query-campaign")

    def test_loader_failure_markers_are_terminal_and_zero_errors_is_not(self):
        self.assertTrue(
            campaign_import.loader_output_has_failure("[ERROR] parser rejected line\n")
        )
        self.assertTrue(campaign_import.loader_output_has_failure("Failed to commit batch\n"))
        self.assertTrue(
            campaign_import.loader_output_has_failure(
                "Traceback (most recent call last)\n"
            )
        )
        self.assertFalse(campaign_import.loader_output_has_failure("loaded; 0 errors\n"))
        self.assertFalse(
            campaign_import.loader_output_has_failure("load completed successfully\n")
        )

    def test_duplicate_import_id_is_rejected_before_docker(self):
        source = self.source('(Similarity (Concept "a") (Concept "b"))\n')
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            import_dir = base / "state" / "campaign-imports" / "campaign-008"
            import_dir.mkdir(parents=True)
            with self.assertRaisesRegex(RuntimeError, "duplicate import ID"):
                campaign_import.apply_import(
                    base, base / ".env", source, "campaign-008"
                )

    def test_volume_archiver_preserves_empty_and_nonempty_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            empty = base / "mongodb"
            populated = base / "redis"
            empty.mkdir()
            populated.mkdir()
            (populated / "appendonly.aof").write_bytes(b"fixture")
            empty_archive = base / "mongodb.tar.gz"
            populated_archive = base / "redis.tar.gz"

            backup_volumes.archive(empty, empty_archive)
            backup_volumes.archive(populated, populated_archive)

            with tarfile.open(empty_archive, "r:gz") as archive:
                self.assertEqual(archive.getnames(), ["mongodb"])
            with tarfile.open(populated_archive, "r:gz") as archive:
                self.assertEqual(
                    archive.extractfile("redis/appendonly.aof").read(), b"fixture"
                )
            self.assertEqual(len(campaign_import.sha256_file(populated_archive)), 64)

    def test_manifest_is_json_serializable(self):
        source = self.source('(CausalImplication (Event "signal") (Event "response"))\n')
        _, manifest = campaign_import.extract(source, "campaign-006")
        json.dumps(manifest)

    def test_history_extracts_only_ground_facts_from_quoted_metta_payloads(self):
        payload = (
            "(|~ ((Implication (Inheritance $1 Bird) (Inheritance $1 Animal)) "
            "(stv 0.9 0.8)) ((Inheritance Penguin (IntSet Bird)) (stv 1.0 0.9)))"
        )
        typed_payload = '(Similarity (Concept "alpha") (Concept "beta"))'
        content = self.history_record(
            "2026-10-02 18:50:00",
            "(shell "
            + json.dumps('(Inheritance (Concept "shell-secret") (Concept "must-not-load"))')
            + ") (metta "
            + json.dumps(payload)
            + ") (metta "
            + json.dumps(typed_payload)
            + ")",
        )
        source = self.source(content)

        with mock.patch.object(campaign_import.subprocess, "run") as run:
            canonical, manifest = campaign_import.extract_history(source, "history-001")

        run.assert_not_called()
        self.assertIn(
            '(Inheritance (Concept "Penguin") (Concept "IntSet:Bird"))', canonical
        )
        self.assertIn('(Similarity (Concept "alpha") (Concept "beta"))', canonical)
        self.assertNotIn("shell-secret", canonical)
        self.assertNotIn("shell-secret", json.dumps(manifest))
        self.assertEqual(manifest["extraction"]["command_counts"]["shell"], 1)
        self.assertEqual(manifest["extraction"]["command_counts"]["metta"], 2)
        self.assertEqual(manifest["extraction"]["commands"]["ignored"], 1)
        self.assertEqual(
            manifest["extraction"]["commands"]["ignore_reasons"],
            {"non_metta_command": 1},
        )
        self.assertEqual(manifest["source"]["assertion_count"], 2)
        self.assertEqual(manifest["relation_counts"]["Inheritance"], 1)
        self.assertEqual(manifest["relation_counts"]["Similarity"], 1)
        fact = manifest["facts"][0]
        self.assertEqual(len(fact["source_entry_sha256"]), 64)
        self.assertEqual(len(fact["metta_payload_sha256"]), 64)
        self.assertEqual(len(fact["candidate_sha256"]), 64)

    def test_history_skips_variables_effectful_content_and_duplicates_by_reason(self):
        payload = (
            "(|~ ((Inheritance $1 Unsafe) (stv 1.0 0.9)) "
            "((Inheritance Safe Fact) (stv 1.0 0.9)) "
            "((Inheritance Safe Fact) (stv 1.0 0.9)) "
            "(Implication (Inheritance Conditional Only) (Inheritance Result Only)))"
        )
        content = self.history_record(
            "2026-10-02 18:50:00",
            "(websearch "
            + json.dumps("irrelevant")
            + ") (remember "
            + json.dumps("also inert")
            + ") (metta "
            + json.dumps(payload)
            + ")",
        )
        canonical, manifest = campaign_import.extract_history(
            self.source(content), "history-002"
        )
        self.assertNotIn("Conditional", canonical)
        self.assertNotIn("Result", canonical)
        reasons = manifest["extraction"]["semantic_candidates"]["skip_reasons"]
        self.assertGreaterEqual(reasons["variable_operand"], 1)
        self.assertGreaterEqual(reasons["duplicate_canonical_fact"], 1)
        self.assertGreaterEqual(reasons["not_canonical_typed_shape"], 1)
        self.assertGreaterEqual(reasons["unsafe_parent_context"], 2)
        candidates = manifest["extraction"]["semantic_candidates"]
        self.assertEqual(
            candidates["seen"], candidates["accepted_unique"] + candidates["skipped"]
        )

    def test_history_skips_malformed_commands_payloads_and_excessive_depth(self):
        too_deep = "(" * (campaign_import.MAX_HISTORY_DEPTH + 2) + "x" + ")" * (
            campaign_import.MAX_HISTORY_DEPTH + 2
        )
        content = self.history_record(
            "2026-10-02 18:50:00",
            "(metta "
            + json.dumps("(Inheritance Extra Argument)")
            + " trailing) (metta "
            + json.dumps("(Inheritance Decoy Fact) (Inheritance Also Decoy)")
            + ") (metta "
            + json.dumps(too_deep)
            + ") (metta "
            + json.dumps("(Inheritance Sound Fact)")
            + ")",
        )
        canonical, manifest = campaign_import.extract_history(
            self.source(content), "history-malformed"
        )
        self.assertIn("Sound", canonical)
        self.assertNotIn("Decoy", canonical)
        payloads = manifest["extraction"]["metta_payloads"]
        self.assertEqual(payloads["seen"], 4)
        self.assertEqual(payloads["parsed"], 1)
        self.assertEqual(payloads["skip_reasons"]["malformed_metta_command"], 1)
        self.assertEqual(payloads["skip_reasons"]["unparseable_payload"], 2)

    def test_history_never_reparses_strings_inside_a_metta_payload(self):
        payload = (
            '(quote "(Inheritance Forged PayloadFact)" '
            "(Inheritance AlsoForged PayloadFact))"
        )
        valid = "(Inheritance Real PayloadFact)"
        source = self.source(
            self.history_record(
                "2026-10-02 18:50:00",
                "(metta "
                + json.dumps(payload)
                + ") (metta "
                + json.dumps(valid)
                + ")",
            )
        )
        canonical, manifest = campaign_import.extract_history(source, "history-strings")
        self.assertNotIn("Forged", canonical)
        self.assertIn("Real", canonical)
        self.assertEqual(manifest["source"]["assertion_count"], 1)
        reasons = manifest["extraction"]["semantic_candidates"]["skip_reasons"]
        self.assertEqual(reasons["unsafe_parent_context"], 1)

    def test_history_recovers_after_unclosed_form_only_at_real_record_boundary(self):
        malformed = (
            '("2026-10-02 18:50:00"\n'
            ' ((metta "(Inheritance Broken Fact)"))\n'
            ' ERROR_FEEDBACK: ((syntax_error broken))\n'
        )
        valid = self.history_record(
            "2026-10-02 18:51:00", '(metta "(Inheritance Recovered Fact)")'
        )
        _, manifest = campaign_import.extract_history(
            self.source(malformed + valid), "history-003"
        )
        self.assertEqual(manifest["extraction"]["entry_counts"]["malformed_form"], 1)
        self.assertEqual(manifest["source"]["assertion_count"], 1)
        self.assertEqual(
            manifest["facts"][0]["canonical_assertion"],
            '(Inheritance (Concept "Recovered") (Concept "Fact"))',
        )

    def test_history_does_not_treat_timestamp_like_text_inside_string_as_record(self):
        embedded = (
            "decoy line\n"
            '("2026-10-02 18:51:00"\n'
            ' ((metta "(Inheritance Forged Fact)"))\n'
            ")"
        )
        first = self.history_record(
            "2026-10-02 18:50:00", "(shell " + json.dumps(embedded) + ")"
        )
        second = self.history_record(
            "2026-10-02 18:52:00", '(metta "(Inheritance Genuine Fact)")'
        )
        canonical, manifest = campaign_import.extract_history(
            self.source(first + second), "history-004"
        )
        self.assertNotIn("Forged", canonical)
        self.assertIn("Genuine", canonical)
        self.assertEqual(manifest["source"]["assertion_count"], 1)

    def test_history_mode_accepts_realistic_line_count_without_weakening_strict_mode(self):
        record = self.history_record(
            "2026-10-02 18:50:00", '(metta "(Inheritance Large History)")'
        )
        content = ("\n" * 2_100) + record
        source = self.source(content)
        canonical, manifest = campaign_import.extract_history(source, "history-005")
        self.assertIn("Large", canonical)
        self.assertGreater(manifest["source"]["line_count"], campaign_import.MAX_ASSERTIONS)
        with self.assertRaises(campaign_import.ValidationError):
            campaign_import.extract(source, "history-005-strict")

    def test_history_rejects_empty_unterminated_oversized_and_non_utf8_sources(self):
        cases = [
            '("2026-10-02 18:50:00" ((shell "no facts")))\n',
            self.history_record(
                "2026-10-02 18:50:00", '(metta "(Inheritance A B)")'
            ).rstrip("\n"),
        ]
        for index, content in enumerate(cases):
            with self.subTest(index=index):
                with self.assertRaises(campaign_import.ValidationError):
                    campaign_import.extract_history(
                        self.source(content), f"history-empty-{index}"
                    )

        non_utf8 = self.source("placeholder\n")
        non_utf8.write_bytes(b"\xff\n")
        with self.assertRaisesRegex(campaign_import.ValidationError, "UTF-8"):
            campaign_import.extract_history(non_utf8, "history-non-utf8")

        oversized = self.source("x" * (campaign_import.MAX_SOURCE_BYTES + 1))
        with self.assertRaises(campaign_import.ValidationError):
            campaign_import.extract_history(oversized, "history-oversized")

    def test_history_cli_requires_explicit_mode_and_dry_run_writes_no_state(self):
        source = self.source(
            self.history_record(
                "2026-10-02 18:50:00", '(metta "(Inheritance Explicit Mode)")'
            )
        )
        with self.assertRaises(campaign_import.ValidationError):
            campaign_import.main(["--dry-run", "history-006", str(source)])
        state = deployment_dir / "state"
        before = state.exists()
        with contextlib.redirect_stdout(io.StringIO()):
            result = campaign_import.main(
                ["--dry-run", "--extract-omega-history", "history-006", str(source)]
            )
        self.assertEqual(result, 0)
        self.assertEqual(state.exists(), before)


if __name__ == "__main__":
    unittest.main()
