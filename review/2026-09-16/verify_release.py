"""Reproduce the 1.3.4 release checks without a live AstrBot or QQ connection."""
import hashlib
import importlib.metadata
import json
import platform
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = Path(__file__).resolve().parent
VERSION = "1.3.4"
RESULT = OUTPUT / f"release-{VERSION}.json"


def run_check(name, command):
    start = time.monotonic()
    completed = subprocess.run(command, cwd=ROOT, capture_output=True,
                               text=True, encoding="utf-8", errors="replace")
    text = completed.stdout + completed.stderr
    (OUTPUT / f"release-{name}.log").write_text(text, encoding="utf-8")
    result = {"command": [Path(command[0]).name, *command[1:]],
              "exit_code": completed.returncode, "seconds": round(time.monotonic()-start, 3)}
    if name == "python-tests":
        count = re.search(r"Ran (\d+) tests? in", text)
        result["tests"] = int(count[1]) if count else None
        result["skipped"] = len(re.findall(r"\.\.\. skipped ", text))
    if name == "frontend-tests":
        for key in ("tests", "pass", "fail", "skipped"):
            match = re.search(rf"^# {key} (\d+)$", text, re.MULTILINE)
            result[key] = int(match[1]) if match else None
    print(f"{name}: {'PASS' if completed.returncode == 0 else 'FAIL'}", flush=True)
    return result


def run():
    metadata = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
    if not re.search(rf"(?m)^version: {re.escape(VERSION)}$", metadata):
        raise SystemExit(f"This verifier targets v{VERSION}; use that revision to reproduce it.")
    node = shutil.which("node")
    if not node:
        raise SystemExit("Node.js is required for frontend checks")
    approved = json.loads((OUTPUT / "acceptance.json").read_text(encoding="utf-8"))["image_sha256"]
    checks = {
        "python": run_check("python-tests", [sys.executable, "-X", "utf8", "-B", "-W", "error",
                                               "-m", "unittest", "discover", "-s", "tests", "-v"]),
        "frontend": run_check("frontend-tests", [node, "--test", "--test-reporter=tap", "tests/test_frontend.cjs"]),
        "cached_previews": run_check("preview", [sys.executable, "-X", "utf8", "-B",
                                                   "review/2026-09-16/make_preview.py", "--cached"]),
        "diff": run_check("diff-check", ["git", "diff", "--check"]),
    }
    paths = [ROOT / "main.py", ROOT / "webui.py"]
    for folder in ("core", "backend", "tests", "review/2026-09-16"):
        paths.extend(sorted((ROOT / folder).glob("*.py")))
    for path in paths:
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    assert schema["contest_report"]["items"]["enabled"]["default"] is False
    checks["compile_and_config"] = {"exit_code": 0, "python_files": len(paths), "reports_default_off": True}
    image_hashes = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in approved}
    assert image_hashes == approved, "Rendered images changed from the approved preview"
    checks["approved_images"] = {"exit_code": 0, "images": len(approved), "unchanged": True}
    documents = [ROOT / name for name in ("README.md", "PLUGIN_STRUCTURE.md", "ACCEPTANCE_REPORT.md", "NOTICE.md")]
    documents.append(OUTPUT / "ACCEPTANCE.md")
    links_checked = 0
    for document in documents:
        for target in re.findall(r"\]\(([^)]+)\)", document.read_text(encoding="utf-8")):
            target = target.split("#", 1)[0]
            if not target or re.match(r"^[a-z]+:", target, re.IGNORECASE):
                continue
            path = (document.parent / unquote(target)).resolve()
            if path.suffix == ".log" or path == RESULT:
                continue  # Historical local logs are explicitly excluded from Git.
            assert path.exists(), f"Broken local link in {document.name}: {target}"
            links_checked += 1
    checks["documentation_links"] = {"exit_code": 0, "checked": links_checked}
    paths.extend(documents)
    paths.extend(ROOT / name for name in ("metadata.yaml", "_conf_schema.json", "CHANGELOG.md", ".gitignore",
                 "public/index.html", "tests/test_frontend.cjs", "tests/fixtures/contest_reports_2262_2264.json",
                 "review/2026-09-16/preview-data.json"))
    result = {
        "version": VERSION, "checked_at": datetime.now(timezone.utc).isoformat(),
        "base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "passed": all(value["exit_code"] == 0 for value in checks.values()), "checks": checks,
        "environment": {"os": platform.platform(), "python": platform.python_version(),
                        "node": subprocess.check_output([node, "--version"], text=True).strip(),
                        "packages": {name: importlib.metadata.version(name) for name in
                                     ("aiohttp", "aiosqlite", "Quart", "APScheduler", "Pillow")}},
        "source_sha256_utf8_lf": {str(path.relative_to(ROOT)).replace("\\", "/"):
            hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest() for path in paths},
        "image_sha256": image_hashes,
        "boundaries": ["No live QQ messages sent; AstrBot and OneBot use test doubles",
                       "No Linux execution", "Public API previews are the approved saved snapshot"],
    }
    RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print("Release evidence: " + str(RESULT.relative_to(ROOT)), flush=True)
    return result["passed"]


if __name__ == "__main__":
    sys.exit(not run())
