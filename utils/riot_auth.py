"""
Riot RSO ("Riot Sign-On") authentication for the unofficial Valorant client
API. This lets us fetch a *specific user's* personal daily shop, which is
private data — unlike the public Henrik API used elsewhere in this bot, this
needs the user's own Riot account session.

Flow, in order:
    1. authorize(username, password)  -> AuthSuccess | MFARequired
    2. (if MFARequired) submit_mfa(cookies, code) -> AuthSuccess
    3. get_entitlement(access_token)   -> entitlement token
    4. get_puuid(access_token)         -> the account's PUUID
    5. get_region(access_token, id_token) -> shard (e.g. "eu", "na")
    6. get_storefront(...)             -> today's shop (raw skin-level UUIDs)

Cookies returned in AuthSuccess.cookies should be stored (encrypted) so we
can call reauth_with_cookies() later without ever storing the password —
the password itself is used once and discarded.

IMPORTANT CAVEATS (be upfront about these with users):
  - Riot's login endpoint occasionally throws up a CAPTCHA / anti-bot check
    for automated logins. When this happens AuthenticationError is raised
    with a message telling the user to try again shortly, there's no
    reliable server-side bypass for this.
  - This uses an unofficial, reverse-engineered API. It could break without
    warning if Riot changes their auth flow.
  - Storing session cookies is still storing something that grants access
    to someone's Valorant account. Treat it as sensitive as a password.
"""

import base64
import json
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

# Riot Client version — this drifts over time. If storefront requests start
# failing with 400s, this is the first thing to check/update.
CLIENT_VERSION = "release-11.06-shipping-15-3831369"

HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "RiotClient/60.0.6.4770705.4749685 rso-auth (Windows;10;;Professional, x64)",
}


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


def _cookies_to_dict(session: aiohttp.ClientSession) -> dict:
    jar = session.cookie_jar.filter_cookies("https://auth.riotgames.com")
    return {k: morsel.value for k, morsel in jar.items()}


def _extract_tokens_from_redirect(uri: str) -> tuple[str, str]:
    """Riot returns the tokens in the URL fragment of a redirect_uri, e.g.
    https://playvalorant.com/opt_in#access_token=...&id_token=...&..."""
    fragment = urlparse(uri).fragment
    params = dict(parse_qsl(fragment))
    access_token = params.get("access_token")
    id_token = params.get("id_token")
    if not access_token or not id_token:
        raise AuthenticationError("Couldn't parse tokens from Riot's response.")
    return access_token, id_token


async def authorize(username: str, password: str) -> AuthSuccess:
    """Log in with username + password. Raises MFARequired if a 2FA code is
    needed, in which case call submit_mfa() with the attached cookies."""
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        # Step 1: establish cookies
        await session.post(
            f"{AUTH_BASE}/api/v1/authorization",
            json={
                "client_id": "play-valorant-web-prod",
                "nonce": "1",
                "redirect_uri": "https://playvalorant.com/opt_in",
                "response_type": "token id_token",
                "scope": "account openid",
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
            data = await r.json()

        new_cookies = _cookies_to_dict(session)
        # Keep whichever cookies we still have if the response didn't reissue them
        merged = {**cookies, **new_cookies}
        return _handle_auth_response(data, merged, is_mfa_step=True)


async def reauth_with_cookies(cookies: dict) -> AuthSuccess:
    """Refresh tokens using a previously stored session cookie — no password
    needed. This is what makes recurring /dailyshop checks possible without
    re-asking for credentials every time. Eventually the cookie itself
    expires (Riot-side, typically some weeks), at which point this will
    fail and the user needs to /linkriot again."""
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
            location = r.headers.get("Location", "")

        if not location or "access_token" not in location:
            raise AuthenticationError(
                "Stored session has expired — please /linkriot again."
            )

        access_token, id_token = _extract_tokens_from_redirect(location)
        new_cookies = _cookies_to_dict(session)
        return AuthSuccess(access_token, id_token, {**cookies, **new_cookies})


def _handle_auth_response(
    data: dict, cookies: dict, is_mfa_step: bool = False
) -> AuthSuccess:
    resp_type = data.get("type")

    if resp_type == "response":
        uri = data["response"]["parameters"]["uri"]
        access_token, id_token = _extract_tokens_from_redirect(uri)
        return AuthSuccess(access_token, id_token, cookies)

    if resp_type == "multifactor" and not is_mfa_step:
        email = data.get("multifactor", {}).get("email", "")
        raise MFARequired(cookies, email_hint=email)

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


async def get_storefront(
    access_token: str, entitlement: str, puuid: str, shard: str
) -> dict:
    url = f"https://pd.{shard}.a.pvp.net/store/v3/storefront/{puuid}"
    headers = {
        **HEADERS,
        "Authorization": f"Bearer {access_token}",
        "X-Riot-Entitlements-JWT": entitlement,
        "X-Riot-ClientVersion": CLIENT_VERSION,
        "X-Riot-ClientPlatform": CLIENT_PLATFORM,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json={}) as r:
            if r.status != 200:
                raise AuthenticationError(
                    f"Storefront request failed (HTTP {r.status}). "
                    f"If this persists, CLIENT_VERSION in riot_auth.py may need updating."
                )
            return await r.json()