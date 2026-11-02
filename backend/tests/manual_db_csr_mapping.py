"""Manual database test for the fixed-CSR catalog mapping.

Usage (from backend/):

    python3 -I tests/manual_db_csr_mapping.py --domain-id 1 --limit 20
    python3 -I tests/manual_db_csr_mapping.py --domain-id 1 --limit 20 --persist
    python3 -I tests/manual_db_csr_mapping.py --import-catalog /root/test/csr_new.xlsx

The script reads the local database, maps Stage5 TARA requirements to the
active CSR catalog, prints a summary and writes /tmp/csr_mapping_db_test.json.
It never prints the API key.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: F401 - register tables
from app.core.database import Base, SessionLocal, engine
from app.services.csr_catalog import get_active_catalog, get_active_catalog_version, import_csr_catalog
from app.services.csr_mapping import (
    _settings_from_db,
    collect_tara_requirements,
    map_tara_requirements,
    persist_mappings,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain-id", type=int, default=1)
    parser.add_argument("--run-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--persist", action="store_true")
    parser.add_argument("--import-catalog", default=None)
    args = parser.parse_args()

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if args.import_catalog:
            print("import catalog:", import_csr_catalog(db, args.import_catalog, activate=True))

        version = get_active_catalog_version(db)
        catalog = get_active_catalog(db)
        if version is None or not catalog:
            raise SystemExit("no active CSR catalog; pass --import-catalog first")
        settings = _settings_from_db(db)
        if not settings:
            raise SystemExit("no LLM settings in system_settings")

        collected = collect_tara_requirements(
            db,
            args.domain_id,
            run_id=args.run_id,
            limit=args.limit if args.limit > 0 else None,
        )
        print(
            f"domain={collected['domain'].name} run={collected['run'].id} "
            f"requirements={collected['input_count']} catalog={len(catalog)} "
            f"version={version.version}"
        )
        mapped = map_tara_requirements(
            collected["requirements"],
            catalog,
            settings,
            batch_size=args.batch_size,
        )
        print("\n=== summary ===")
        print(json.dumps(mapped["summary"], ensure_ascii=False, indent=2))

        saved = 0
        if args.persist:
            saved = persist_mappings(
                db,
                collected["domain"],
                collected["run"],
                mapped["rows"],
                catalog_version=version.version,
                replace=True,
            )
            print(f"\npersisted mapping rows: {saved}")

        print("\n=== sample ===")
        for row in mapped["rows"][:15]:
            print(
                f"{row['tara_req_id']} -> {row['target_csr_id'] or 'UNMAPPED'} "
                f"[{row['match_type']}] conf={row['confidence']} "
                f"asset={row['asset_name']}"
            )
            print("  ", (row.get("rationale") or "")[:180])

        out = {
            "domain_id": args.domain_id,
            "run_id": collected["run"].id,
            "catalog_version": version.version,
            "persisted_rows": saved,
            "summary": mapped["summary"],
            "mappings": mapped["rows"],
            "tara_to_targets": mapped["tara_to_targets"],
        }
        out_path = "/tmp/csr_mapping_db_test.json"
        with open(out_path, "w", encoding="utf-8") as handle:
            json.dump(out, handle, ensure_ascii=False, indent=2, default=str)
        print(f"\nsaved {out_path}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
