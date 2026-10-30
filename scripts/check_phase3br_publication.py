"""Inspect the exact publication files without printing suspected secret values."""

import argparse
import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path

PATTERNS = {
    "google_api_key": re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    "github_token": re.compile(r"(?:gh[pousr]_[0-9A-Za-z]{30,}|github_pat_[0-9A-Za-z_]{40,})"),
    "telegram_bot_token": re.compile(r"\b[0-9]{7,12}:[A-Za-z0-9_-]{30,}\b"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "database_credentials": re.compile(r"(?:postgres(?:ql)?|mysql|redis)://[^\s:/]+:[^\s@]+@", re.I),
    "secret_query": re.compile(r"[?&](?:secret|api_key|token|password)=[A-Za-z0-9_-]{12,}", re.I),
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
}
SECRET_NAME = re.compile(r"^(?:GEMINI_API_KEY|GOOGLE_API_KEY|API_KEY|TG_TOKEN|TELEGRAM_TOKEN|TELEGRAM_REMINDER_SECRET|PASSWORD|CLIENT_SECRET|ACCESS_TOKEN|REFRESH_TOKEN|DATABASE_URL)$", re.I)


def scan(root, output):
    tracked = subprocess.check_output(["git", "diff", "--name-only", "69207c261d84a0fdb21f967a68cfa2e643713951"], cwd=root, text=True).splitlines()
    untracked = subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard"], cwd=root, text=True).splitlines()
    paths = sorted(set(tracked + untracked) - {str(output.relative_to(root)).replace("\\", "/")})
    findings, manifest = [], []
    for relative in paths:
        path = root / relative
        if not path.is_file():
            continue
        raw = path.read_bytes()
        text = raw.decode("utf-8-sig")
        manifest.append({"path": relative, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
        if path.name == "release_history_snapshot.json":
            findings.append({"path": relative, "kind": "raw_production_history_not_for_publication", "line": 1})
        for name, regex in PATTERNS.items():
            for match in regex.finditer(text):
                findings.append({"path": relative, "kind": name, "line": text.count("\n", 0, match.start()) + 1})
        if path.suffix == ".py":
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and SECRET_NAME.fullmatch(target.id) and node.value.value:
                            findings.append({"path": relative, "kind": "literal_secret_assignment", "line": node.lineno})
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"getenv", "get"}:
                    if len(node.args) >= 2 and isinstance(node.args[0], ast.Constant) and SECRET_NAME.fullmatch(str(node.args[0].value)):
                        if isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str) and node.args[1].value:
                            findings.append({"path": relative, "kind": "literal_secret_default", "line": node.lineno})
    result = {"scope": "Only Phase 3B-R changed/new publication files, including entire changed Python files",
              "methods": ["credential token patterns", "secret-bearing URLs", "Python literal/default AST checks"],
              "findings": findings, "passed": not findings, "files": manifest,
              "excluded_private_input": "release_history_snapshot.json (production lexical history; retained locally)",
              "limitation": "Pattern/AST review is not proof against every possible encoding or unknown secret format."}
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"scanned_files": len(manifest), "findings": findings, "passed": not findings}))
    raise SystemExit(bool(findings))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    scan(args.root.resolve(), args.output.resolve())
