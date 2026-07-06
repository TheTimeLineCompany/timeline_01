"""Summarize the CPU entity/temporal probe output."""

from __future__ import annotations

import json
from pathlib import Path


def main() -> None:
    path = Path(__file__).resolve().parents[2] / "tasks" / "033_cpu_entity_temporal_probe_output.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raw = path.read_text(encoding="utf-16")
    json_start = raw.find("[")
    if json_start > 0:
        raw = raw[json_start:]
    data = json.loads(raw)
    for case in data:
        print(f"CASE {case['id']}")
        for lane in ["spacy", "gliner2", "gliner_decoder"]:
            lane_data = case[lane]
            warnings = lane_data.get("warnings") or []
            preview = [(item["text"], item["label"]) for item in lane_data.get("entities", [])[:14]]
            print(f"  {lane}: {lane_data['count']} warning_count={len(warnings)}")
            if warnings:
                print(f"    warning: {warnings[0]}")
            print(f"    {preview}")
        temporal = [
            (item["label"], item["time_ref_id"], item["precision"])
            for item in case["temporal"].get("matches", [])
        ]
        print(f"  temporal: {case['temporal']['count']}")
        print(f"    {temporal}")
        print()


if __name__ == "__main__":
    main()
