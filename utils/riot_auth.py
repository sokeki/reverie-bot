"""
Riot RSO ("Riot Sign-On") authentication — scripted username/password login,
with cookie-based reauth afterward so the user only has to log in again every
1-3 weeks instead of every ~hour.

Flow:
    1. authorize(username, password)  -> AuthSuccess | raises MFARequired
    2. (if MFARequired) submit_mfa(cookies, code) -> AuthSuccess
    3. Store AuthSuccess.cookies (encrypted!) — this is what makes long-lived
       sessions possible without ever storing the password.
    4. reauth_with_cookies(cookies) -> fresh AuthSuccess, using the stored
       cookies, no password needed. Call this whenever you need a fresh
       access_token — cheap, and doesn't consume the cookie's remaining
       lifetime.
    5. get_entitlement / get_puuid / get_region / get_storefront as needed.

IMPORTANT — this uses a fixed set of request parameters (User-Agent,
client_id, redirect_uri, scope, empty PKCE fields) confirmed against
SkinPeek's current production source, since Riot's login endpoint will throw
a hidden hCaptcha requirement at requests that don't match this shape
closely enough (this is what broke an earlier version of this file that
used a plain "RiotClient/..." User-Agent). If login starts failing again in
the future, these are the values to check against SkinPeek's own repo again.

Riot's login sometimes still blocks automated attempts outright (Cloudflare
IP/hosting-reputation checks) — this can't be fixed with code, only by
running from a different network. AuthenticationError messages try to make
clear when that's what happened vs. genuinely wrong credentials.
"""

import base64
import json
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlparse

import aiohttp

AUTH_BASE = "https://auth.riotgames.com"
ENTITLEMENTS_URL = "https://entitlements.auth.riotgames.com/api/token/v1"
USERINFO_URL = "https://auth.riotgames.com/userinfo"
GEO_URL = "https://riot-geo.pas.si.riotgames.com/pas/v1/product/valorant"

CLIENT_PLATFORM = base64.b64encode(
    json.dumps(
        {
            "platformType": "PC",
            "platformOS": "Windows",
            "platformOSVersion": "10.0.19042.1.256.64bit",
            "platformChipset": "Unknown",
        }
    ).encode()
).decode()

HEADERS = {
    "Content-Type": "application/json",
    # NOT a RiotClient/... UA on purpose — that string currently triggers an
    # hCaptcha requirement on auth.riotgames.com that a plain HTTP client
    # can't solve. This value is what SkinPeek's own codebase switched to for
    # the same reason (see their GitHub issue #93).
    "User-Agent": "ShooterGame/13 Windows/10.0.19043.1.256.64bit",
}

# Riot Client version — fetched dynamically and cached, rather than
# hardcoded, since a hardcoded value silently goes stale and starts causing
# 404s on storefront requests. This community-maintained endpoint tracks
# Riot's current build automatically (same source SkinPeek itself uses).
_client_version_cache: str | None = None


class AuthenticationError(Exception):
    """Raised for bad credentials, rate limiting, or an unrecognized response."""


class MFARequired(Exception):
    """Raised when Riot wants a 2FA code. Carries the cookies needed to continue."""

    def __init__(self, cookies: dict, email_hint: str = ""):
        self.cookies = cookies
        self.email_hint = email_hint
        super().__init__("Multi-factor authentication code required")


@dataclass
class AuthSuccess:
    access_token: str
    id_token: str
    cookies: dict = field(default_factory=dict)
    expires_at: float = 0.0  # unix timestamp, from the access_token's own exp claim


def _decode_jwt_exp(token: str) -> float:
    try:
        payload_b64 = token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        exp = payload.get("exp")
        if exp:
            return float(exp)
    except Exception:
        pass
    return time.time() + 3600


def _cookies_to_dict(session: aiohttp.ClientSession) -> dict:
    jar = session.cookie_jar.filter_cookies("https://auth.riotgames.com")
    return {k: morsel.value for k, morsel in jar.items()}


def _extract_tokens_from_redirect(uri: str) -> tuple[str, str]:
    fragment = urlparse(uri).fragment
    params = dict(parse_qsl(fragment))
    access_token = params.get("access_token")
    id_token = params.get("id_token")
    if not access_token or not id_token:
        raise AuthenticationError("Couldn't parse tokens from Riot's response.")
    return access_token, id_token


def _check_cloudflare_block(response: aiohttp.ClientResponse):
    """A different failure mode from a real auth_failure — no amount of
    request-body tweaking fixes this, only hosting on a different IP does."""
    if response.status == 403 and response.headers.get("X-Frame-Options") == "SAMEORIGIN":
        raise AuthenticationError(
            "Blocked by Riot's Cloudflare firewall (HTTP 403). This usually means "
            "the server's IP address/hosting provider is flagged — try running "
            "this from a residential connection instead of a cloud host to confirm."
        )


def build_login_url(redirect_uri: str, state: str) -> str:
    """Login link for the dashboard auto-capture flow. redirect_uri should
    point at the dashboard's /riot-callback route, with `state` baked into
    its own query string (?state=...) so the query string survives the
    redirect — Riot appends the tokens as a URL *fragment*
    (#access_token=...), which sits after any existing query string and
    never gets sent to any server on its own, the callback page's own JS
    is what reads it and posts it back to us."""
    from urllib.parse import urlencode

    full_redirect = f"{redirect_uri}?{urlencode({'state': state})}"
    params = {
        "redirect_uri": full_redirect,
        "client_id": "riot-client",
        "response_type": "token id_token",
        "scope": "openid link ban lol_region",
        "nonce": "1",
    }
    return f"{AUTH_BASE}/authorize?{urlencode(params)}"


def parse_cookie_string(raw: str) -> dict:
    """Parses a raw 'Cookie:' request-header value (as copied from browser
    DevTools) like 'name1=value1; name2=value2' into a dict. Used for the
    manual/advanced linking path, for users who'd rather do this than the
    normal login flow."""
    cookies = {}
    for part in raw.strip().split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        cookies[name.strip()] = value.strip()
    if "ssid" not in cookies:
        raise AuthenticationError(
            "Couldn't find an 'ssid' cookie in that text — make sure you copied "
            "the entire Cookie header value, not just part of it."
        )
    return cookies


async def authorize(username: str, password: str) -> AuthSuccess:
    """Log in with username + password. Raises MFARequired if a 2FA code is
    needed, in which case call submit_mfa() with the attached cookies."""
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        # Step 1: establish cookies.
        # client_id "riot-client" + redirect_uri "http://localhost/redirect"
        # is the native-client login shape — using the web client_id here
        # instead is one of the things that causes a blanket auth_failure
        # regardless of correct credentials.
        await session.post(
            f"{AUTH_BASE}/api/v1/authorization",
            json={
                "client_id": "riot-client",
                "code_challenge": "",
                "code_challenge_method": "",
                "acr_values": "",
                "claims": "",
                "nonce": "1",
                "redirect_uri": "http://localhost/redirect",
                "response_type": "token id_token",
                "scope": "openid link ban lol_region",
            },
        )

        # Step 2: submit credentials
        async with session.put(
            f"{AUTH_BASE}/api/v1/authorization",
            json={
                "type": "auth",
                "username": username,
                "password": password,
                "remember": True,
                "language": "en_US",
            },
        ) as r:
            _check_cloudflare_block(r)
            data = await r.json()

        cookies = _cookies_to_dict(session)
        return _handle_auth_response(data, cookies)


async def submit_mfa(cookies: dict, code: str) -> AuthSuccess:
    """Complete login after MFARequired was raised."""
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        session.cookie_jar.update_cookies(cookies)
        async with session.put(
            f"{AUTH_BASE}/api/v1/authorization",
            json={"type": "multifactor", "code": code, "rememberDevice": True},
        ) as r:
            _check_cloudflare_block(r)
            data = await r.json()

        new_cookies = _cookies_to_dict(session)
        merged = {**cookies, **new_cookies}
        return _handle_auth_response(data, merged, is_mfa_step=True)


async def reauth_with_cookies(cookies: dict) -> AuthSuccess:
    """Refresh tokens using previously stored cookies — no password needed.
    This is what makes /dailyshop work without re-prompting login every
    time. Eventually the cookie itself expires (Riot-side — roughly 1 week
    if only the ssid cookie was kept, roughly 3 weeks with the full set —
    at which point this raises AuthenticationError and the user needs to
    /linkriot again."""
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        session.cookie_jar.update_cookies(cookies)
        params = {
            "redirect_uri": "https://playvalorant.com/opt_in",
            "client_id": "play-valorant-web-prod",
            "response_type": "token id_token",
            "scope": "account openid",
            "nonce": "1",
        }
        async with session.get(
            f"{AUTH_BASE}/authorize", params=params, allow_redirects=False
        ) as r:
            _check_cloudflare_block(r)
            location = r.headers.get("Location", "")

        if not location or "access_token" not in location:
            raise AuthenticationError(
                "Stored session has expired — please /linkriot again."
            )

        access_token, id_token = _extract_tokens_from_redirect(location)
        new_cookies = _cookies_to_dict(session)
        return AuthSuccess(
            access_token,
            id_token,
            {**cookies, **new_cookies},
            _decode_jwt_exp(access_token),
        )


def _handle_auth_response(
    data: dict, cookies: dict, is_mfa_step: bool = False
) -> AuthSuccess:
    resp_type = data.get("type")

    if resp_type == "response":
        uri = data["response"]["parameters"]["uri"]
        access_token, id_token = _extract_tokens_from_redirect(uri)
        return AuthSuccess(access_token, id_token, cookies, _decode_jwt_exp(access_token))

    if resp_type == "multifactor" and not is_mfa_step:
        email = data.get("multifactor", {}).get("email", "")
        raise MFARequired(cookies, email_hint=email)

    # Not a success — log the raw response so we can see exactly what Riot
    # actually said, rather than guessing at error string names.
    print(f"[RiotAuth] Non-success auth response: {json.dumps(data)}")

    error = data.get("error", "")
    if error in ("auth_failure", "invalid_credentials"):
        raise AuthenticationError("Incorrect username or password.")
    if error == "rate_limited":
        raise AuthenticationError(
            "Riot is rate-limiting login attempts — please wait a few minutes and try again."
        )
    if "captcha" in json.dumps(data).lower():
        raise AuthenticationError(
            "Riot's anti-bot check (CAPTCHA) blocked this login attempt. "
            "This happens occasionally with automated logins — try again in a bit."
        )

    raise AuthenticationError(
        f"Unexpected response from Riot during login (type={resp_type!r}). "
        f"This usually means Riot changed something — the bot owner should check the logs."
    )


async def get_entitlement(access_token: str) -> str:
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        async with session.post(
            ENTITLEMENTS_URL,
            json={},
            headers={"Authorization": f"Bearer {access_token}"},
        ) as r:
            data = await r.json()
    token = data.get("entitlements_token")
    if not token:
        raise AuthenticationError("Couldn't fetch entitlement token.")
    return token


async def get_puuid(access_token: str) -> str:
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        async with session.get(
            USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
        ) as r:
            data = await r.json()
    puuid = data.get("sub")
    if not puuid:
        raise AuthenticationError("Couldn't fetch account PUUID.")
    return puuid


async def get_region(access_token: str, id_token: str) -> str:
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        async with session.put(
            GEO_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={"id_token": id_token},
        ) as r:
            data = await r.json()
    shard = data.get("affinities", {}).get("live")
    if not shard:
        raise AuthenticationError("Couldn't determine account region/shard.")
    return shard


async def _get_client_version() -> str:
    global _client_version_cache
    if _client_version_cache:
        return _client_version_cache
    async with aiohttp.ClientSession() as session:
        async with session.get("https://valorant-api.com/v1/version") as r:
            if r.status != 200:
                raise AuthenticationError(
                    "Couldn't fetch the current Valorant client version "
                    "(valorant-api.com returned a non-200 status)."
                )
            data = await r.json()
    version = data.get("data", {}).get("riotClientVersion")
    if not version:
        raise AuthenticationError(
            "valorant-api.com/v1/version response didn't contain riotClientVersion."
        )
    _client_version_cache = version
    return version


async def get_storefront(
    access_token: str, entitlement: str, puuid: str, shard: str
) -> dict:
    client_version = await _get_client_version()
    url = f"https://pd.{shard}.a.pvp.net/store/v3/storefront/{puuid}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "X-Riot-Entitlements-JWT": entitlement,
        "X-Riot-ClientVersion": client_version,
        "X-Riot-ClientPlatform": CLIENT_PLATFORM,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json={}) as r:
            if r.status != 200:
                global _client_version_cache
                _client_version_cache = None
                raise AuthenticationError(f"Storefront request failed (HTTP {r.status}).")
            return await r.json()