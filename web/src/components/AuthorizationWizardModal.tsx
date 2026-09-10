import React, { useEffect, useState } from 'react';
import { apiClient } from '../api/client';

export interface AuthorizationWizardModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSuccess: (authorization: AuthorizationRecord) => void;
  accountId?: string;
  mode?: 'PAPER' | 'TESTNET' | 'LIVE';
  venue?: string;
}

export interface AuthorizationRecord {
  authorization_id: string;
  status?: string;
  expires_at?: string;
}

export const AuthorizationWizardModal: React.FC<AuthorizationWizardModalProps> = ({
  isOpen,
  onClose,
  onSuccess,
  accountId: initialAccountId,
  mode: initialMode,
  venue: initialVenue,
}) => {
  const [accountId, setAccountId] = useState(initialAccountId || '');
  const [venue, setVenue] = useState(initialVenue || (initialMode === 'PAPER' ? 'simulated' : 'gate'));
  const [mode, setMode] = useState<'PAPER' | 'TESTNET' | 'LIVE'>(initialMode || 'PAPER');
  const [decisionPath, setDecisionPath] = useState<'AI_LED' | 'STRATEGY_DRIVEN'>('AI_LED');
  const [allowedInstruments, setAllowedInstruments] = useState('BTC_USDT');
  const [allowLong, setAllowLong] = useState(true);
  const [allowShort, setAllowShort] = useState(true);
  const [maxRiskFraction, setMaxRiskFraction] = useState(0.01);
  const [maxLeverage, setMaxLeverage] = useState(3);
  const dailyLossLimit = 0.03;
  const [durationHours, setDurationHours] = useState(4);
  const [userConfirmed, setUserConfirmed] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  useEffect(() => {
    if (!isOpen) return;
    setAccountId(initialAccountId || '');
    setMode(initialMode || 'PAPER');
    setVenue(initialVenue || (initialMode === 'PAPER' ? 'simulated' : 'gate'));
    setErrorMessage(null);
    setUserConfirmed(false);
  }, [isOpen, initialAccountId, initialMode, initialVenue]);

  if (!isOpen) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!accountId.trim()) {
      setErrorMessage('A registered account is required before granting authorization.');
      return;
    }
    if (!userConfirmed) {
      setErrorMessage('You must explicitly verify local authorization.');
      return;
    }

    setIsSubmitting(true);
    setErrorMessage(null);

    const sides: string[] = [];
    if (allowLong) sides.push('LONG');
    if (allowShort) sides.push('SHORT');

    const payload = {
      account_id: accountId,
      venue,
      mode,
      decision_path: decisionPath,
      allowed_instruments: allowedInstruments.split(',').map((s) => s.trim()).filter(Boolean),
      allowed_sides: sides,
      max_risk_fraction: maxRiskFraction,
      max_leverage: maxLeverage,
      daily_loss_limit_fraction: dailyLossLimit,
      duration_seconds: durationHours * 3600,
      confirmed_by: 'LOCAL_USER_WIZARD',
    };

    try {
      const created = await apiClient.v2<AuthorizationRecord>('/trading-authorizations', 'POST', payload);
      onSuccess(created);
      onClose();
    } catch (err: unknown) {
      setErrorMessage(err instanceof Error ? err.message || 'Network error' : 'Network error');
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <LocalizedSurface><div
      style={{
        position: 'fixed',
        top: 0,
        left: 0,
        right: 0,
        bottom: 0,
        backgroundColor: 'rgba(0, 0, 0, 0.75)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 1000,
        backdropFilter: 'blur(4px)',
      }}
    >
      <div
        style={{
          backgroundColor: '#161b22',
          border: '1px solid #30363d',
          borderRadius: 8,
          width: '560px',
          maxWidth: '90vw',
          padding: '24px',
          color: '#c9d1d9',
          boxShadow: '0 8px 24px rgba(0,0,0,0.5)',
        }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
          <h3 style={{ margin: 0, color: '#58a6ff' }}>
            🛡️ Trading Authorization Wizard (N09)
          </h3>
          <button
            onClick={onClose}
            style={{
              background: 'none',
              border: 'none',
              color: '#8b949e',
              cursor: 'pointer',
              fontSize: 18,
            }}
          >
            ✕
          </button>
        </div>

        {errorMessage && (
          <div
            style={{
              padding: '10px 14px',
              backgroundColor: 'rgba(248, 81, 73, 0.15)',
              border: '1px solid #f85149',
              borderRadius: 6,
              color: '#f85149',
              marginBottom: 16,
              fontSize: 13,
            }}
          >
            {errorMessage}
          </div>
        )}

        <form onSubmit={handleSubmit} style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
            <div>
              <label style={{ fontSize: 12, color: '#8b949e' }}>Account ID</label>
              <input
                type="text"
                value={accountId}
                onChange={(e) => setAccountId(e.target.value)}
                readOnly={Boolean(initialAccountId)}
                required
                style={{
                  width: '100%',
                  backgroundColor: '#0d1117',
                  border: '1px solid #30363d',
                  borderRadius: 4,
                  padding: '6px 8px',
                  color: '#c9d1d9',
                  marginTop: 4,
                }}
              />
            </div>
            <div>
              <label style={{ fontSize: 12, color: '#8b949e' }}>Target Mode</label>
              <select
                value={mode}
                onChange={(e) => {
                  const nextMode = e.target.value as 'PAPER' | 'TESTNET' | 'LIVE';
                  setMode(nextMode);
                  if (!initialVenue) setVenue(nextMode === 'PAPER' ? 'simulated' : 'gate');
                }}
                disabled={Boolean(initialAccountId)}
                style={{
                  width: '100%',
                  backgroundColor: '#0d1117',
                  border: '1px solid #30363d',
                  borderRadius: 4,
                  padding: '6px 8px',
                  color: '#c9d1d9',
                  marginTop: 4,
                }}
              >
                <option value="PAPER">PAPER (Simulation)</option>
                <option value="TESTNET">TESTNET (Sandbox Venue)</option>
                <option value="LIVE" disabled>LIVE (Real Capital - Strict Lock)</option>
              </select>
            </div>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
            <div>
              <label style={{ fontSize: 12, color: '#8b949e' }}>Decision Path</label>
              <select
                value={decisionPath}
                onChange={(e) => {
                  const nextPath = e.target.value;
                  if (nextPath === 'AI_LED' || nextPath === 'STRATEGY_DRIVEN') {
                    setDecisionPath(nextPath);
                  }
                }}
                style={{
                  width: '100%',
                  backgroundColor: '#0d1117',
                  border: '1px solid #30363d',
                  borderRadius: 4,
                  padding: '6px 8px',
                  color: '#c9d1d9',
                  marginTop: 4,
                }}
              >
                <option value="AI_LED">AI_LED (Autonomous 9B Decisions)</option>
                <option value="STRATEGY_DRIVEN">STRATEGY_DRIVEN (Quant Rules + Review)</option>
              </select>
            </div>
            <div>
              <label style={{ fontSize: 12, color: '#8b949e' }}>Allowed Instruments</label>
              <input
                type="text"
                value={allowedInstruments}
                onChange={(e) => setAllowedInstruments(e.target.value)}
                placeholder="BTC_USDT, ETH_USDT"
                style={{
                  width: '100%',
                  backgroundColor: '#0d1117',
                  border: '1px solid #30363d',
                  borderRadius: 4,
                  padding: '6px 8px',
                  color: '#c9d1d9',
                  marginTop: 4,
                }}
              />
            </div>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 12 }}>
            <div>
              <label style={{ fontSize: 12, color: '#8b949e' }}>Max Risk / Trade</label>
              <input
                type="number"
                step="0.0025"
                min="0.001"
                max="0.05"
                value={maxRiskFraction}
                onChange={(e) => setMaxRiskFraction(parseFloat(e.target.value))}
                style={{
                  width: '100%',
                  backgroundColor: '#0d1117',
                  border: '1px solid #30363d',
                  borderRadius: 4,
                  padding: '6px 8px',
                  color: '#c9d1d9',
                  marginTop: 4,
                }}
              />
            </div>
            <div>
              <label style={{ fontSize: 12, color: '#8b949e' }}>Max Leverage</label>
              <input
                type="number"
                min="1"
                max="20"
                value={maxLeverage}
                onChange={(e) => setMaxLeverage(parseInt(e.target.value, 10))}
                style={{
                  width: '100%',
                  backgroundColor: '#0d1117',
                  border: '1px solid #30363d',
                  borderRadius: 4,
                  padding: '6px 8px',
                  color: '#c9d1d9',
                  marginTop: 4,
                }}
              />
            </div>
            <div>
              <label style={{ fontSize: 12, color: '#8b949e' }}>Validity Duration</label>
              <select
                value={durationHours}
                onChange={(e) => setDurationHours(parseInt(e.target.value, 10))}
                style={{
                  width: '100%',
                  backgroundColor: '#0d1117',
                  border: '1px solid #30363d',
                  borderRadius: 4,
                  padding: '6px 8px',
                  color: '#c9d1d9',
                  marginTop: 4,
                }}
              >
                <option value={1}>1 Hour</option>
                <option value={4}>4 Hours</option>
                <option value={12}>12 Hours</option>
                <option value={24}>24 Hours</option>
              </select>
            </div>
          </div>

          <div style={{ display: 'flex', gap: 16, marginTop: 4 }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 13, cursor: 'pointer' }}>
              <input
                type="checkbox"
                checked={allowLong}
                onChange={(e) => setAllowLong(e.target.checked)}
              />
              Allow Long Positions
            </label>
            <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 13, cursor: 'pointer' }}>
              <input
                type="checkbox"
                checked={allowShort}
                onChange={(e) => setAllowShort(e.target.checked)}
              />
              Allow Short Positions
            </label>
          </div>

          <div
            style={{
              padding: '10px 12px',
              backgroundColor: '#21262d',
              border: '1px solid #30363d',
              borderRadius: 6,
              fontSize: 12,
              marginTop: 6,
            }}
          >
            <label style={{ display: 'flex', alignItems: 'flex-start', gap: 8, cursor: 'pointer' }}>
              <input
                type="checkbox"
                checked={userConfirmed}
                onChange={(e) => setUserConfirmed(e.target.checked)}
                style={{ marginTop: 2 }}
              />
              <span>
                I explicitly grant local authorization for AI to place and manage orders within these exact limits.
                AI will not ask for per-order confirmation. Expiry or revocation will freeze new risk while preserving protective stop orders.
              </span>
            </label>
          </div>

          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 12 }}>
            <button
              type="button"
              onClick={onClose}
              style={{
                padding: '8px 16px',
                backgroundColor: '#21262d',
                border: '1px solid #30363d',
                borderRadius: 6,
                color: '#c9d1d9',
                cursor: 'pointer',
              }}
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={isSubmitting || !userConfirmed}
              style={{
                padding: '8px 16px',
                backgroundColor: userConfirmed ? '#238636' : '#23863655',
                border: '1px solid rgba(240, 246, 252, 0.1)',
                borderRadius: 6,
                color: '#ffffff',
                fontWeight: 600,
                cursor: userConfirmed ? 'pointer' : 'not-allowed',
              }}
            >
              {isSubmitting ? 'Granting...' : 'Grant Scope Authorization'}
            </button>
          </div>
        </form>
      </div>
    </div></LocalizedSurface>
  );
};
import { LocalizedSurface } from '../i18n';
