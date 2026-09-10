"""Verify the newly built backend in a private temporary data root, never the installed app."""
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.storage import SQLiteStore
from core.trading.ledger import AccountLedger


def main():
    exe = ROOT / "src-tauri/binaries/ai-market-analyst-backend-x86_64-pc-windows-msvc.exe"
    with tempfile.TemporaryDirectory(prefix="aima-final-smoke-") as directory:
        data = Path(directory)
        database = data / "data/smoke.sqlite3"
        store = SQLiteStore(database)
        store.initialize()
        store.upsert_app_setting("market_hydration.enabled", False)
        ledger = AccountLedger(store)
        ledger.create_account("smoke", mode="PAPER", initial_deposit=Decimal("1000"), config={"venue":"simulated"})
        ledger.record_trade_fill(account_id="smoke", instrument_id="BTCUSDT", side="BUY",
            quantity=Decimal("1"), price=Decimal("100"), fee=Decimal("0"),
            mode="PAPER", venue="simulated", order_id="smoke", trade_id="smoke",
            position_id="smoke", stop_price=90, protection_status="ACTIVE")
        ledger.close()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        token = uuid4().hex
        env = {**os.environ, "AIMA_DATA_ROOT": str(data), "DATABASE_PATH": str(database),
            "AIMA_OWNERSHIP_TOKEN": token, "AIMA_PACKAGED_SIDECAR":"1"}
        process = subprocess.Popen([str(exe), "--port", str(port), "--instance-id", "final-isolated-smoke", "--ownership-token", token],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        def query(path, method="GET"):
            request = Request(f"http://127.0.0.1:{port}{path}", method=method,
                headers={"X-AIMA-Ownership-Token":token}, data=b"" if method=="POST" else None)
            with urlopen(request, timeout=15) as response:
                return json.load(response)
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("新后端启动失败")
                try:
                    query("/health")
                    break
                except Exception:
                    time.sleep(0.5)
            else:
                raise RuntimeError("新后端启动超时")
            analysis = query("/v2/ai-analysis?account_id=smoke")
            assert len(analysis["execution_records"]) == 1
            assert analysis["execution_records"][0]["economic_role"] == "ENTRY"
            assert analysis["style_dna"]["discipline_score"] is None
            assert analysis["execution_scope"]["account_id"] == "smoke"
            refresh = query("/v2/macro-calendar/refresh", "POST")
            workspace = query("/v2/workspace?account_id=smoke")
            assert workspace["macro_calendar"]["actual_supported"] is False
            assert len(query("/v2/ai-analysis?account_id=smoke")["execution_records"]) == 1
            print(json.dumps({"packaged_backend":"PASS", "isolated_data":True,
                "entry_projection":"PASS", "read_does_not_duplicate_fill":"PASS",
                "no_fabricated_score":"PASS", "calendar_api":"PASS",
                "calendar_fetch_status":refresh["status"], "calendar_events":len(workspace["macro_events"]),
                "tested_at":datetime.now(timezone.utc).isoformat()}, ensure_ascii=False))
        finally:
            if process.poll() is None:
                # Exactly the subprocess tree created by this test, never a
                # name-based kill of the user's installed application.
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
                process.wait(timeout=15)


if __name__ == "__main__":
    main()
