"""M3, step 0: download the publishers' files again and compare them with the snapshot.

Needs network access to www.ssga.com and sdmx.oecd.org. Run from the repository folder:
    python3 scripts/measure_publisher.py
Writes measure_out/publisher_<month>.json and the downloaded files to measure_out/raw/.
The downloaded files are for audit only; they are not part of the repository.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from measure import publisher  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--snapshot", default=str(ROOT / "data" / "snapshots" / "dgbf_2026-08.json"))
    parser.add_argument("--out", default="measure_out")
    args = parser.parse_args(argv)
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        print("NG: openpyxl is not installed. Run: pip install -r requirements.txt")
        return 2
    snapshot = json.loads(pathlib.Path(args.snapshot).read_text(encoding="utf-8"))
    out_dir = pathlib.Path(args.out)
    record = publisher.run(snapshot, out_dir)
    out = out_dir / f"publisher_{snapshot['month']}.json"
    out.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    failed = [d for d in record["downloads"] if not d.get("md5")]
    print(f"Downloaded {len(record['downloads']) - len(failed)}/{len(record['downloads'])} files.")
    for d in failed:
        print(f"  NG {d['url']}: {d.get('error')}")
    print(f"Compared {record['compared']} values: {record['equal']} equal, {len(record['different'])} different.")
    for row in record["different"][:20]:
        print(f"  {row['item']}: snapshot {row['snapshot']} / publisher {row['publisher']}")
    for note in record["notes"]:
        print(f"Note: {note}")
    print(f"Saved {out}")
    return 0 if not failed and not record["different"] else 3


if __name__ == "__main__":
    sys.exit(main())
