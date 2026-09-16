"""Fetch public records and render previews without enabling or sending production reports."""
import asyncio
import argparse
import json
import sys
import tempfile
import shutil
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from core.cf_api import request_cf_api
from core.contest_report import build_report, fetch_member_evidence
from core.contest_report_image import render_report_pages
from core.rate_limit import configure_codeforces_api_rate_limiter

OUTPUT = Path(__file__).resolve().parent
HANDLES = ("StarSilk", "Zinc-acetate", "rainboy")


async def run(contest_ids):
    with tempfile.TemporaryDirectory(prefix="cf-report-preview-") as temp:
        configure_codeforces_api_rate_limiter(Path(temp) / "fixture.db")
        async with aiohttp.ClientSession() as session:
            async def get(method, params):
                data = await request_cf_api(session, method, params, timeout=60)
                if data.get("status") != "OK":
                    raise RuntimeError(f"{method}: {data.get('comment')}")
                return data["result"]
            prepared = []
            for cid in contest_ids:
                changes = await get("contest.ratingChanges", {"contestId": cid})
                standings = await get("contest.standings", {"contestId": cid})
                prepared.append((cid, changes, standings))
            since = min(item[2]["contest"]["startTimeSeconds"] for item in prepared)
            members = await fetch_member_evidence(session, HANDLES, since)
            previews = []
            snapshots = []
            for cid, changes, standings in prepared:
                report = build_report(standings, changes, HANDLES, members)
                if not report["rows"]:
                    continue
                for page, content in enumerate(render_report_pages(report, ROOT / "resources/SourceHanSansSC-Bold.otf"), 1):
                    original = OUTPUT / f"contest-{cid}-page-{page}.png"
                    archived = OUTPUT / "correction" / f"prior-contest-{cid}-page-{page}.png"
                    archived.parent.mkdir(parents=True, exist_ok=True)
                    if original.exists() and not archived.exists():
                        shutil.copy2(original, archived)
                    original.write_bytes(content)
                    (OUTPUT / f"contest-{cid}-corrected-page-{page}.png").write_bytes(content)
                previews.append(report)
                selected = {handle.casefold() for handle in HANDLES}
                snapshots.append({
                    "contest_id": cid,
                    "rating_changes": [row for row in changes if row["handle"].casefold() in selected],
                    "standings": {"contest": standings["contest"], "problems": standings["problems"],
                                  "rows": [row for row in standings["rows"] if any(
                                      member["handle"].casefold() in selected
                                      for member in row["party"]["members"])]},
                })
                print(json.dumps(report, ensure_ascii=False), flush=True)
            evidence = {"checked_at": datetime.now(timezone.utc).isoformat(), "handles": HANDLES,
                        "requested_contests": contest_ids,
                        "rank_basis": "personal_contest_history",
                        "member_evidence": {key: {**value,
                            "history": [row for row in value["history"] if row["contestId"] in contest_ids],
                            "submissions": [row for row in value["submissions"] if row.get("contestId") in contest_ids]}
                            for key, value in members.items()},
                        "reports": previews, "source_snapshots": snapshots,
                        "sources": [f"https://codeforces.com/api/{method}?contestId={cid}"
                                    for cid in contest_ids for method in ("contest.ratingChanges", "contest.standings")]
                                   + [f"https://codeforces.com/api/{method}?handle={handle}"
                                      for handle in HANDLES for method in ("user.rating", "user.status")]}
            (OUTPUT / "preview-data.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cached", action="store_true", help="Render the saved official data without network requests")
    parser.add_argument("--contests", type=int, nargs="+", default=[2264, 2262], help="Contest IDs for this review; do not infer participation from rating history")
    args = parser.parse_args()
    if args.cached:
        evidence = json.loads((OUTPUT / "preview-data.json").read_text(encoding="utf-8"))
        if evidence.get("rank_basis") != "personal_contest_history":
            raise SystemExit("This is an old preview. Refresh the official member evidence first.")
        for report in evidence["reports"]:
            for page, content in enumerate(render_report_pages(report, ROOT / "resources/SourceHanSansSC-Bold.otf"), 1):
                (OUTPUT / f"contest-{report['contest_id']}-page-{page}.png").write_bytes(content)
                (OUTPUT / f"contest-{report['contest_id']}-corrected-page-{page}.png").write_bytes(content)
    else:
        asyncio.run(run(args.contests))
