"""SRV-AUTH pages: assets, CSP-safe markup and tokens copied from the web app (V.auth-page-style).

@trace FR-AUTH-14 FR-AUTH-19 FR-AUTH-20 FR-AUTH-21
"""

from __future__ import annotations

import re
import tarfile
from pathlib import Path
from typing import Any

import pytest
from auth_helpers import client, ready_engine, settings_for
from conftest import Pg

pytestmark = pytest.mark.anyio

REPO = Path(__file__).resolve().parents[3]
STATIC = REPO / "server" / "src" / "reader" / "auth" / "static"
PAGES = ["/api/auth/bootstrap", "/api/auth/signin"]
ASSETS = {
    "/api/auth/bootstrap": "text/html",
    "/api/auth/signin": "text/html",
    "/api/auth/static/passkey.js": "text/javascript",
    "/api/auth/static/auth.css": "text/css",
}
# The edge CSP at 2924dcf (OPS-DEPLOY's deploy/Caddyfile); FR-AUTH-21 forbids changing it for the auth pages.
PINNED_CSP = (
    "default-src 'self'; img-src 'self' blob:; connect-src 'self' wss://localhost; frame-src 'none'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


async def _setup(pg: Pg, tmp_path: Path) -> tuple[Any, Any]:
    settings = settings_for(tmp_path)
    return settings, await ready_engine(pg, settings)


# FR-AUTH-21 ---------------------------------------------------------------------------------------------


async def test_static_routes_types_and_cache(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    async with client(settings, engine) as c:
        for path, media in ASSETS.items():
            r = await c.get(path)
            assert r.status_code == 200, path
            assert r.headers["content-type"].split(";")[0].strip() == media, path
            assert r.headers.get("cache-control") == "no-cache", path
    await engine.dispose()


async def test_pages_link_one_stylesheet_and_carry_no_inline_style_or_script(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    async with client(settings, engine) as c:
        for page in PAGES:
            html = (await c.get(page)).text
            links = re.findall(r"<link\b[^>]*>", html, re.I)
            sheets = [link for link in links if re.search(r'rel="stylesheet"', link)]
            assert len(sheets) == 1 and 'href="/api/auth/static/auth.css"' in sheets[0], page
            assert html.index("/api/auth/static/auth.css") < html.index("/api/auth/static/passkey.js"), page
            assert not re.search(r"<style\b", html, re.I), page
            assert not re.search(r"\sstyle\s*=", html, re.I), page
            assert not re.search(r"\son[a-z]+\s*=", html, re.I), page
            assert re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S | re.I) == [""], page
            assert not re.search(r"(src|href)=\"(https?:)?//", html, re.I), page
            svg = re.findall(r"<svg\b[^>]*>", html, re.I)
            assert len(svg) == 1 and 'aria-hidden="true"' in svg[0] and 'focusable="false"' in svg[0], page
    await engine.dispose()


def test_stylesheet_imports_nothing() -> None:
    css = (STATIC / "auth.css").read_text("utf-8")
    assert "@import" not in css
    assert "url(" not in css


def test_edge_csp_is_unchanged() -> None:
    caddyfile = (REPO / "deploy" / "Caddyfile").read_text("utf-8")
    found = re.findall(r'Content-Security-Policy\s+"([^"]+)"', caddyfile)
    assert found == [PINNED_CSP]


# FR-AUTH-19 ---------------------------------------------------------------------------------------------


def _props(block: str) -> dict[str, str]:
    found = re.finditer(r"(--acme-[a-z0-9-]+)\s*:\s*([^;]+);", block)
    return {m.group(1): " ".join(m.group(2).split()) for m in found}


def _block(css: str, start: str) -> str:
    """The body of the first `{…}` block that follows `start` (one nesting level for @media)."""
    i = css.index(start)
    j = css.index("{", i)
    depth, k = 0, j
    while True:
        if css[k] == "{":
            depth += 1
        elif css[k] == "}":
            depth -= 1
            if depth == 0:
                return css[j + 1 : k]
        k += 1


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _library_tokens() -> dict[str, str]:
    with tarfile.open(REPO / "web" / "vendor" / "acme-ui-v0.1.0.tgz") as tar:
        member = tar.extractfile("package/dist/styles.css")
        assert member is not None
        css = _strip_comments(member.read().decode("utf-8"))
    return _props(_block(_block(css, "@layer acme.tokens"), ":root"))


def test_tokens_equal_the_web_apps() -> None:
    auth = _strip_comments((STATIC / "auth.css").read_text("utf-8"))
    calm = _strip_comments((REPO / "web" / "src" / "ui" / "tokens" / "calm.css").read_text("utf-8"))
    dark, light = _props(_block(auth, ":root")), _props(_block(auth, "@media (prefers-color-scheme: light)"))
    calm_dark = _props(_block(calm, ":root"))
    calm_light = _props(_block(calm, "@media (prefers-color-scheme: light)"))
    library = _library_tokens()
    assert dark, "auth.css declares tokens"
    used = set(re.findall(r"var\((--acme-[a-z0-9-]+)\)", auth))
    assert used <= set(dark), f"every used token is declared: {sorted(used - set(dark))}"
    for name, value in dark.items():
        source = calm_dark.get(name, library.get(name))
        assert source is not None, f"{name} exists in calm.css or the library"
        assert value == source, f"{name}: {value} != {source}"
    assert light, "auth.css has a light scheme"
    for name, value in light.items():
        assert value == calm_light.get(name), f"light {name}: {value} != {calm_light.get(name)}"
    assert set(light) == {n for n in dark if n in calm_dark}, "the light block overrides every colour token"


# FR-AUTH-20 ---------------------------------------------------------------------------------------------


def test_script_maps_every_state_from_a_fixed_table() -> None:
    js = (STATIC / "passkey.js").read_text("utf-8")
    for state in [
        "ready",
        "unsupported",
        "no_token",
        "waiting",
        "cancelled",
        "known",
        "rejected",
        "expired",
        "rate_limited",
        "unreachable",
        "failed",
        "done",
    ]:
        assert re.search(rf"\b{state}\s*:", js), state
    assert "failure.message" not in js, "no exception text reaches the page"
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js
