import importlib.util
import json
import re
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "yf-skill-check/scripts/audit.py"
spec = importlib.util.spec_from_file_location("audit", SCRIPT)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class AuditWorkflow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.skills = self.root / "skills"
        self.skills.mkdir()

    def skill(self, name, description="Debug bugs and fix regressions.", body="Use a regression test before changing code.", folder=None):
        p = self.skills / (folder or name) / "SKILL.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\nname: {name}\ndescription: {json.dumps(description)}\n---\n{body}\n")
        return p

    def run_scan(self, *extra, out="report"):
        result = subprocess.run([sys.executable, str(SCRIPT), "scan", "--platform", "codex", "--only-roots", "--root", str(self.skills), "--out", str(self.root / out), *extra], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads((self.root / out / "scan.json").read_text())

    def review(self, data):
        pair = data["candidates"][0]
        return {"scan_id": data["scan_id"], "reviewer": "fixture evaluation", "reviews": [{"candidate_id": pair["id"], "verdict": "confirmed", "trigger": "Fix this regression", "reason": "Conflicting requirements", "recommendation": "Choose one testing policy", "evidence": [{"skill_id": sid, "quote": "Use a regression test before changing code."} for sid in pair["skill_ids"]]}]}

    def test_same_target_aliases_are_one_skill_without_cycle(self):
        p = self.skill("debugger")
        (self.skills / "debug-alias").symlink_to(p.parent, target_is_directory=True)
        (self.skills / "cycle").symlink_to(self.skills, target_is_directory=True)
        data = self.run_scan()
        self.assertEqual(len(data["skills"]), 1)
        self.assertEqual(len(data["skills"][0]["aliases"]), 2)
        self.assertEqual(data["candidates"], [])

    def test_copies_are_reported_but_not_collapsed(self):
        p = self.skill("debugger")
        self.skill("debugger", folder="copy").write_text(p.read_text())
        data = self.run_scan()
        self.assertEqual(len(data["skills"]), 2)
        self.assertEqual(sum(f["kind"] == "duplicate_copy" for f in data["findings"]), 1)
        self.assertEqual(data["candidates"][0]["verdict"], "pending")

    def test_confirmed_conflicts_require_real_evidence_and_fresh_files(self):
        self.skill("debug-first")
        p = self.skill("debug-second")
        data = self.run_scan()
        reviews = self.review(data)
        self.assertEqual(audit.apply_reviews(data, reviews)["candidates"][0]["verdict"], "confirmed")
        bad = json.loads(json.dumps(reviews))
        bad["reviews"][0]["evidence"][0]["quote"] = "Invented evidence"
        with self.assertRaisesRegex(ValueError, "原文不存在"):
            audit.apply_reviews(data, bad)
        p.write_text(p.read_text() + "Changed policy\n")
        with self.assertRaisesRegex(ValueError, "发生变化"):
            audit.apply_reviews(data, reviews)

    def test_catalog_tracks_visibility_and_skips_implicit_disabled_skill(self):
        p = self.skill("visible-debug")
        q = self.skill("manual-debug")
        (q.parent / "agents").mkdir()
        (q.parent / "agents/openai.yaml").write_text("policy:\n  allow_implicit_invocation: false\n")
        self.skill("unverified-debug")
        catalog = self.root / "catalog.json"
        catalog.write_text(json.dumps({"skills": [{"path": str(p), "name": "visible-debug", "description": "Fix bugs."}]}))
        data = self.run_scan("--catalog", str(catalog))
        self.assertEqual(sum(s["visibility"] == "observed" for s in data["skills"]), 1)
        manual = next(s for s in data["skills"] if s["name"] == "manual-debug")
        self.assertFalse(manual["eligible"])
        self.assertTrue(all(manual["id"] not in c["skill_ids"] for c in data["candidates"]))

    def test_html_does_not_execute_skill_text(self):
        self.skill("unsafe-text", description="</script><script>window.INJECTED=true</script>")
        data = self.run_scan()
        html = (self.root / "report/report.html").read_text()
        self.assertNotIn("</script><script>window.INJECTED", html)
        self.assertIn("\\u003c/script>", html)
        self.assertEqual(data["skills"][0]["description"], "</script><script>window.INJECTED=true</script>")

    def test_only_roots_never_scans_unrelated_platform_directory(self):
        self.skill("debugger")
        other = self.root / ".claude/skills/private-skill"
        other.mkdir(parents=True)
        (other / "SKILL.md").write_text("---\nname: private-skill\ndescription: Debug bugs.\n---\nPrivate\n")
        data = self.run_scan("--cwd", str(self.root))
        self.assertEqual([s["name"] for s in data["skills"]], ["debugger"])

    def test_missing_links_are_detected_but_fenced_examples_are_not(self):
        self.skill("links", body="[missing](references/missing.md)\n```md\n[example](references/example.md)\n```\n")
        data = self.run_scan()
        missing = [f for f in data["findings"] if f["kind"] == "broken_reference"]
        self.assertEqual(len(missing), 1)
        self.assertIn("missing.md", missing[0]["reason"])

    def test_folded_frontmatter_and_legacy_commands(self):
        meta, body, _ = audit.frontmatter("---\nname: bilingual\ndescription: >-\n  生成封面。\n  Create covers.\n---\nUse references.\n")
        self.assertIn("Create covers.", meta["description"])
        meta, body, _ = audit.frontmatter("---\ndescription: Fix bugs.\n---\nDo the work", legacy_name="fix")
        self.assertEqual(meta["name"], "fix")

    def test_unknown_host_does_not_guess_roots(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "scan", "--platform", "other", "--out", str(self.root / "none")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("必须提供", r.stderr)

    def test_claude_user_source_overrides_project_same_name(self):
        project, user = self.root / "project", self.root / "user"
        project.mkdir()
        (project / ".git").touch()
        for base, body in [(project, "Project policy"), (user, "User policy")]:
            p = base / ".claude/skills/shared/SKILL.md"
            p.parent.mkdir(parents=True)
            p.write_text(f"---\nname: shared\ndescription: Debug bugs.\n---\n{body}\n")
        with patch.object(audit.Path, "home", return_value=user):
            code = audit.main(["scan", "--platform", "claude-code", "--cwd", str(project), "--out", str(self.root / "claude-report")])
        self.assertEqual(code, 0)
        data = json.loads((self.root / "claude-report/scan.json").read_text())
        loser = next(s for s in data["skills"] if "project" in s["sources"])
        winner = next(s for s in data["skills"] if "user" in s["sources"])
        self.assertEqual(loser["shadowed_by"], winner["id"])
        self.assertFalse(loser["eligible"])
        self.assertEqual(data["candidates"], [])

    def test_invalid_skill_is_a_coverage_issue_and_report_survives(self):
        self.skill("valid")
        p = self.skill("invalid")
        p.write_text("---\nname: invalid\ndescription: [not, a, string]\n---\nData\n")
        data = self.run_scan()
        self.assertEqual(len(data["skills"]), 1)
        self.assertTrue(any(f["kind"] == "unreadable" for f in data["findings"]))
        self.assertTrue((self.root / "report/report.html").is_file())

    def test_length_and_broad_scope_do_not_create_priority_advice(self):
        self.skill("complete-workflow", description="An assistant for any task.", body="Plan the next decision carefully.\n" * 390)
        data = self.run_scan()
        report = audit.prepare_report(data)
        self.assertEqual({f["kind"] for f in report["findings"]}, {"long_body", "broad_trigger"})
        self.assertTrue(all(f["level"] == "info" for f in report["findings"]))
        self.assertEqual(report["presentation"]["actions"], [])
        self.assertEqual(report["presentation"]["watch"], [])

    def test_incomplete_empty_and_risky_reports_never_claim_all_clear(self):
        self.skill("debug-one")
        self.skill("debug-two")
        data = self.run_scan()
        self.assertTrue(audit.prepare_report(data)["presentation"]["incomplete"])
        data["candidates"][0]["verdict"] = "risk"
        data["reviewer"] = "model"
        report = audit.prepare_report(data)
        self.assertEqual(len(report["presentation"]["watch"]), 1)
        self.assertEqual(report["presentation"]["actions"], [])
        data["omitted_candidates"] = 1
        self.assertTrue(audit.prepare_report(data)["presentation"]["incomplete"])
        data["omitted_candidates"] = 0
        data["findings"] = [audit.issue("unreadable", "warning", "Unreachable", "Permission", "Check path")]
        self.assertTrue(audit.prepare_report(data)["presentation"]["incomplete"])
        data["findings"], data["skills"], data["candidates"] = [], [], []
        self.assertIn("没有找到", audit.prepare_report(data)["presentation"]["conclusion"])

    def test_file_advice_requires_impact_real_evidence_and_fresh_source(self):
        p = self.skill("required-link", body="Read [required steps](references/missing.md) before executing this workflow.")
        data = self.run_scan()
        finding = next(f for f in data["findings"] if f["kind"] == "broken_reference")
        self.assertEqual(audit.prepare_report(data)["findings"][0]["level"], "watch")
        review = {"scan_id": data["scan_id"], "reviewer": "fixture", "reviews": [], "finding_reviews": [{
            "finding_id": finding["id"], "level": "action", "trigger": "Execute this workflow", "impact": "Required steps unavailable",
            "reason": "The instructions require the missing file", "recommendation": "Restore the required instructions",
            "evidence": [{"skill_id": data["skills"][0]["id"], "quote": "before executing this workflow."}]}]}
        checked = audit.prepare_report(audit.apply_reviews(data, review))
        self.assertEqual(len(checked["presentation"]["actions"]), 1)
        bad = json.loads(json.dumps(review))
        bad["finding_reviews"][0].pop("impact")
        with self.assertRaisesRegex(ValueError, "具体适用情景"):
            audit.apply_reviews(data, bad)
        bad = json.loads(json.dumps(review))
        bad["finding_reviews"][0]["evidence"][0]["quote"] = "Invented dependency"
        with self.assertRaisesRegex(ValueError, "原文不存在"):
            audit.apply_reviews(data, bad)
        p.write_text(p.read_text() + "\nChanged\n")
        with self.assertRaisesRegex(ValueError, "发生变化"):
            audit.apply_reviews(data, review)

    def test_priority_and_observation_counts_are_separate(self):
        self.skill("debug-one")
        self.skill("debug-two")
        data = self.run_scan()
        report = audit.prepare_report(audit.apply_reviews(data, self.review(data)))
        self.assertEqual(len(report["presentation"]["actions"]), 1)
        self.assertEqual(report["presentation"]["watch"], [])

    @unittest.skipUnless(shutil.which("node"), "需要 Node 运行页面启动回归检查")
    def test_opening_template_shows_missing_data_message_without_crashing(self):
        html = (SCRIPT.parent.parent / "assets/report.html").read_text()
        script = re.search(r"<script>\s*(.*?)</script>", html, re.S)[1]
        data = re.search(r'id="audit-data">(.*?)</script>', html, re.S)[1]
        harness = r"""
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const nodes = {'audit-data':{textContent:input.data}, 'data-notice':{hidden:true,textContent:''}, 'lead':{textContent:''}, 'report-meta':{textContent:''}};
const document = {getElementById:id=>nodes[id],querySelectorAll:()=>[]};
try { vm.runInNewContext(input.script,{document,console}); }
catch(error) { console.error(error.message); process.exit(1); }
if(nodes['data-notice'].hidden || !nodes['data-notice'].textContent.includes('模板')) process.exit(2);
"""
        result = subprocess.run([shutil.which("node"), "-e", harness], input=json.dumps({"script": script, "data": data}), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr or "模板应显示缺少数据提示")


if __name__ == "__main__":
    unittest.main()
