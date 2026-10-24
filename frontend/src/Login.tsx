// Login screen. Token field with the copy from the brief.
// If location.hash carries token=..., log in automatically and clear the fragment.

import { useEffect, useRef, useState } from "react";
import { ApiError, login } from "./api";
import { ActivityIcon } from "./icons";

interface LoginProps {
  onSuccess: () => void;
}

function tokenFromHash(): string | null {
  const hash = location.hash.startsWith("#") ? location.hash.slice(1) : location.hash;
  if (!hash) return null;
  const params = new URLSearchParams(hash);
  const token = params.get("token");
  return token && token.trim() ? token.trim() : null;
}

export function Login({ onSuccess }: LoginProps) {
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const autoTried = useRef(false);

  const submit = async (value: string) => {
    setBusy(true);
    setError(null);
    try {
      await login(value);
      onSuccess();
    } catch (err) {
      if (err instanceof ApiError) {
        setError(
          err.code === "rate_limited"
            ? "Too many attempts. Wait a moment and try again."
            : "That token was not accepted.",
        );
      } else {
        setError("Cannot reach relay.");
      }
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    if (autoTried.current) return;
    autoTried.current = true;
    const fromHash = tokenFromHash();
    if (fromHash) {
      history.replaceState(null, "", location.pathname + location.search);
      void submit(fromHash);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="login-shell">
      <div className="login-card">
        <div className="login-logo" aria-hidden="true">
          <ActivityIcon style={{ width: 26, height: 26 }} />
        </div>
        <h1>relay</h1>
        <p>
          Paste the token from <code>relay token</code> on the laptop.
        </p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (token.trim() && !busy) void submit(token.trim());
          }}
        >
          <label htmlFor="login-token">Access token</label>
          <input
            id="login-token"
            type="password"
            autoComplete="off"
            autoCapitalize="off"
            spellCheck={false}
            value={token}
            onChange={(e) => {
              setToken(e.target.value);
              setError(null);
            }}
            placeholder="relay token"
          />
          {error ? (
            <p className="login-error" role="alert">
              {error}
            </p>
          ) : null}
          <button className="button primary" type="submit" disabled={busy || !token.trim()}>
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </div>
    </div>
  );
}
