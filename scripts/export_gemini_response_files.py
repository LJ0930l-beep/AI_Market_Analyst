"""Export recorded Gemini JSON replies from consistent read-only replay snapshots."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import zipfile


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def strict_json(raw: str):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("DUPLICATE_JSON_KEY")
            result[key] = value
        return result
    def reject(value):
        raise ValueError("NONFINITE_JSON_NUMBER:" + value)
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=reject)


def export(directory: Path, output: Path | None = None) -> dict:
    directory = Path(directory).resolve()
    registration = strict_json((directory / "research-plan.json").read_text(encoding="utf-8"))
    if registration["plan"].get("research_only") is not True:
        raise ValueError("RESEARCH_DATABASE_REQUIRED")
    output = Path(output).resolve() if output else directory / "gemini-json"
    # New timestamped batches preserve earlier exports without deleting files.
    output.mkdir(parents=True, exist_ok=True)
    batch = output / datetime.now(timezone.utc).strftime("export-%Y%m%dT%H%M%S%fZ")
    batch.mkdir()
    records, files = [], []
    for window in registration["plan"]["pilot_windows"]:
        name = str(window["id"])
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError("INVALID_WINDOW_PATH")
        database = directory / name / "results.sqlite3"
        if not database.exists():
            continue
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as original:
            with sqlite3.connect(":memory:") as db:
                original.backup(db)
                db.row_factory = sqlite3.Row
                rows = db.execute("SELECT * FROM ai_template_replay_decisions ORDER BY rowid").fetchall()
                for index, row in enumerate(rows, 1):
                    context = strict_json(row["context_json"]) if row["context_json"] else {}
                    settings = context.get("model_inference_settings") or {}
                    audit = settings.get("model_response_audit") or {}
                    attempts = audit.get("attempts") or []
                    stamp = re.sub(r"[^0-9TZ]", "", str(row["as_of"]))
                    target = batch / name / f"{index:04d}_{stamp}"
                    target.mkdir(parents=True)
                    entry = {"window": name, "scan_index": index, "as_of": row["as_of"],
                             "scan_key": row["scan_key"], "row_status": row["status"],
                             "error_code": row["error_code"], "attempts": [],
                             "omitted_attempts": audit.get("omitted_attempts", 0)}
                    for number, attempt in enumerate(attempts, 1):
                        raw = attempt.get("raw_response")
                        meta = {k: v for k, v in attempt.items() if k != "raw_response"}
                        label = "initial" if number == 1 else f"followup-{number-1}"
                        if isinstance(raw, str):
                            valid, error = True, None
                            try:
                                strict_json(raw)
                            except (ValueError, TypeError) as exc:
                                valid, error = False, str(exc)
                            suffix = ".json" if valid and not attempt.get("raw_response_truncated") else ".txt"
                            path = target / f"{number:02d}-{label}-raw{suffix}"
                            data = raw.encode("utf-8")
                            path.write_bytes(data)  # Preserve the exact recorded response text.
                            meta.update(raw_file=path.relative_to(batch).as_posix(), raw_sha256=sha(data),
                                        recorded_json_valid=valid, json_error=error,
                                        recorded_not_original_sse_bytes=True)
                        else:
                            meta["raw_response_missing"] = True
                        save(target / f"{number:02d}-{label}-receipt.json", meta)
                        entry["attempts"].append(meta)
                    if row["decision_json"]:
                        decision = strict_json(row["decision_json"])
                        save(target / "effective-decision.json", decision)
                        entry["effective_action"] = decision.get("action")
                    save(target / "normalizations.json", {
                        "normalizations": settings.get("model_output_normalizations") or [],
                        "scope": "SYSTEM_PROJECTION_SEPARATE_FROM_RECORDED_MODEL_RAW_RESPONSE"})
                    save(target / "scan.json", entry)
                    records.append(entry)
    for path in sorted(batch.rglob("*")):
        if path.is_file():
            files.append({"path": path.relative_to(batch).as_posix(), "sha256": sha(path.read_bytes())})
    manifest = {"scope": "RECORDED_GEMINI_REPLIES_NOT_PROFIT_OR_GATE_ACCEPTANCE",
                "plan_sha256": registration["plan_sha256"], "exported_at": datetime.now(timezone.utc).isoformat(),
                "scans": len(records), "recorded_model_attempts": sum(len(r["attempts"]) for r in records),
                "row_statuses": {status: sum(r["row_status"] == status for r in records)
                                 for status in sorted({r["row_status"] for r in records})},
                "missing_raw_attempts": sum(a.get("raw_response_missing", False) for r in records for a in r["attempts"]),
                "omitted_attempts": sum(r["omitted_attempts"] for r in records),
                "files": files, "records": records,
                "private_exchange_calls": 0, "model_calls_added": 0}
    save(batch / "manifest.json", manifest)
    (batch / "说明.txt").write_text(
        "按窗口、扫描时间排列。*-raw.json保留本地记录的Gemini原始回复文字；补齐回复另存。\n"
        "effective-decision.json是系统采用的决策；normalizations.json说明字段投影。\n"
        "错误或不完整回复保存为txt；缺失/歧义调用在scan及manifest标明，不能视为成功。\n"
        "这是历史研究回复，非Gate实际成交或盈利验收证据。时间使用UTC。\n", encoding="utf-8")
    archive = batch.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        for path in sorted(batch.rglob("*")):
            if path.is_file():
                zipped.write(path, path.relative_to(batch))
    receipt = {"directory": str(batch), "zip": str(archive), "zip_sha256": sha(archive.read_bytes()),
               "scans": manifest["scans"], "recorded_model_attempts": manifest["recorded_model_attempts"],
               "missing_raw_attempts": manifest["missing_raw_attempts"], "omitted_attempts": manifest["omitted_attempts"],
               "row_statuses": manifest["row_statuses"], "model_calls_added": 0, "private_exchange_calls": 0}
    save(output / "latest-export.json", receipt)
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    print(json.dumps(export(args.directory, args.output), ensure_ascii=False))
