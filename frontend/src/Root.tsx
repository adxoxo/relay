// Root: decides between the login screen and the app based on auth state.
// Registers the global 401 handler that flips back to the login screen.

import { useEffect, useState } from "react";
import { me, onUnauthorized } from "./api";
import { App } from "./App";
import { Login } from "./Login";
import { ToastProvider } from "./toast";
import { isPhoneScreen } from "./util";

type AuthState = "checking" | "in" | "out";

export function Root() {
  const [auth, setAuth] = useState<AuthState>("checking");

  useEffect(() => {
    onUnauthorized(() => setAuth("out"));
  }, []);

  useEffect(() => {
    // If a token is in the hash, let the login screen handle it directly.
    if (location.hash.includes("token=")) {
      setAuth("out");
      return;
    }
    me()
      .then((res) => setAuth(res.authenticated ? "in" : "out"))
      .catch(() => setAuth("out"));
  }, []);

  // Mark touch devices so the terminal key bar shows, and phones (physical
  // screen size) for the phone landscape layout.
  useEffect(() => {
    if (matchMedia("(pointer: coarse)").matches || "ontouchstart" in window) {
      document.body.classList.add("touch");
    }
    if (isPhoneScreen()) document.body.classList.add("phone-screen");
  }, []);

  // Mark an open on-screen keyboard. With interactive-widget=resizes-content
  // the keyboard shrinks the layout viewport, so a text field that has focus
  // plus a clearly shorter viewport means the keyboard is up. The open session
  // then hides the header and nav, and the terminal keeps the most rows.
  useEffect(() => {
    if (!document.body.classList.contains("touch")) return;
    // Real device rotation, not the layout viewport: an on-screen keyboard
    // shrinks innerHeight (and can leave it below innerWidth) without
    // rotating the device, which innerWidth > innerHeight cannot tell apart
    // from a real rotation. screen.orientation.type (with screen.width /
    // screen.height as a fallback where it is unavailable) does not change
    // when the keyboard opens or closes.
    const orientation = () => {
      const type = screen.orientation?.type;
      if (type) return type.startsWith("landscape") ? "landscape" : "portrait";
      return screen.width > screen.height ? "landscape" : "portrait";
    };
    let lastOrientation = orientation();
    let fullHeight = innerHeight;
    const editableFocused = () => {
      const el = document.activeElement as HTMLElement | null;
      return !!el && (el.tagName === "TEXTAREA" || el.tagName === "INPUT" || el.isContentEditable);
    };
    const update = () => {
      const now = orientation();
      const height = window.visualViewport?.height ?? innerHeight;
      if (now !== lastOrientation) {
        lastOrientation = now;
        fullHeight = height;
      }
      if (!editableFocused()) fullHeight = Math.max(fullHeight, height);
      // The real visible viewport (behind the on-screen keyboard), for layout
      // that must fit it exactly: 100dvh does not shrink for the keyboard on
      // every browser, so the terminal and composer read this var instead.
      document.documentElement.style.setProperty("--vvh", `${height}px`);
      document.body.classList.toggle("kb-open", editableFocused() && height < fullHeight * 0.8);
    };
    const later = () => window.setTimeout(update, 60);
    update(); // set --vvh before the first keyboard or orientation event
    window.addEventListener("resize", update);
    window.visualViewport?.addEventListener("resize", update);
    document.addEventListener("focusin", later);
    document.addEventListener("focusout", later);
    return () => {
      window.removeEventListener("resize", update);
      window.visualViewport?.removeEventListener("resize", update);
      document.removeEventListener("focusin", later);
      document.removeEventListener("focusout", later);
      document.body.classList.remove("kb-open");
    };
  }, []);

  if (auth === "checking") {
    return (
      <div className="login-shell">
        <div className="login-card">
          <p>Loading…</p>
        </div>
      </div>
    );
  }

  if (auth === "out") {
    return <Login onSuccess={() => setAuth("in")} />;
  }

  return (
    <ToastProvider>
      <App />
    </ToastProvider>
  );
}
