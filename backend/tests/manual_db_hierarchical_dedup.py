"""Manual database-driven test for the hierarchical CSR dedup pipeline.

Run from backend/:

    python3 -I tests/manual_db_hierarchical_dedup.py --limit 4

The script reads the local tara_local.db, selects assets with a completed
Stage5 result from the latest completed run, and runs:

    per-asset whole-requirement dedup
    -> balanced hierarchical group dedup
    -> final atomization
    -> atomic-level dedup

It writes the full result to /tmp/hierarchical_dedup_db_test.json and prints
counts plus a sample.  The API key is never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.csr_hierarchical_dedup import run_pipeline, source_ids_of


def load_assets(db_path: str, limit: int):
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    run_row = cur.execute(
        "select id from tara_runs where status='completed' order by id desc limit 1"
    ).fetchone()
    if not run_row:
        raise SystemExit("no completed tara_run found")
    run_id = run_row[0]
    rows = cur.execute(
        """
        select distinct a.id
        from assets a
        join tara_steps s on s.asset_id = a.id
        where s.stage='stage5' and s.status='completed' and s.run_id=?
        order by a.id
        limit ?
        """,
        (run_id, limit),
    ).fetchall()
    asset_ids = [r[0] for r in rows]
    if not asset_ids:
        raise SystemExit(f"no completed stage5 assets for run {run_id}")

    assets_data = []
    for asset_id in asset_ids:
        asset = cur.execute(
            "select id,name,asset_type,description,protocol,diagram_id,status from assets where id=?",
            (asset_id,),
        ).fetchone()
        step = cur.execute(
            "select analysis_result from tara_steps where asset_id=? and stage='stage5' and run_id=? order by id desc limit 1",
            (asset_id, run_id),
        ).fetchone()
        if not step:
            continue
        final_output = json.loads(step[0]).get("final_output", {})
        requirements = final_output.get("requirements") or final_output.get("summarized_csrs") or []
        if not requirements:
            continue
        assets_data.append({
            "asset": {
                "id": asset[0],
                "name": asset[1],
                "asset_type": asset[2] or "",
                "description": asset[3] or "",
                "protocol": asset[4] or "",
                "diagram_id": asset[5],
                "status": asset[6] or "confirmed",
            },
            "requirements": requirements,
        })

    settings_row = cur.execute(
        "select api_base_url, api_key, model_name from system_settings limit 1"
    ).fetchone()
    con.close()
    settings = None
    if settings_row and settings_row[1]:
        settings = {
            "api_base_url": settings_row[0],
            "api_key": settings_row[1],
            "model_name": settings_row[2],
        }
    return run_id, assets_data, settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="/root/tara-ai-platform/tara_local.db")
    parser.add_argument("--limit", type=int, default=4)
    args = parser.parse_args()

    run_id, assets_data, settings = load_assets(args.db, args.limit)
    print(f"run_id={run_id} assets={len(assets_data)} llm={'yes' if settings else 'no'}")
    if not assets_data:
        raise SystemExit("no assets with requirements")

    finals, report = run_pipeline(assets_data, settings)
    print("\n=== report ===")
    print(json.dumps(report, ensure_ascii=False, indent=2))

    print("\n=== sample finals ===")
    for row in finals[:12]:
        print(
            f"src={source_ids_of(row)} "
            f"subject={row.get('subject')} :: "
            f"{str(row.get('text') or row.get('cybersecurity_requirement'))[:260]}"
        )

    out = {
        "run_id": run_id,
        "asset_ids": [x["asset"]["id"] for x in assets_data],
        "report": report,
        "finals": [
            {k: (sorted(v) if isinstance(v, set) else v) for k, v in row.items()}
            for row in finals
        ],
    }
    out_path = "/tmp/hierarchical_dedup_db_test.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print(f"\nsaved {out_path}")


if __name__ == "__main__":
    main()
