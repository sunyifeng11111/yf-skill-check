#!/usr/bin/env python3
"""Read-only skill discovery, candidate screening, and offline report rendering."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import re
import sys
from urllib.parse import unquote

VERSION = "1.1.1"
PLATFORMS = ("codex", "claude-code", "cursor", "other")
VERDICTS = {"confirmed", "risk", "complementary", "exclusive", "not_conflict", "needs_context"}
MAX_FILE_BYTES = 2_000_000
CONCEPTS = {
    "bug": r"\bbugs?\b|debug|diagnos|regression|故障|修复|排查|调试|修bug",
    "image": r"image.?gen|generate.{0,18}image|生成.{0,8}图|生图|illustrat|插画|位图|bitmap",
    "cover": r"\bcover\b|thumbnail|封面",
    "web": r"frontend|front.end|landing page|website|网页|前端|网站",
    "mobile": r"\bmobile\b|\bios\b|android|移动端|手机",
    "design": r"\bdesign\b|\bui\b|\bux\b|interface|设计|界面|视觉",
    "video": r"\bvideo\b|视频|影片",
    "voice": r"voice|narrat|\btts\b|配音|口播|旁白",
    "music": r"\bmusic\b|\bbgm\b|soundtrack|音乐|配乐",
    "docs": r"\bdocx\b|word document|\bdocument[s]?\b|文档",
    "slides": r"\bpptx?\b|slides?|presentation|演示|幻灯片",
    "sheet": r"spreadsheet|\bxlsx\b|\bcsv\b|excel|表格",
    "pdf": r"\bpdf\b",
    "skill": r"\bskills?\b|技能",
    "review": r"review|审查|评审|审核",
    "writing": r"\bwriting\b|\bwrite\b|写作|文章",
    "pet": r"\bpets?\b|宠物|吉祥物",
    "research": r"research|investigat|研究|调研",
}


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def dump(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class TokenCounter:
    def __init__(self, encoding=None):
        self.encoder = None
        if encoding:
            try:
                import tiktoken
                self.encoder = tiktoken.get_encoding(encoding)
            except (ImportError, ValueError, OSError) as exc:
                raise ValueError(f"无法使用 tokenizer {encoding}：{exc}；省略 --encoding 可使用字符估算。") from exc
        self.method = f"tiktoken/{encoding}" if self.encoder else "字符启发式：汉字约 1.5 Token，其余字符约 4 字符/Token"

    def count(self, value: str) -> dict:
        if self.encoder:
            number = len(self.encoder.encode(value, disallowed_special=()))
            return {"estimate": number, "low": number, "high": number}
        cjk = sum(1 for c in value if "\u3400" <= c <= "\u9fff")
        number = math.ceil(cjk * 1.5 + (len(value) - cjk) / 4)
        return {"estimate": number, "low": math.floor(number * .7), "high": math.ceil(number * 1.5)}


def scalar(raw: str):
    value = raw.strip()
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    if value.startswith('"'):
        return json.loads(value)
    if value.startswith("'"):
        if not value.endswith("'") or len(value) < 2:
            raise ValueError("未闭合的 YAML 字符串")
        return value[1:-1].replace("''", "'")
    if value.startswith(("[", "{")) or value in ("null", "~"):
        raise ValueError("字段必须是字符串")
    return re.split(r"\s+#", value, maxsplit=1)[0].rstrip()


def frontmatter(text: str, legacy_name=None) -> tuple[dict, str, str]:
    normalized = text.lstrip("\ufeff").replace("\r\n", "\n")
    match = re.match(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|$)", normalized, re.S)
    if not match:
        if legacy_name:
            return {"name": legacy_name, "description": "", "disable-model-invocation": True}, normalized, "legacy-command"
        raise ValueError("缺少或未闭合的 YAML frontmatter")
    yaml_text = match[1]
    try:
        import yaml
    except ImportError:
        yaml = None
    if yaml:
        meta = yaml.safe_load(yaml_text)
        if not isinstance(meta, dict):
            raise ValueError("frontmatter 必须是映射")
        parser = "PyYAML"
    else:
        # Extract only supported top-level scalars; do not claim full YAML validation.
        lines, meta = yaml_text.splitlines(), {}
        index = 0
        relevant = {"name", "description", "disable-model-invocation"}
        while index < len(lines):
            keymatch = re.match(r"^([\w-]+):\s*(.*)$", lines[index])
            index += 1
            if not keymatch or keymatch[1] not in relevant:
                continue
            key, value = keymatch.groups()
            if key in meta:
                raise ValueError(f"重复字段 {key}")
            if value in (">", "|-", "|", ">-", "|+", ">+"):
                block = []
                while index < len(lines) and (not lines[index].strip() or lines[index][0].isspace()):
                    block.append(lines[index].strip())
                    index += 1
                meta[key] = ("\n" if value.startswith("|") else " ").join(block).strip()
            else:
                if value[:1] in ('"', "'"):
                    quote = value[0]
                    while not value.rstrip().endswith(quote) and index < len(lines) and lines[index][:1].isspace():
                        value += " " + lines[index].strip()
                        index += 1
                meta[key] = scalar(value)
        parser = "标准库提取相关标量（非完整 YAML 校验）"
    if legacy_name:
        meta.setdefault("name", legacy_name)
        meta.setdefault("description", "")
    for key in ("name", "description"):
        if not isinstance(meta.get(key), str) or not meta[key].strip():
            if key == "description" and legacy_name:
                meta[key] = ""
                continue
            raise ValueError(f"{key} 缺失、为空或不是字符串")
    return meta, normalized[match.end():], parser


def workspace_levels(cwd: Path) -> list[Path]:
    for parent in (cwd, *cwd.parents):
        if (parent / ".git").exists():
            return list((cwd, *cwd.parents))[:list((cwd, *cwd.parents)).index(parent) + 1]
    return [cwd]


def roots_for(platform: str, cwd: Path, home: Path) -> list[dict]:
    roots = []
    project_names = {"codex": [".agents/skills"], "claude-code": [".claude/skills", ".claude/commands"],
                     "cursor": [".cursor/skills", ".agents/skills", ".claude/skills", ".codex/skills"], "other": []}
    user_names = {"codex": [".agents/skills", ".codex/skills"], "claude-code": [".claude/skills", ".claude/commands"],
                  "cursor": [".cursor/skills", ".agents/skills", ".claude/skills", ".codex/skills"], "other": []}
    for depth, level in enumerate(workspace_levels(cwd)):
        for name in project_names[platform]:
            roots.append({"path": str(level / name), "source": "project", "priority": 200 - depth, "legacy": name.endswith("commands")})
    for name in user_names[platform]:
        roots.append({"path": str(home / name), "source": "user", "priority": 300, "legacy": name.endswith("commands")})
    if platform == "codex":
        roots.append({"path": "/etc/codex/skills", "source": "admin", "priority": 400, "legacy": False})
    return roots


def walk_skills(root: Path, legacy=False) -> tuple[list[Path], list[dict]]:
    found, errors, visited = [], [], set()
    def visit(path, depth=0):
        try:
            actual = path.resolve(strict=True)
            if path.is_file():
                if path.name == "SKILL.md" or (legacy and path.suffix == ".md"):
                    found.append(path)
                return
            if (path / "SKILL.md").is_file():
                found.append(path / "SKILL.md")
                return
            if actual in visited:
                return
            visited.add(actual)
            if depth >= 12:
                errors.append({"path": str(path), "reason": "目录深度超过 12，未继续扫描"})
                return
            for child in sorted(path.iterdir()):
                if child.name in {".git", "node_modules", "__pycache__", ".DS_Store"}:
                    continue
                if child.is_symlink() and not child.exists():
                    errors.append({"path": str(child), "reason": "符号链接目标不存在"})
                elif child.is_dir() or child.name == "SKILL.md" or (legacy and child.suffix == ".md"):
                    visit(child, depth + 1)
        except (OSError, RuntimeError) as exc:
            errors.append({"path": str(path), "reason": str(exc)})
    if root.exists():
        visit(root)
    elif root.is_symlink():
        errors.append({"path": str(root), "reason": "根目录的符号链接目标不存在"})
    return found, errors


def issue(kind, severity, title, reason, recommendation, skill_ids=None, paths=None):
    return {"id": "i-" + digest(kind + reason + str(skill_ids) + str(paths))[:12], "kind": kind,
            "severity": severity, "title": title, "reason": reason, "recommendation": recommendation,
            "skill_ids": skill_ids or [], "paths": paths or []}


def implicit_policy(path: Path, meta: dict, platform: str) -> tuple[bool, str]:
    if platform == "claude-code" and meta.get("disable-model-invocation") is True:
        return False, "frontmatter 声明显式调用"
    if platform == "codex":
        config = path.parent / "agents" / "openai.yaml"
        if config.is_file():
            policy = re.search(r"(?m)^policy:\s*\n((?:[ \t]+[^\n]*\n?|\n)*)", config.read_text(encoding="utf-8"))
            if policy and re.search(r"allow_implicit_invocation:\s*false\b", policy[1]):
                return False, "openai.yaml 禁用隐式调用"
    if meta.get("disable-model-invocation") is True:
        return True, "声明 disable-model-invocation；当前平台语义需复核"
    return True, "未发现适用于当前平台的隐式调用限制"


def local_links(body: str, path: Path) -> list[dict]:
    result = []
    body = re.sub(r"(?ms)^\s*(`{3,}|~{3,}).*?^\s*\1\s*$", "", body)
    body = re.sub(r"`[^`\n]+`", "", body)
    for value in re.findall(r"\]\(([^\n)]+)\)", body):
        value = value.split(' "', 1)[0].strip().strip("<>")
        if not value or re.match(r"^[\w+.-]+:", value) or value.startswith("#"):
            continue
        target = unquote(value.split("#", 1)[0])
        if not target or any(x in target for x in ("$", "*", "<", ">", "{", "}")):
            continue
        result.append({"reference": value, "exists": (path.parent / target).exists()})
    return result


def text_features(text: str) -> set[str]:
    text = text.lower()
    stop = {"skill", "skills", "when", "with", "from", "this", "that", "have", "into", "user", "uses", "requests", "should", "only", "using", "create"}
    words = {w for w in re.findall(r"[a-z][a-z0-9-]{2,}", text) if w not in stop}
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        words.update(run[i:i + 2] for i in range(len(run) - 1))
    return words


def pair_id(left, right):
    return "c-" + digest("|".join(sorted([left, right])))[:12]


def candidates_for(skills: list[dict]) -> list[dict]:
    eligible = [s for s in skills if s["eligible"] and s["description"]]
    frequency = Counter(w for s in eligible for w in text_features(s["description"]))
    features = {s["id"]: text_features(s["description"]) for s in eligible}
    candidates = []
    for left, right in itertools.combinations(eligible, 2):
        a, b = features[left["id"]], features[right["id"]]
        weight = lambda words: sum(math.log((len(eligible) + 1) / (frequency[w] + 1)) + 1 for w in words)
        lexical = weight(a & b) / (weight(a | b) or 1)
        common = sorted(set(left["concepts"]) & set(right["concepts"]))
        same_name = left["name"] == right["name"]
        version_pair = re.sub(r"-v\d+$", "", left["name"]) == re.sub(r"-v\d+$", "", right["name"])
        score = min(1, lexical + min(.32, .12 * len(common)) + (.45 if same_name else .3 if version_pair else 0))
        specific = bool(set(common) & {"bug", "cover", "pdf", "sheet", "slides", "music", "pet"})
        if score < .22 or (lexical < .06 and len(common) < 2 and not same_name and not version_pair and not specific):
            continue
        candidates.append({"id": pair_id(left["id"], right["id"]), "skill_ids": [left["id"], right["id"]],
                           "score": round(score, 3), "signals": common, "origin": "static", "verdict": "pending"})
    return sorted(candidates, key=lambda c: (-c["score"], c["id"]))


def scan(args) -> dict:
    cwd = Path(args.cwd).expanduser().resolve()
    if not cwd.is_dir():
        raise ValueError("工作目录不存在")
    if args.platform == "other" and not args.root and not args.catalog:
        raise ValueError("未知平台必须提供 --root 或 --catalog；不猜测宿主目录")
    out = Path(args.out).expanduser().resolve()
    if out.exists() and any(out.iterdir()):
        raise ValueError("输出目录非空，请使用新的目录以保留既有报告")
    counter = TokenCounter(args.encoding)
    roots = [] if args.only_roots else roots_for(args.platform, cwd, Path.home())
    roots += [{"path": str(Path(p).expanduser().absolute()), "source": "provided", "priority": 0, "legacy": False} for p in args.root]
    roots = list({r["path"]: r for r in roots}.values())
    catalog = []
    if args.catalog:
        catalog = json.loads(Path(args.catalog).read_text(encoding="utf-8"))["skills"]
        if not isinstance(catalog, list):
            raise ValueError("catalog.skills 必须是数组")
    observed = {}
    entries, findings = [], []
    for item in catalog:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not Path(item["path"]).is_absolute():
            raise ValueError("catalog 条目必须含绝对 path")
        path = Path(item["path"]).expanduser()
        if path.is_dir():
            path /= "SKILL.md"
        observed[str(path.resolve())] = item
        entries.append((path, {"source": "session", "priority": 1000, "legacy": False}))
    for root in roots:
        rootpath = Path(root["path"])
        root["exists"] = rootpath.exists()
        files, errors = walk_skills(rootpath, root["legacy"])
        entries.extend((p, root) for p in files)
        findings.extend(issue("unreadable", "warning", "扫描范围不完整", e["reason"], "修复路径或权限后重新体检。", paths=[e["path"]]) for e in errors)
    by_real, bodies, total_entries = {}, {}, len(entries)
    for path, source in entries:
        real = str(path.resolve())
        if real in by_real:
            record = by_real[real]
            if str(path) not in record["aliases"]:
                record["aliases"].append(str(path))
            if source["source"] not in record["sources"]:
                record["sources"].append(source["source"])
            record["priority"] = max(record["priority"], source["priority"])
            continue
        sid = "s-" + digest(real)[:12]
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("文件超过 2 MB，未读取")
            raw = path.read_text(encoding="utf-8")
            meta, body, parser = frontmatter(raw, path.stem if source["legacy"] else None)
            auto, policy = implicit_policy(path, meta, args.platform)
        except Exception as exc:
            findings.append(issue("unreadable", "warning", "技能无法读取或解析", str(exc), "检查 SKILL.md 的编码、frontmatter 与权限。", paths=[str(path)]))
            continue
        visible = real in observed
        info = observed.get(real, {})
        desc = info.get("description", meta["description"])
        name = info.get("name", meta["name"])
        if not isinstance(desc, str) or not isinstance(name, str):
            raise ValueError("catalog.name 和 description 必须是字符串")
        links = local_links(body, path)
        record = {"id": sid, "name": name, "description": desc, "path": real, "aliases": [str(path)],
                  "sources": [source["source"]], "priority": source["priority"], "sha256": digest(raw),
                  "parser": parser, "visibility": "observed" if visible else "unverified", "implicit": auto,
                  "policy": policy, "eligible": auto, "shadowed_by": None,
                  "tokens": counter.count(body), "catalog_tokens": counter.count(f"{name}: {desc} ({real})"),
                  "lines": len(body.splitlines()), "references": links,
                  "concepts": [key for key, pattern in CONCEPTS.items() if re.search(pattern, desc, re.I)]}
        by_real[real], bodies[sid] = record, raw
        if len(desc) > 600 or len(record["concepts"]) >= 6 or re.search(r"any task|all tasks|anything|任何任务|所有任务|万能", desc, re.I):
            findings.append(issue("broad_trigger", "info", "简介范围记录", "简介较长、涉及多类任务或包含通用触发词；这些特征本身不能证明误触发。", "先确认是否承担合理的综合职责；只有发现具体误选情景时，才建议收窄简介。", [sid]))
        if record["tokens"]["estimate"] > 3000 and not any(link["exists"] for link in links):
            findings.append(issue("long_body", "info", "使用说明的 Token 信息", f"读取使用说明估算占用 {record['tokens']['estimate']} Token。未发现有效的本地 Markdown 文件链接；这不代表没有按需加载设计。", "完整工作流可以保持现状。长度不代表质量问题；只有确认存在可独立加载的内容及具体收益时，才考虑拆分。", [sid]))
        for link in links:
            if not link["exists"]:
                findings.append(issue("broken_reference", "warning", "本地引用失效", f"链接目标不存在：{link['reference']}", "修复引用或说明它是运行时生成的文件。", [sid]))
    skills = sorted(by_real.values(), key=lambda s: (s["name"], s["path"]))
    if args.platform == "claude-code":
        for name in {s["name"] for s in skills if ":" not in s["name"]}:
            group = [s for s in skills if s["name"] == name and s["visibility"] != "observed" and s["priority"] in range(1, 1000)]
            if len(group) > 1:
                winner = max(group, key=lambda s: s["priority"])
                for loser in group:
                    if loser is not winner and loser["priority"] < winner["priority"]:
                        loser["shadowed_by"], loser["eligible"] = winner["id"], False
                        findings.append(issue("shadowed", "info", "同名技能按来源优先级被覆盖", f"{loser['name']} 的 {', '.join(loser['sources'])} 来源被更高优先级来源覆盖。", "保留需要的来源，避免维护不会生效的副本；会话实际目录优先。", [loser["id"], winner["id"]]))
    for sha in {s["sha256"] for s in skills}:
        group = [s for s in skills if s["sha256"] == sha]
        if len(group) > 1:
            findings.append(issue("duplicate_copy", "hint", "独立安装副本内容相同", f"{len(group)} 个不同真实文件拥有相同内容；不等于平台一定重复注入。", "检查是否需要保留多个来源；同目标符号链接已去重。", [s["id"] for s in group]))
    candidates = candidates_for(skills)
    selected, omitted = candidates[:args.max_candidates], max(0, len(candidates) - args.max_candidates)
    fingerprint = digest(json.dumps({"platform": args.platform, "skills": skills, "candidates": selected}, sort_keys=True, ensure_ascii=False))
    data = {"version": VERSION, "scan_id": fingerprint, "generated_at": datetime.now(timezone.utc).isoformat(),
            "platform": args.platform, "host_evidence": args.host_evidence or "由执行模型通过 --platform 声明，未提供额外依据",
            "cwd": str(cwd), "roots": roots, "catalog_provided": bool(args.catalog), "token_method": counter.method,
            "token_scope": "name + description + path 的规范化目录估算，非宿主实际注入字符串；正文不含引用文件",
            "skills": skills, "findings": findings, "candidates": selected,
            "candidate_total": len(candidates), "omitted_candidates": omitted, "input_entries": total_entries,
            "deduplicated_entries": total_entries - len(skills) - sum(f["kind"] == "unreadable" and "技能" in f["title"] for f in findings),
            "limitations": ["未读取历史会话；无真实触发率、实际账单或完整上下文注入记录。", "候选筛查可能漏掉语义相近但词面不同的触发条件；筛查分数不代表冲突概率。", "默认不扫描全部插件缓存、账户同步、企业或按文件作用域加入的嵌套技能；可由宿主补充实际路径。", "字符估算区间是启发式范围，不是统计置信区间。"]}
    out.mkdir(parents=True, exist_ok=True)
    dump(out / "scan.json", data)
    dump(out / "summary.json", {"scan_id": fingerprint, "platform": args.platform, "skills": len(skills), "observed": sum(s["visibility"] == "observed" for s in skills), "findings": len(findings), "candidate_total": len(candidates), "candidate_selected": len(selected), "candidate_omitted": omitted, "token_method": counter.method})
    index = {s["id"]: s for s in skills}
    dump(out / "review-tasks.json", {"scan_id": fingerprint, "candidates": [{**c, "skills": [{"id": index[sid]["id"], "name": index[sid]["name"], "path": index[sid]["path"], "description": index[sid]["description"], "policy": index[sid]["policy"]} for sid in c["skill_ids"]]} for c in selected]})
    dump(out / "routing-index.json", {"scan_id": fingerprint, "skills": [{k: s[k] for k in ("id", "name", "description", "path", "implicit", "policy", "concepts")} for s in skills]})
    dump(out / "finding-review-tasks.json", {"scan_id": fingerprint, "findings": findings})
    render(data, None, out / "report.html")
    return {"report": str(out / "report.html"), "skills": len(skills), "candidates": len(selected), "omitted": omitted, "status": "静态初检，候选尚待模型复核"}


def apply_reviews(data: dict, reviews: dict | None) -> dict:
    data = json.loads(json.dumps(data))
    data["reviewer"] = None
    if reviews is None:
        return data
    if reviews.get("scan_id") != data["scan_id"]:
        raise ValueError("复核文件 scan_id 与本次扫描不一致")
    if not isinstance(reviews.get("reviewer"), str) or not reviews["reviewer"].strip():
        raise ValueError("复核文件缺少 reviewer")
    by_skill = {s["id"]: s for s in data["skills"]}
    by_candidate = {c["id"]: c for c in data["candidates"]}
    seen, texts = set(), {}
    review_list = reviews.get("reviews")
    if not isinstance(review_list, list):
        raise ValueError("reviews 必须是数组")
    for review in review_list:
        if "candidate_id" in review:
            if review["candidate_id"] not in by_candidate:
                raise ValueError("未知 candidate_id")
            candidate = by_candidate[review["candidate_id"]]
        else:
            ids = review.get("skill_ids")
            if not isinstance(ids, list) or len(ids) != 2 or len(set(ids)) != 2 or any(s not in by_skill for s in ids):
                raise ValueError("新增候选必须引用两个已扫描且不同的 skill_ids")
            cid = pair_id(*ids)
            candidate = by_candidate.get(cid)
            if candidate is None:
                candidate = {"id": cid, "skill_ids": ids, "score": None, "signals": [], "origin": "model", "verdict": "pending"}
                by_candidate[cid] = candidate
                data["candidates"].append(candidate)
        if candidate["id"] in seen:
            raise ValueError("同一候选重复复核")
        seen.add(candidate["id"])
        verdict = review.get("verdict")
        if verdict not in VERDICTS:
            raise ValueError(f"无效 verdict：{verdict}")
        for field in ("trigger", "reason", "recommendation"):
            if not isinstance(review.get(field), str) or not review[field].strip():
                raise ValueError(f"复核结论缺少 {field}")
        preferred = review.get("preferred_skill_id")
        if preferred is not None and preferred not in candidate["skill_ids"]:
            raise ValueError("preferred_skill_id 必须来自当前候选")
        evidence = review.get("evidence", [])
        if not isinstance(evidence, list):
            raise ValueError("evidence 必须是数组")
        validated = []
        for item in evidence:
            sid, quote = item.get("skill_id"), item.get("quote")
            if sid not in candidate["skill_ids"] or not isinstance(quote, str) or not quote.strip():
                raise ValueError("证据必须引用候选 skill 和非空连续原文")
            if sid not in texts:
                raw = Path(by_skill[sid]["path"]).read_text(encoding="utf-8")
                if digest(raw) != by_skill[sid]["sha256"]:
                    raise ValueError("技能文件在扫描后发生变化，请重新扫描再复核")
                texts[sid] = raw
            if quote not in texts[sid]:
                raise ValueError(f"证据原文不存在于 {by_skill[sid]['name']}")
            validated.append({"skill_id": sid, "quote": quote, "path": by_skill[sid]["path"], "line": texts[sid][:texts[sid].index(quote)].count("\n") + 1})
        if {e["skill_id"] for e in validated} != set(candidate["skill_ids"]):
            raise ValueError("复核结论需要双方技能的原文证据")
        candidate.update({"verdict": verdict, "trigger": review["trigger"], "reason": review["reason"], "recommendation": review["recommendation"], "preferred_skill_id": preferred, "evidence": validated})
        for field in ("title", "impact"):
            if field in review:
                if not isinstance(review[field], str) or not review[field].strip():
                    raise ValueError(f"复核结论的 {field} 必须是非空字符串")
                candidate[field] = review[field]
    data["reviewer"] = reviews["reviewer"]
    finding_reviews = reviews.get("finding_reviews", [])
    if not isinstance(finding_reviews, list):
        raise ValueError("finding_reviews 必须是数组")
    by_finding = {f["id"]: f for f in data["findings"]}
    seen_findings = set()
    for review in finding_reviews:
        fid = review.get("finding_id")
        if fid not in by_finding or fid in seen_findings:
            raise ValueError("未知或重复的 finding_id")
        seen_findings.add(fid)
        finding = by_finding[fid]
        level = review.get("level")
        if level not in {"action", "watch", "info"} or finding["kind"] == "unreadable":
            raise ValueError("无效的文件检查分级；读取限制应保留在检查范围中")
        for field in ("reason", "recommendation"):
            if not isinstance(review.get(field), str) or not review[field].strip():
                raise ValueError(f"文件检查结论缺少 {field}")
        validated = []
        evidence = review.get("evidence", [])
        if not isinstance(evidence, list):
            raise ValueError("evidence 必须是数组")
        for item in evidence:
            sid, quote = item.get("skill_id"), item.get("quote")
            if sid not in finding["skill_ids"] or not isinstance(quote, str) or not quote.strip():
                raise ValueError("文件检查证据必须来自相关技能的连续原文")
            raw = Path(by_skill[sid]["path"]).read_text(encoding="utf-8")
            if digest(raw) != by_skill[sid]["sha256"]:
                raise ValueError("技能文件在扫描后发生变化，请重新扫描再复核")
            if quote not in raw:
                raise ValueError("文件检查证据原文不存在")
            validated.append({"skill_id": sid, "quote": quote, "path": by_skill[sid]["path"], "line": raw[:raw.index(quote)].count("\n") + 1})
        if level == "action":
            if not validated or any(not isinstance(review.get(k), str) or not review[k].strip() for k in ("trigger", "impact")):
                raise ValueError("优先建议需要具体适用情景、影响和原文证据")
        finding["review"] = {k: review.get(k, "") for k in ("level", "reason", "recommendation", "trigger", "impact")}
        finding["review"]["evidence"] = validated
    return data


def prepare_report(data: dict) -> dict:
    """Separate decisions from observations without changing the audit evidence."""
    result = json.loads(json.dumps(data))
    result["report_version"] = VERSION
    levels = {"long_body": "info", "broad_trigger": "info", "shadowed": "info",
              "duplicate_copy": "watch", "broken_reference": "watch", "unreadable": "coverage"}
    for finding in result["findings"]:
        finding["level"] = finding.get("review", {}).get("level", levels.get(finding["kind"], "info"))
    actions, watch = [], []
    for candidate in result["candidates"]:
        verdict = candidate["verdict"]
        if verdict == "confirmed":
            actions.append({"type": "candidate", "id": candidate["id"]})
        elif verdict in {"risk", "needs_context"}:
            watch.append({"type": "candidate", "id": candidate["id"]})
    for finding in result["findings"]:
        item = {"type": "finding", "id": finding["id"]}
        if finding["level"] == "action":
            actions.append(item)
        elif finding["level"] == "watch":
            watch.append(item)
    pending = sum(c["verdict"] == "pending" for c in result["candidates"])
    coverage = sum(f["level"] == "coverage" for f in result["findings"])
    incomplete = bool(pending or result.get("omitted_candidates", 0) or coverage)
    if actions:
        conclusion = f"有 {len(actions)} 项建议优先处理"
        detail = "先看具体使用情景，再决定是否调整。其余信息可以按需查看。"
    elif incomplete or (result["candidates"] and not result.get("reviewer")):
        conclusion = "检查尚未完成，暂不判断整体配置"
        detail = "当前没有已确认的优先建议；部分组合或文件还未检查完成。"
    elif not result["skills"]:
        conclusion = "当前范围内没有找到可读取的技能"
        detail = "请先确认平台提供的技能目录，报告暂时不能判断配置。"
    elif watch:
        conclusion = "已检查范围内，没有需要优先处理的事项"
        detail = f"有 {len(watch)} 项可以留意，尚未确认具体影响。可以先保持现有配置。"
    else:
        conclusion = "已检查范围内，可以保持现有配置"
        detail = "未发现需要优先处理的事项。长度和 Token 估算保留为配置信息。"
    result["presentation"] = {"actions": actions, "watch": watch, "pending": pending, "coverage": coverage,
                              "incomplete": incomplete, "conclusion": conclusion, "detail": detail}
    return result


def render(data: dict, reviews: dict | None, out: Path) -> dict:
    result = prepare_report(apply_reviews(data, reviews))
    # Escape every '<' so skill descriptions cannot terminate the JSON script element.
    payload = json.dumps(result, ensure_ascii=False).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    template = Path(__file__).resolve().parent.parent / "assets" / "report.html"
    html = template.read_text(encoding="utf-8").replace("__AUDIT_DATA__", payload, 1)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    reviewed = sum(c["verdict"] != "pending" for c in result["candidates"])
    return {"report": str(out.resolve()), "reviewed": reviewed, "pending": len(result["candidates"]) - reviewed}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("scan", help="只读扫描并生成静态初检报告")
    collect.add_argument("--platform", required=True, choices=PLATFORMS)
    collect.add_argument("--cwd", default=os.getcwd())
    collect.add_argument("--root", action="append", default=[])
    collect.add_argument("--only-roots", action="store_true")
    collect.add_argument("--catalog")
    collect.add_argument("--host-evidence")
    collect.add_argument("--out", required=True)
    collect.add_argument("--max-candidates", type=int, default=60)
    collect.add_argument("--encoding", help="可选 tiktoken encoding；需要环境预先安装，不自动下载依赖")
    view = sub.add_parser("render", help="验证模型复核并生成最终 HTML")
    view.add_argument("--scan", required=True)
    view.add_argument("--reviews")
    view.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "scan":
            if args.max_candidates < 1:
                raise ValueError("--max-candidates 必须大于零")
            result = scan(args)
        else:
            data = json.loads(Path(args.scan).read_text(encoding="utf-8"))
            reviews = json.loads(Path(args.reviews).read_text(encoding="utf-8")) if args.reviews else None
            result = render(data, reviews, Path(args.out).expanduser().resolve())
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"体检失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
