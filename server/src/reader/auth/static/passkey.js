// SRV-AUTH FR-AUTH-14: the bootstrap and sign-in ceremonies for the two stage-1 pages (WEB-UI replaces them).
// FR-AUTH-20: the page states and their fixed messages. The ceremony itself (routes, bodies, headers, WebAuthn calls,
// fragment handling, the navigation on success) is unchanged by the restyle (FR-AUTH-14 amendment, 2026-11-23).
// FR-AUTH-31: the sign-up page (ADR-0031), the sign-in `pending_approval` state and the sign-in `next` allow-list.
// Same-origin script only (no inline code), so a `script-src 'self'` CSP holds.
"use strict";

(() => {
  const kind = document.body.dataset.ceremony;
  const main = document.querySelector("main.auth");
  const status = document.getElementById("status");
  const button = document.getElementById("go");
  const form = document.getElementById("signup-form");
  const handleInput = document.getElementById("handle");
  const nameInput = document.getElementById("display-name");
  const handleHint = handleInput ? handleInput.getAttribute("aria-describedby") : null;

  // The bootstrap token travels in the URL fragment, which the browser never sends to a server. Read it once and
  // clear it at once, so it leaves the address bar and the history entry.
  let token = "";
  if (kind === "bootstrap") {
    token = location.hash.slice(1);
    history.replaceState(null, "", location.pathname);
  }

  // FR-AUTH-20, FR-AUTH-31: every message the page can show. Nothing from a server response or an exception is ever
  // displayed. `field` names the input a state is about: it gets focus and aria-invalid.
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
      message:
        kind === "signup"
          ? "That passkey could not be registered. Try again."
          : "That passkey could not be registered. Try again; if it keeps failing, run `reader bootstrap` again.",
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
    handle_invalid: {
      message: "Use 3–32 lower-case letters, digits and hyphens, starting with a letter.",
      button: "enabled",
      field: "handle",
    },
    display_name_invalid: {
      message: "Enter a display name of up to 64 characters.",
      button: "enabled",
      field: "display-name",
    },
    handle_unavailable: { message: "This handle is not available. Choose another.", button: "enabled", field: "handle" },
    signup_full: {
      message: "Sign-ups are paused until the owner reviews the waiting requests. Try again later.",
      button: "hidden",
    },
    pending: { message: "Account created. The owner must approve it before you can sign in.", button: "hidden" },
    pending_approval: {
      message: "This account is waiting for the owner's approval. Try again later.",
      button: "enabled",
    },
  };

  const fields = { handle: handleInput, "display-name": nameInput };

  /** Show a state. `afterCeremony` moves focus: the state's field, the button when it can be used again, else the
   * status line. */
  const show = (name, afterCeremony = false, message = STATES[name].message) => {
    const state = STATES[name];
    main.dataset.state = name;
    status.textContent = message;
    button.hidden = state.button === "hidden";
    button.disabled = state.button !== "enabled";
    for (const [key, input] of Object.entries(fields)) {
      if (!input) continue;
      input.disabled = name === "waiting";
      if (state.field === key) {
        input.setAttribute("aria-invalid", "true");
        input.setAttribute("aria-describedby", "status");
      } else {
        input.removeAttribute("aria-invalid");
        if (key === "handle" && handleHint) input.setAttribute("aria-describedby", handleHint);
        else input.removeAttribute("aria-describedby");
      }
    }
    if (!afterCeremony) return;
    if (state.field && fields[state.field]) fields[state.field].focus();
    else if (state.button === "enabled") button.focus();
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

  const signUp = async (handle, displayName) => {
    const options = await post("/api/auth/signup/options", { handle, display_name: displayName }, "options");
    const credential = await navigator.credentials.create({
      publicKey: PublicKeyCredential.parseCreationOptionsFromJSON(options),
    });
    await post("/api/auth/signup/verify", credential.toJSON(), "verify");
  };

  const minutes = (retryAfter) => {
    // Whole seconds, at most six digits (FR-AUTH-11's limit is at most an hour); anything else reads "later".
    if (retryAfter === null || !/^\d{1,6}$/.test(retryAfter.trim())) return STATES.rate_limited.message;
    const n = Math.max(1, Math.ceil(Number(retryAfter) / 60));
    return `Too many attempts. Try again in ${n} ${n === 1 ? "minute" : "minutes"}.`;
  };

  /** FR-AUTH-20's and FR-AUTH-31's table: the state for a failure, and its message when the message is computed. */
  const classify = (failure) => {
    if (failure instanceof RequestFailure) {
      const { step, httpStatus, code } = failure;
      if (httpStatus === 0 || httpStatus >= 500) return ["unreachable"];
      if (httpStatus === 429 && code === "rate_limited") return ["rate_limited", minutes(failure.retryAfter)];
      if (httpStatus === 400 && code === "signin_failed" && kind === "signin" && step === "verify") return ["rejected"];
      if (httpStatus === 403 && code === "pending_approval" && kind === "signin" && step === "verify") {
        return ["pending_approval"];
      }
      if (httpStatus === 400 && code === "bootstrap_failed" && kind === "bootstrap") {
        return [step === "options" ? "expired" : "not_registered"];
      }
      if (kind === "signup") {
        if (httpStatus === 409 && code === "handle_unavailable") return ["handle_unavailable"];
        if (httpStatus === 429 && code === "signup_full") return ["signup_full"];
        if (httpStatus === 400 && code === "display_name_invalid" && step === "options") return ["display_name_invalid"];
        if (httpStatus === 400 && code === "signup_failed" && step === "verify") return ["not_registered"];
      }
      return ["failed"];
    }
    if (failure instanceof DOMException && (failure.name === "NotAllowedError" || failure.name === "AbortError")) {
      return ["cancelled"];
    }
    return ["failed"];
  };

  // The sign-up page's own checks (FR-AUTH-25's form rules), before any request.
  const HANDLE = /^[a-z][a-z0-9-]{2,31}$/;
  const handleOk = (h) => HANDLE.test(h) && !h.includes("--") && !h.endsWith("-");

  const run = async () => {
    let handle = "";
    let displayName = "";
    if (kind === "signup") {
      handle = handleInput.value;
      displayName = nameInput.value.trim();
      if (!handleOk(handle)) {
        show("handle_invalid", true);
        return;
      }
      if (displayName === "") {
        show("display_name_invalid", true);
        return;
      }
    }
    show("waiting");
    try {
      if (kind === "bootstrap") await register();
      else if (kind === "signup") await signUp(handle, displayName);
      else await signIn();
    } catch (failure) {
      const [name, message] = classify(failure);
      show(name, true, message ?? STATES[name].message);
      return;
    }
    if (kind === "signup") {
      show("pending", true);
      return;
    }
    show("done");
    // FR-AUTH-31, FR-AUTH-47: the sign-in page compares its raw query with each allow-listed value and navigates to
    // that value's constant, never to the input; the other pages never read the query.
    const target =
      kind !== "signin"
        ? "/"
        : location.search === "?next=/signups"
          ? "/signups"
          : location.search === "?next=/settings"
            ? "/settings"
            : "/";
    location.assign(target);
  };

  const pkc = window.PublicKeyCredential;
  const parse = kind === "signin" ? "parseRequestOptionsFromJSON" : "parseCreationOptionsFromJSON";
  if (kind === "bootstrap" && !token) {
    show("no_token");
    return;
  }
  if (typeof pkc !== "function" || typeof pkc[parse] !== "function") {
    show("unsupported");
    return;
  }
  show("ready");
  if (form) {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      run();
    });
  } else {
    button.addEventListener("click", run);
  }
})();
