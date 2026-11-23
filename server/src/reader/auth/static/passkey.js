// SRV-AUTH FR-AUTH-14: the bootstrap and sign-in ceremonies for the two stage-1 pages (WEB-UI replaces them).
// FR-AUTH-20: the page states and their fixed messages. The ceremony itself (routes, bodies, headers, WebAuthn calls,
// fragment handling, the navigation on success) is unchanged by the restyle (FR-AUTH-14 amendment, 2026-11-23).
// Same-origin script only (no inline code), so a `script-src 'self'` CSP holds.
"use strict";

(() => {
  const kind = document.body.dataset.ceremony;
  const main = document.querySelector("main.auth");
  const status = document.getElementById("status");
  const button = document.getElementById("go");

  // The bootstrap token travels in the URL fragment, which the browser never sends to a server. Read it once and
  // clear it at once, so it leaves the address bar and the history entry.
  let token = "";
  if (kind === "bootstrap") {
    token = location.hash.slice(1);
    history.replaceState(null, "", location.pathname);
  }

  // FR-AUTH-20: every message the page can show. Nothing from a server response or an exception is ever displayed.
  const STATES = {
    ready: { message: "", button: "enabled" },
    unsupported: {
      message: "This browser cannot use passkeys here. Use a current Safari, Chrome, Edge or Firefox.",
      button: "hidden",
    },
    no_token: {
      message: "This setup link is incomplete. Run `reader bootstrap` again for a new link.",
      button: "hidden",
    },
    waiting: { message: "Waiting for your passkey…", button: "disabled" },
    cancelled: {
      message: "The passkey request was cancelled or timed out. Try again when you are ready.",
      button: "enabled",
    },
    rejected: {
      message: "That passkey was not accepted here. Try again, or use another passkey saved for this site.",
      button: "enabled",
    },
    expired: {
      message: "This setup link has expired or was already used. Run `reader bootstrap` again for a new link.",
      button: "hidden",
    },
    not_registered: {
      message: "That passkey could not be registered. Try again; if it keeps failing, run `reader bootstrap` again.",
      button: "enabled",
    },
    rate_limited: { message: "Too many attempts. Try again later.", button: "enabled" },
    unreachable: {
      message: "reader-ai could not be reached. Check that it is running, then try again.",
      button: "enabled",
    },
    failed: { message: "Something went wrong. Try again.", button: "enabled" },
    done: {
      message: kind === "bootstrap" ? "Passkey created. Opening reader-ai…" : "Signed in. Opening reader-ai…",
      button: "disabled",
    },
  };

  /** Show a state. `afterCeremony` moves focus: the button when it can be used again, else the status line. */
  const show = (name, afterCeremony = false, message = STATES[name].message) => {
    const state = STATES[name];
    main.dataset.state = name;
    status.textContent = message;
    button.hidden = state.button === "hidden";
    button.disabled = state.button !== "enabled";
    if (!afterCeremony) return;
    if (state.button === "enabled") button.focus();
    else if (state.button === "hidden") status.focus();
  };

  /** A failed request: its HTTP status (0 without a response), the server's error code and Retry-After. */
  class RequestFailure {
    constructor(step, httpStatus, code, retryAfter) {
      this.step = step;
      this.httpStatus = httpStatus;
      this.code = code;
      this.retryAfter = retryAfter;
    }
  }

  const post = async (path, body, step) => {
    let response;
    try {
      response = await fetch(path, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
    } catch {
      throw new RequestFailure(step, 0, "", null);
    }
    if (!response.ok) {
      const error = await response.json().catch(() => null);
      const code = error && typeof error.error === "string" ? error.error : "";
      throw new RequestFailure(step, response.status, code, response.headers.get("Retry-After"));
    }
    return response.json();
  };

  const register = async () => {
    const options = await post("/api/auth/bootstrap/options", { token }, "options");
    const credential = await navigator.credentials.create({
      publicKey: PublicKeyCredential.parseCreationOptionsFromJSON(options),
    });
    await post("/api/auth/bootstrap/verify", credential.toJSON(), "verify");
  };

  const signIn = async () => {
    const options = await post("/api/auth/signin/options", {}, "options");
    const credential = await navigator.credentials.get({
      publicKey: PublicKeyCredential.parseRequestOptionsFromJSON(options),
    });
    await post("/api/auth/signin/verify", credential.toJSON(), "verify");
  };

  const minutes = (retryAfter) => {
    if (retryAfter === null || !/^\d+$/.test(retryAfter.trim())) return STATES.rate_limited.message;
    const n = Math.max(1, Math.ceil(Number(retryAfter) / 60));
    return `Too many attempts. Try again in ${n} ${n === 1 ? "minute" : "minutes"}.`;
  };

  /** FR-AUTH-20's table: the state for a failure, and its message when the message is computed. */
  const classify = (failure) => {
    if (failure instanceof RequestFailure) {
      const { step, httpStatus, code } = failure;
      if (httpStatus === 0 || httpStatus >= 500) return ["unreachable"];
      if (httpStatus === 429 && code === "rate_limited") return ["rate_limited", minutes(failure.retryAfter)];
      if (httpStatus === 400 && code === "signin_failed" && kind === "signin" && step === "verify") return ["rejected"];
      if (httpStatus === 400 && code === "bootstrap_failed" && kind === "bootstrap") {
        return [step === "options" ? "expired" : "not_registered"];
      }
      return ["failed"];
    }
    if (failure instanceof DOMException && (failure.name === "NotAllowedError" || failure.name === "AbortError")) {
      return ["cancelled"];
    }
    return ["failed"];
  };

  const run = async () => {
    show("waiting");
    try {
      await (kind === "bootstrap" ? register() : signIn());
    } catch (failure) {
      const [name, message] = classify(failure);
      show(name, true, message ?? STATES[name].message);
      return;
    }
    show("done");
    location.assign("/");
  };

  const pkc = window.PublicKeyCredential;
  const parse = kind === "bootstrap" ? "parseCreationOptionsFromJSON" : "parseRequestOptionsFromJSON";
  if (kind === "bootstrap" && !token) {
    show("no_token");
    return;
  }
  if (typeof pkc !== "function" || typeof pkc[parse] !== "function") {
    show("unsupported");
    return;
  }
  show("ready");
  button.addEventListener("click", run);
})();
