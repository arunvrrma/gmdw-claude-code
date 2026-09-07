#!/usr/bin/env python3
"""GMDW structured-Markdown contract tests.

The artifact format is the interface between an LLM writing prose and a
deterministic program acting on it, so these tests care about two things:

* **it parses what a model plausibly writes** — bold markers, inline code, a
  table introduced by a sentence, an em dash the terminal turned into `::`;
* **it fails legibly** — a rejection names one section or one row, because the
  orchestrator feeds that message straight back as the whole correction. A
  validation error that says "invalid" is a full regeneration; one that says
  "milestone M2 has no verification sub-bullet" is a two-line fix.

Stdlib only. Run: python3 tests/test_parsers.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from gmdw import parsers as P  # noqa: E402

WID = "wf_20260828_001"


def env(atype, version=1, produced_by="analyst-planner", extra=""):
    return (f"---\n"
            f"artifact_type: {atype}\n"
            f"workflow_id: {WID}\n"
            f"version: {version}\n"
            f"produced_by: {produced_by}\n"
            f"created_at: 2026-08-28T10:00:00Z\n"
            f"status: complete\n{extra}"
            f"---\n\n")


PLAN_BODY = """## Objective
Add a rate limiter to the public API.

## Files In Scope
- src/app.py — modify — add the limiter middleware
- src/limiter.py — create — the token bucket itself

## Milestones
- M1 — Add the bucket
  - files: src/limiter.py
  - verification: python3 -m pytest tests/test_limiter.py
- M2 — Wire it into the app
  - files: src/app.py
  - verification: python3 -m pytest tests/

## Acceptance Criteria
| id | criterion | verification method |
|---|---|---|
| AC1 | requests beyond the limit get a 429 | unit_test |
| AC2 | the limit is configurable | static_check |

## Risk Mitigations
| risk | likelihood | impact | mitigation |
|---|---|---|---|
| throttles legitimate bursts | medium | high | generous default, configurable |

## Scope Out
- distributed rate limiting across replicas

## Assumptions
- there is a single process behind the API
"""

REVIEW_BODY = """## Verdict
verdict: revise
rationale: the bucket refills on the wrong clock.

## Findings
| id | severity | category | file | description | recommendation |
|---|---|---|---|---|---|
| F1 | major | correctness | src/limiter.py:20 | refills on wall clock | use a monotonic clock |
| F2 | minor | maintainability | src/app.py:5 | unclear name | rename to `limiter` |

## Acceptance Criteria
| id | status | evidence |
|---|---|---|
| AC1 | pass | tests/test_limiter.py::test_429 |
| AC2 | not_verifiable | no config surface exists yet |

## Security Checklist
| area | evidence |
|---|---|
| input_handling | the limiter key is bounded at src/limiter.py:8 |

## Summary
Sound approach, one real bug in the refill path.
"""


# =========================================================================== #

class TestDocumentModel(unittest.TestCase):

    def test_front_matter_and_sections(self):
        doc = P.parse_document(env("plan") + PLAN_BODY)
        self.assertEqual(doc.envelope["artifact_type"], "plan")
        self.assertEqual(doc.envelope["workflow_id"], WID)
        self.assertTrue(doc.has("Objective"))
        self.assertTrue(doc.has("Files In Scope"))
        self.assertIn("rate limiter", doc.body("Objective"))

    def test_section_lookup_is_case_and_punctuation_insensitive(self):
        doc = P.parse_document(env("plan") + "## FILES  in-scope\n- a.py — create — x\n")
        self.assertTrue(doc.has("Files In Scope"))

    def test_fenced_content_is_not_parsed_as_structure(self):
        # An artifact that documents the format must not be re-parsed by its own
        # example — the ## and | inside a fence are content.
        doc = P.parse_document(env("plan") + """## Objective
Explain the format:

```
## Milestones
| id | severity |
```

Done.
""")
        self.assertFalse(doc.has("Milestones"))
        self.assertEqual(list(doc.sections), ["objective"])

    def test_missing_front_matter_is_reported_not_raised(self):
        errs = P.validate_artifact(PLAN_BODY, "plan", workflow_id=WID, version=1)
        self.assertEqual(len(errs), 1)
        self.assertIn("front matter", errs[0])

    def test_garbage_input_does_not_raise(self):
        for junk in ("", "   ", "---\n", "|||", "## \n", "\x00binary"):
            P.parse_document(junk)
            self.assertTrue(P.validate_artifact(junk, "plan", workflow_id=WID,
                                                version=1))


class TestExtractors(unittest.TestCase):

    def test_table_tolerates_a_leading_sentence_and_decorations(self):
        rows = P.table("Here is what I found:\n\n"
                       "| id | Severity | Verification Method |\n"
                       "|:---|---:|---|\n"
                       "| **F1** | `blocker` | unit_test |\n")
        self.assertEqual(rows, [{"id": "F1", "severity": "blocker",
                                 "verification_method": "unit_test"}])

    def test_nested_bullets_collect_sub_fields(self):
        items = P.nested_bullets("- M1 — do it\n  - files: a.py, b.py\n"
                                 "  - verification: make test\n- M2 — then this\n")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0][1]["files"], "a.py, b.py")
        self.assertEqual(items[1][0], "M2 — then this")

    def test_wrapped_bullets_keep_their_continuation_lines(self):
        # Found by the first v0.3 live run: an agent wrapped its `## Scope Out`
        # bullets across lines and everything after the first line vanished from
        # the plan briefing, so the human at the gate read a sentence cut off
        # mid-clause. A wrapped sentence is one sentence.
        items = P.bullets(
            "- No change to the move logic in `plan_moves()` (extension\n"
            "  grouping, `other/` routing) — dry-run only changes what\n"
            "  `main()` does with the already-correct plan.\n"
            "- No new CLI flags beyond `--dry-run` — not requested.\n")
        self.assertEqual(len(items), 2)
        self.assertTrue(items[0].endswith("already-correct plan."), items[0])
        self.assertIn("other/", items[0])
        self.assertEqual(items[1], "No new CLI flags beyond `--dry-run` — not requested.")

    def test_wrapped_sub_fields_keep_their_continuation_lines(self):
        items = P.nested_bullets(
            "- M1 — do the thing\n"
            "  - verification: python3 -m pytest tests/ &&\n"
            "    python3 -m py_compile src/app.py\n")
        self.assertEqual(items[0][1]["verification"],
                         "python3 -m pytest tests/ && python3 -m py_compile src/app.py")

    def test_unindented_prose_ends_the_list(self):
        items = P.bullets("- one\n- two\n\nA following paragraph.\n")
        self.assertEqual(items, ["one", "two"])

    def test_clean_only_unwraps_a_value_that_is_entirely_one_span(self):
        # Also from the live run: stripping the outer backtick unconditionally
        # mangled "wire it into `main()`" into "...`main()".
        self.assertEqual(P._clean("`modify`"), "modify")
        self.assertEqual(P._clean("**blocker**"), "blocker")
        self.assertEqual(P._clean("Add the `--dry-run` flag to `main()`"),
                         "Add the `--dry-run` flag to `main()`")
        self.assertEqual(P._clean("**a** and **b**"), "**a** and **b**")

    def test_ascii_separator_is_accepted(self):
        # A terminal or editor that eats em dashes must not cost a retry.
        doc = P.parse_document(env("plan") + "## Files In Scope\n"
                                             "- src/app.py :: modify :: why\n")
        files = P.parse_plan(doc)["files"]
        self.assertEqual(files, [{"path": "src/app.py", "action": "modify",
                                  "reason": "why"}])

    def test_files_in_scope_also_accepts_a_table(self):
        doc = P.parse_document(env("plan") + "## Files In Scope\n"
                                             "| path | action | reason |\n|---|---|---|\n"
                                             "| src/app.py | create | new |\n")
        self.assertEqual(P.parse_plan(doc)["files"],
                         [{"path": "src/app.py", "action": "create", "reason": "new"}])

    def test_fenced_json_is_extracted_by_language(self):
        body = "Some prose.\n\n```json\n[\"a\", \"b\"]\n```\n\n```\nnot json\n```\n"
        self.assertEqual(P.fenced_blocks(body, "json"), ['["a", "b"]'])
        self.assertEqual(len(P.fenced_blocks(body)), 2)


class TestEnvelope(unittest.TestCase):

    def test_mismatched_identity_is_rejected(self):
        errs = P.validate_artifact(env("plan", version=2) + PLAN_BODY, "plan",
                                   workflow_id=WID, version=1)
        self.assertTrue(any("version" in e for e in errs))
        errs = P.validate_artifact(env("review") + PLAN_BODY, "plan",
                                   workflow_id=WID, version=1)
        self.assertTrue(any("artifact_type" in e for e in errs))
        errs = P.validate_artifact(env("plan") + PLAN_BODY, "plan",
                                   workflow_id="wf_20260101_009", version=1)
        self.assertTrue(any("workflow_id" in e for e in errs))

    def test_blocked_artifact_is_envelope_checked_only(self):
        # "I could not do this, here is why" is a complete answer. Rejecting it
        # for missing the sections it is explaining away would strand the run.
        text = env("plan", extra="blocked_reason: the request needs a database "
                                 "that does not exist\n").replace(
            "status: complete", "status: blocked") + "## Objective\n"
        self.assertEqual(P.validate_artifact(text, "plan", workflow_id=WID,
                                             version=1), [])
        doc = P.parse_document(text)
        self.assertTrue(P.is_blocked(doc.envelope))

    def test_blocked_without_a_reason_is_rejected(self):
        text = env("plan").replace("status: complete", "status: blocked") + \
            "## Objective\nx\n"
        errs = P.validate_artifact(text, "plan", workflow_id=WID, version=1)
        self.assertTrue(any("blocked_reason" in e for e in errs))


class TestPlan(unittest.TestCase):

    def parse(self, body=PLAN_BODY):
        return P.parse_plan(P.parse_document(env("plan") + body))

    def test_valid_plan_parses_completely(self):
        self.assertEqual(P.validate_artifact(env("plan") + PLAN_BODY, "plan",
                                             workflow_id=WID, version=1), [])
        plan = self.parse()
        self.assertEqual([f["path"] for f in plan["files"]],
                         ["src/app.py", "src/limiter.py"])
        self.assertEqual([f["action"] for f in plan["files"]],
                         ["modify", "create"])
        self.assertEqual([m["id"] for m in plan["milestones"]], ["M1", "M2"])
        self.assertEqual(plan["milestones"][0]["files"], ["src/limiter.py"])
        self.assertEqual([a["id"] for a in plan["acceptance_criteria"]],
                         ["AC1", "AC2"])
        self.assertEqual(plan["risks"][0]["impact"], "high")
        self.assertEqual(plan["scope_out"],
                         ["distributed rate limiting across replicas"])

    def test_missing_section_names_the_heading_to_add(self):
        body = PLAN_BODY.replace("## Risk Mitigations", "## Risks")
        errs = P.validate_artifact(env("plan") + body, "plan",
                                   workflow_id=WID, version=1)
        self.assertEqual(errs, ["missing required section '## Risk Mitigations'"])

    def test_bad_file_action_is_named_precisely(self):
        body = PLAN_BODY.replace("— modify —", "— refactor —")
        errs = P.validate_artifact(env("plan") + body, "plan",
                                   workflow_id=WID, version=1)
        self.assertEqual(len(errs), 1)
        self.assertIn("src/app.py", errs[0])
        self.assertIn("'refactor'", errs[0])

    def test_milestone_without_verification_is_rejected(self):
        body = PLAN_BODY.replace("  - verification: python3 -m pytest tests/\n", "")
        errs = P.validate_artifact(env("plan") + body, "plan",
                                   workflow_id=WID, version=1)
        self.assertTrue(any("M2" in e and "verification" in e for e in errs))

    def test_bad_acceptance_method_is_rejected(self):
        body = PLAN_BODY.replace("| unit_test |", "| vibes |")
        errs = P.validate_artifact(env("plan") + body, "plan",
                                   workflow_id=WID, version=1)
        self.assertTrue(any("AC1" in e and "vibes" in e for e in errs))

    def test_empty_files_in_scope_is_rejected(self):
        # This list IS the Implementor's write allowlist, so an empty one is not
        # a permissive plan, it is an unimplementable one.
        body = PLAN_BODY.replace("- src/app.py — modify — add the limiter middleware\n"
                                 "- src/limiter.py — create — the token bucket itself\n",
                                 "TBD\n")
        errs = P.validate_artifact(env("plan") + body, "plan",
                                   workflow_id=WID, version=1)
        self.assertTrue(any("write scope" in e for e in errs))


class TestReview(unittest.TestCase):

    def parse(self, body=REVIEW_BODY):
        return P.parse_review(P.parse_document(env("review", produced_by="reviewer")
                                               + body))

    def test_valid_review_parses(self):
        self.assertEqual(
            P.validate_artifact(env("review", produced_by="reviewer") + REVIEW_BODY,
                                "review", workflow_id=WID, version=1), [])
        r = self.parse()
        self.assertEqual(r["verdict"], "revise")
        self.assertEqual([f["id"] for f in r["findings"]], ["F1", "F2"])
        self.assertEqual(r["findings"][0]["location"], "src/limiter.py:20")
        self.assertEqual(r["acceptance_criteria"][1]["status"], "not_verifiable")
        self.assertEqual(r["checked"][0]["area"], "input_handling")

    def test_bad_verdict_is_rejected(self):
        body = REVIEW_BODY.replace("verdict: revise", "verdict: looks fine to me")
        errs = P.validate_artifact(env("review", produced_by="reviewer") + body,
                                   "review", workflow_id=WID, version=1)
        self.assertTrue(any("Verdict" in e for e in errs))

    def test_duplicate_finding_ids_are_rejected(self):
        body = REVIEW_BODY.replace("| F2 |", "| F1 |")
        errs = P.validate_artifact(env("review", produced_by="reviewer") + body,
                                   "review", workflow_id=WID, version=1)
        self.assertTrue(any("duplicate finding id" in e for e in errs))

    def test_not_verifiable_requires_evidence(self):
        body = REVIEW_BODY.replace("| AC2 | not_verifiable | no config surface exists yet |",
                                   "| AC2 | not_verifiable |  |")
        errs = P.validate_artifact(env("review", produced_by="reviewer") + body,
                                   "review", workflow_id=WID, version=1)
        self.assertTrue(any("not_verifiable" in e for e in errs))

    def test_security_checklist_required_only_when_flagged(self):
        body = REVIEW_BODY.split("## Security Checklist")[0] + \
            "## Summary\nfine.\n"
        text = env("review", produced_by="reviewer") + body
        self.assertEqual(P.validate_artifact(text, "review", workflow_id=WID,
                                             version=1), [])
        errs = P.validate_artifact(text, "review", workflow_id=WID, version=1,
                                   require_security_checklist=True)
        self.assertTrue(any("Security Checklist" in e for e in errs))
        self.assertTrue(any("not an approval" in e for e in errs))


class TestClassification(unittest.TestCase):

    BODY = """## Complexity
| dimension | score |
|---|---|
| files_impacted | 2 |
| architectural | 1 |
| security | 3 |
| data_model | 0 |
| integrations | 1 |
| testing | 2 |

## Risk Level
route: full
flags: security
total: 9

## Governance Profile
Security score of 3 forces the full route.

## Evidence
- src/auth.py — token verification lives here
"""

    def test_parses_scores_route_and_flags(self):
        self.assertEqual(P.validate_artifact(env("classification") + self.BODY,
                                             "classification", workflow_id=WID,
                                             version=1), [])
        c = P.parse_classification(P.parse_document(env("classification") + self.BODY))
        self.assertEqual(c["scores"]["security"], 3)
        self.assertEqual(c["route"], "full")
        self.assertEqual(c["override_flags"], ["security"])
        self.assertEqual(c["total"], 9)

    def test_flags_none_means_no_flags(self):
        c = P.parse_classification(P.parse_document(
            env("classification") + self.BODY.replace("flags: security", "flags: none")))
        self.assertEqual(c["override_flags"], [])

    def test_missing_dimension_is_named(self):
        body = self.BODY.replace("| testing | 2 |\n", "")
        errs = P.validate_artifact(env("classification") + body, "classification",
                                   workflow_id=WID, version=1)
        self.assertTrue(any("testing" in e for e in errs))

    def test_out_of_range_score_is_rejected(self):
        body = self.BODY.replace("| security | 3 |", "| security | 7 |")
        errs = P.validate_artifact(env("classification") + body, "classification",
                                   workflow_id=WID, version=1)
        self.assertTrue(any("must be 0-3" in e for e in errs))


class TestManifest(unittest.TestCase):

    BODY = """## Base Commit
0123456789abcdef0123456789abcdef01234567

## Changes
| path | action | summary |
|---|---|---|
| src/app.py | modified | wired the limiter in |

## Tests
- command: python3 -m pytest tests/
- result: pass
- added: tests/test_limiter.py

## Milestone Status
| milestone id | status | note |
|---|---|---|
| M1 | done | |

## Deviations
- none

## Findings Addressed
| finding id | resolution |
|---|---|
| F1 | switched to time.monotonic at src/limiter.py:20 |
"""

    def test_parses(self):
        text = env("implementation_manifest", produced_by="implementor") + self.BODY
        self.assertEqual(P.validate_artifact(text, "implementation",
                                             workflow_id=WID, version=1), [])
        m = P.parse_manifest(P.parse_document(text))
        self.assertEqual(m["tests"]["result"], "pass")
        self.assertEqual(m["tests"]["command"], "python3 -m pytest tests/")
        self.assertEqual(m["changes"][0]["action"], "modified")
        self.assertEqual(m["deviations"], [])
        self.assertEqual(m["findings_addressed"][0]["finding_id"], "F1")

    def test_declared_result_without_a_command_is_rejected(self):
        # The command is what gets re-run to check the claim. A result with no
        # command is an unverifiable assertion, which is the one thing the
        # honesty check exists to prevent.
        body = self.BODY.replace("- command: python3 -m pytest tests/\n", "")
        errs = P.validate_artifact(
            env("implementation_manifest", produced_by="implementor") + body,
            "implementation", workflow_id=WID, version=1)
        self.assertTrue(any("re-run" in e for e in errs))

    def test_bad_base_commit_is_rejected(self):
        body = self.BODY.replace("0123456789abcdef0123456789abcdef01234567", "HEAD")
        errs = P.validate_artifact(
            env("implementation_manifest", produced_by="implementor") + body,
            "implementation", workflow_id=WID, version=1)
        self.assertTrue(any("Base Commit" in e for e in errs))

    def test_deviations_table_parses_and_requires_why(self):
        body = self.BODY.replace("## Deviations\n- none\n",
                                 "## Deviations\n| what | why | requires approval |\n"
                                 "|---|---|---|\n| src/extra.py |  | yes |\n")
        errs = P.validate_artifact(
            env("implementation_manifest", produced_by="implementor") + body,
            "implementation", workflow_id=WID, version=1)
        self.assertTrue(any("'why' is required" in e for e in errs))


class TestRepoAnalysis(unittest.TestCase):

    BODY = """## Summary
A small Python service.

## File Tree
- src/app.py — the entry point
- src/limiter.py — does not exist yet

## Dependencies
- stdlib only

## Architecture Notes
- src/app.py — one module, no framework
"""

    def test_parses_paths_for_the_existence_check(self):
        text = env("repo_analysis") + self.BODY
        self.assertEqual(P.validate_artifact(text, "repo_analysis",
                                             workflow_id=WID, version=1), [])
        ra = P.parse_repo_analysis(P.parse_document(text))
        self.assertEqual(ra["paths"], ["src/app.py", "src/limiter.py"])

    def test_empty_file_tree_is_rejected(self):
        body = self.BODY.replace("- src/app.py — the entry point\n"
                                 "- src/limiter.py — does not exist yet\n", "\n")
        errs = P.validate_artifact(env("repo_analysis") + body, "repo_analysis",
                                   workflow_id=WID, version=1)
        self.assertTrue(any("File Tree" in e for e in errs))


if __name__ == "__main__":
    unittest.main(verbosity=2)
