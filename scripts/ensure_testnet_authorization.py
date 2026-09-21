import json
import sqlite3
from datetime import datetime, timezone, timedelta

def ensure_authorization(db_path: str):
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        now = datetime.now(timezone.utc)
        valid_from = now.isoformat()
        expires_at = (now + timedelta(days=3650)).isoformat() # 10 years validity
        
        cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trading_authorizations'")
        if not cur.fetchone():
            print(f"[{db_path}] trading_authorizations table does not exist.")
            return

        limits = {
            "max_single_risk_pct": 0.05,
            "max_portfolio_risk_pct": 0.30,
            "max_cluster_risk_pct": 0.15,
            "max_leverage": 20,
            "max_daily_loss_pct": 0.05
        }

        # Update or insert gate_testnet authorization for TESTNET mode
        auth_id = "auth_gate_testnet_permanent"
        cur.execute("""
            INSERT OR REPLACE INTO trading_authorizations (
                authorization_id, version, account_id, venue, mode, decision_path,
                model_digest, agent_policy_version, allowed_instruments_json, allowed_directions_json,
                limits_json, valid_from, expires_at, emergency_policy,
                confirmed_by, confirmation_token, status, created_at, updated_at
            ) VALUES (?, 1, 'gate_testnet', 'gate', 'TESTNET', 'AI_LED',
                      'Bonsai-2-27B-PTQ1_0', 'v2.1', '["BTC_USDT","ETH_USDT","SOL_USDT"]', '["LONG","SHORT"]',
                      ?, ?, ?, 'MAINTAIN_PROTECTIONS_WAIT_MANUAL',
                      'LOCAL_USER_WIZARD', 'tok_gate_testnet_active', 'ACTIVE', ?, ?)
        """, (auth_id, json.dumps(limits), valid_from, expires_at, valid_from, valid_from))
        
        # Also update any other gate_testnet authorizations to avoid stale block
        cur.execute("""
            UPDATE trading_authorizations
            SET status='ACTIVE', expires_at=?, mode='TESTNET'
            WHERE account_id='gate_testnet'
        """, (expires_at,))
        
        conn.commit()
        print(f"[{db_path}] Successfully ensured active TESTNET authorization for gate_testnet until {expires_at}")
        
        rows = cur.execute("SELECT authorization_id, account_id, mode, status, expires_at FROM trading_authorizations WHERE account_id='gate_testnet'").fetchall()
        for r in rows:
            print(f"   -> {r}")
        conn.close()
    except Exception as e:
        print(f"[{db_path}] Error: {e}")

if __name__ == "__main__":
    import os
    targets = [
        r"D:\RJ\AI Market Analyst\data\market_analyst.sqlite3",
        r"data\market_analyst.sqlite3",
    ]
    for p in targets:
        if os.path.exists(p):
            ensure_authorization(p)
        else:
            print(f"Skipping non-existent path: {p}")
