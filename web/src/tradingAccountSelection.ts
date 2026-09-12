const KEY = "aima.trading-account";

export function readTradingAccount(): string {
  try {
    return window.sessionStorage.getItem(KEY) ?? "";
  } catch {
    return "";
  }
}

export function rememberTradingAccount(accountId: string): void {
  try {
    if (accountId && accountId.trim()) {
      window.sessionStorage.setItem(KEY, accountId.trim());
    }
  } catch {
    /* session only fallback */
  }
}
