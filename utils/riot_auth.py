"""
Riot account linking via the browser-redirect flow — the user logs in on
Riot's own real login page in their own browser, then pastes back the
resulting (broken-looking) redirect URL, which has the tokens sitting in it.

This replaced an earlier version that POSTed the username/password directly
from the bot's server. That approach hit Riot's hCaptcha/Cloudflare
anti-automation wall reliably, since it looks exactly like what it is: a
script logging in, not a person. This version never sends credentials
anywhere from the bot at all — it can't be told apart from a normal login
because it IS a normal login, just in the user's own browser.

Trade-off: because the bot never sees the browser's session cookie (only the
final URL the user copies), there's no silent background reauth. The access
token is only good for about an hour; once it expires the user needs to
click the login link and paste the URL again. That's an intentional
trade-off for not touching credentials or long-lived session cookies at all.
"""

import base64
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlparse

import aiohttp

AUTH_BASE = "https://auth.riotgames.com"
ENTITLEMENTS_URL = "https://entitlements.auth.riotgames.com/api/token/v1"
USERINFO_URL = "https://auth.riotgames.com/userinfo"
GEO_URL = "https://riot-geo.pas.si.riotgames.com/pas/v1/product/valorant"

# Deliberately a non-existent local address. Riot still redirects here after
# a successful login; since nothing's listening, the browser shows a "can't
# reach this page" error — but the tokens are already sitting in the address
# bar's URL for the user to copy. Using a real page (e.g. playvalorant.com)
# instead would risk that page's own JS consuming/hiding the token first.
LOGIN_REDIRECT_URI = "http://localhost/redirect"

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

# Riot Client version — fetched dynamically and cached, rather than
# hardcoded, since a hardcoded value silently goes stale and starts causing
# 404s on storefront requests. This community-maintained endpoint tracks
# Riot's current build automatically (same source SkinPeek itself uses).
_client_version_cache: str | None = None


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


class AuthenticationError(Exception):
    """Raised when tokens can't be parsed or a Riot API call fails."""


@dataclass
class AuthSuccess:
    access_token: str
    id_token: str
    expires_at: float  # unix timestamp


def build_login_url() -> str:
    """The link the user opens in their own browser to log in normally."""
    params = {
        "redirect_uri": LOGIN_REDIRECT_URI,
        "client_id": "riot-client",
        "response_type": "token id_token",
        "scope": "openid link ban lol_region",
        "nonce": "1",
    }
    return f"{AUTH_BASE}/authorize?{urlencode(params)}"


def _decode_jwt_exp(token: str) -> float:
    """Best-effort read of a JWT's exp claim, without verifying signature —
    we're just reading our own token's stated lifetime, not trusting a
    third party's, so this is fine."""
    try:
        payload_b64 = token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        exp = payload.get("exp")
        if exp:
            return float(exp)
    except Exception:
        pass
    return time.time() + 3600  # fallback: assume 1 hour


def redeem_redirect_url(url: str) -> AuthSuccess:
    """Parse the tokens out of the URL the user pastes back. No network
    call — the tokens are already right there in the URL fragment."""
    fragment = urlparse(url.strip()).fragment
    params = dict(parse_qsl(fragment))
    access_token = params.get("access_token")
    id_token = params.get("id_token")
    if not access_token or not id_token:
        raise AuthenticationError(
            "Couldn't find login tokens in that URL. Make sure you copied the "
            "*entire* address bar contents after the error page loaded, "
            "including everything after the #."
        )
    return AuthSuccess(access_token, id_token, _decode_jwt_exp(access_token))


async def get_entitlement(access_token: str) -> str:
    async with aiohttp.ClientSession() as session:
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
    async with aiohttp.ClientSession() as session:
        async with session.get(
            USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
        ) as r:
            data = await r.json()
    puuid = data.get("sub")
    if not puuid:
        raise AuthenticationError("Couldn't fetch account PUUID.")
    return puuid


async def get_region(access_token: str, id_token: str) -> str:
    async with aiohttp.ClientSession() as session:
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
                # Client version might have rotated mid-flight — clear the
                # cache so the next attempt re-fetches a fresh one.
                global _client_version_cache
                _client_version_cache = None
                raise AuthenticationError(
                    f"Storefront request failed (HTTP {r.status})."
                )
            return await r.json()