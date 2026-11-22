// SRV-AUTH FR-AUTH-14: the bootstrap and sign-in ceremonies for the two stage-1 pages (WEB-UI replaces them).
// Same-origin script only (no inline code), so a `script-src 'self'` CSP holds.
"use strict";

(() => {
  const kind = document.body.dataset.ceremony;
  const status = document.getElementById("status");
  const button = document.getElementById("go");

  // The bootstrap token travels in the URL fragment, which the browser never sends to a server. Read it once and
  // clear it at once, so it leaves the address bar and the history entry.
  let token = "";
  if (kind === "bootstrap") {
    token = location.hash.slice(1);
    history.replaceState(null, "", location.pathname);
  }

  const post = async (path, body) => {
    const response = await fetch(path, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) throw new Error(`request failed (${response.status})`);
    return response.json();
  };

  const register = async () => {
    const options = await post("/api/auth/bootstrap/options", { token });
    const credential = await navigator.credentials.create({
      publicKey: PublicKeyCredential.parseCreationOptionsFromJSON(options),
    });
    await post("/api/auth/bootstrap/verify", credential.toJSON());
  };

  const signIn = async () => {
    const options = await post("/api/auth/signin/options", {});
    const credential = await navigator.credentials.get({
      publicKey: PublicKeyCredential.parseRequestOptionsFromJSON(options),
    });
    await post("/api/auth/signin/verify", credential.toJSON());
  };

  const run = async () => {
    button.disabled = true;
    status.textContent = "Waiting for your passkey…";
    try {
      await (kind === "bootstrap" ? register() : signIn());
      location.assign("/");
    } catch (failure) {
      status.textContent = `Did not work: ${failure.message}. Try again.`;
      button.disabled = false;
    }
  };

  if (kind === "bootstrap" && !token) {
    status.textContent = "This link has no token. Run `reader bootstrap` again.";
    return;
  }
  status.textContent = "";
  button.hidden = false;
  button.addEventListener("click", run);
})();
