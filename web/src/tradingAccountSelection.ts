const KEY = "aima.trading-account";
export function readTradingAccount(): string {
  try { return window.sessionStorage.getItem(KEY) ?? ""; } catch { return ""; }
}
export function rememberTradingAccount(accountId: string): void {
  try { window.sessionStorage.setItem(KEY, accountId); } catch { /* session only fallback */ }
}
