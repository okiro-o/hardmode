"""HTTP clients for the donation services. Blocking (urllib), called from the polling thread.

DonationAlerts (OAuth 2.0 authorization code; the streamer registers an application in the DA dashboard):
* authorize: ``https://www.donationalerts.com/oauth/authorize?client_id&redirect_uri&response_type=code&scope``
* token:     ``POST https://www.donationalerts.com/oauth/token`` (form: grant_type authorization_code | refresh_token)
* user:      ``GET  https://www.donationalerts.com/api/v1/user/oauth``          (scope oauth-user-show)
* donations: ``GET  https://www.donationalerts.com/api/v1/alerts/donations?page`` (scope oauth-donation-index)
  limit: 60 requests per minute per application.
Sources: @donation-alerts/api-call, /auth, /api 4.0.0 (npm), checked 2026-11-01; apidoc page was unreachable.

DonatePay (personal API key from the DonatePay dashboard, no OAuth):
* user:         ``GET https://donatepay.ru/api/v1/user?access_token=KEY``
* transactions: ``GET https://donatepay.ru/api/v1/transactions?access_token=KEY&limit&order&type=donation``
  item: ``{id, what, sum: "100.00", status: success|user|cancel|wait, type, vars: {name, comment}, comment,
  created_at: {date, timezone}}``; ``status=user`` = test notification from the dashboard.
  limit: one request per method per key every ~20 s.
Source: antim0118/DonatePayAPI (C#, mirrors the official page that is behind a login).
"""
from __future__ import annotations

import datetime as _dt
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

DA_BASE = "https://www.donationalerts.com"
DA_SCOPES = ("oauth-user-show", "oauth-donation-index")
DP_BASE = "https://donatepay.ru/api/v1"
USER_AGENT = "hardmode/1.0"


class ProviderError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Donation:
    provider: str            # "da" | "dp" | "test"
    id: str
    user: str
    amount: float            # in the streamer's main currency when the service tells it
    currency: str
    message: str
    created_ts: float | None = None
    test: bool = False

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def _http(method: str, url: str, *, headers: dict | None = None, form: dict | None = None,
          timeout: float = 10.0) -> dict:
    data = urllib.parse.urlencode(form).encode("utf-8") if form is not None else None
    h = {"Accept": "application/json", "User-Agent": USER_AGENT, **(headers or {})}
    if data is not None:
        h["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise ProviderError(f"HTTP {e.code}: {detail or e.reason}", status=e.code) from None
    except urllib.error.URLError as e:
        raise ProviderError(f"нет связи: {e.reason}") from None
    except OSError as e:          # timeouts, resets
        raise ProviderError(f"нет связи: {e}") from None
    try:
        out = json.loads(raw) if raw else {}
    except ValueError:
        raise ProviderError(f"не JSON: {raw[:120]}") from None
    return out if isinstance(out, dict) else {"data": out}


def _float(v) -> float:
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------- DonationAlerts
def da_authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    q = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
         "scope": " ".join(DA_SCOPES), "state": state}
    return f"{DA_BASE}/oauth/authorize?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote)


def da_token(client_id: str, client_secret: str, *, code: str | None = None, redirect_uri: str = "",
             refresh_token: str | None = None) -> dict:
    """Exchange a code or refresh. Returns {access_token, refresh_token, expires_in}."""
    if refresh_token:
        form = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id,
                "client_secret": client_secret, "scope": " ".join(DA_SCOPES)}
    else:
        form = {"grant_type": "authorization_code", "client_id": client_id, "client_secret": client_secret,
                "redirect_uri": redirect_uri, "code": code or ""}
    out = _http("POST", f"{DA_BASE}/oauth/token", form=form)
    if not out.get("access_token"):
        raise ProviderError(f"DonationAlerts не выдал токен: {str(out)[:200]}")
    return out


def da_user(access_token: str) -> dict:
    out = _http("GET", f"{DA_BASE}/api/v1/user/oauth", headers={"Authorization": f"Bearer {access_token}"})
    return out.get("data") if isinstance(out.get("data"), dict) else {}


def da_donations(access_token: str, page: int = 1) -> list[Donation]:
    out = _http("GET", f"{DA_BASE}/api/v1/alerts/donations?page={int(page)}",
                headers={"Authorization": f"Bearer {access_token}"})
    rows = out.get("data") if isinstance(out.get("data"), list) else []
    return [d for d in (parse_da(r) for r in rows) if d is not None]


def parse_da(r: dict) -> Donation | None:
    if not isinstance(r, dict) or r.get("id") is None:
        return None
    amount = r.get("amount_in_user_currency")
    amount = _float(amount) if amount not in (None, "") else _float(r.get("amount"))
    created = r.get("created_at_ts")
    ts = float(created) if isinstance(created, (int, float)) else _parse_utc(r.get("created_at"))
    return Donation(provider="da", id=str(r["id"]), user=str(r.get("username") or "аноним")[:40], amount=amount,
                    currency=str(r.get("currency") or ""), message=str(r.get("message") or "")[:500], created_ts=ts)


def _parse_utc(value) -> float | None:
    try:
        d = _dt.datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    return d.replace(tzinfo=_dt.timezone.utc).timestamp()


# --------------------------------------------------------------------------- DonatePay
def dp_user(api_key: str) -> dict:
    out = _http("GET", f"{DP_BASE}/user?" + urllib.parse.urlencode({"access_token": api_key}))
    if out.get("status") != "success":
        raise ProviderError(str(out.get("message") or "DonatePay отклонил ключ"))
    return out.get("data") if isinstance(out.get("data"), dict) else {}


def dp_transactions(api_key: str, limit: int = 25) -> list[Donation]:
    q = {"access_token": api_key, "limit": int(limit), "order": "DESC", "type": "donation"}
    out = _http("GET", f"{DP_BASE}/transactions?" + urllib.parse.urlencode(q))
    if out.get("status") != "success":
        raise ProviderError(str(out.get("message") or "DonatePay вернул ошибку"))
    rows = out.get("data") if isinstance(out.get("data"), list) else []
    return [d for d in (parse_dp(r) for r in rows) if d is not None]


_TZ_OFFSETS = {"utc": 0, "europe/moscow": 3}


def parse_dp(r: dict) -> Donation | None:
    """None for anything that is not a finished donation (cashouts, cancelled, pending)."""
    if not isinstance(r, dict) or r.get("id") is None:
        return None
    status = str(r.get("status") or "")
    if r.get("type") not in (None, "donation") or status not in ("success", "user"):
        return None
    v = r.get("vars") if isinstance(r.get("vars"), dict) else {}
    message = v.get("comment") or r.get("comment") or ""
    created = r.get("created_at") if isinstance(r.get("created_at"), dict) else {}
    ts = None
    try:
        d = _dt.datetime.strptime(str(created.get("date") or "")[:19], "%Y-%m-%d %H:%M:%S")
        hours = _TZ_OFFSETS.get(str(created.get("timezone") or "Europe/Moscow").lower(), 3)
        ts = d.replace(tzinfo=_dt.timezone(_dt.timedelta(hours=hours))).timestamp()
    except ValueError:
        pass
    return Donation(provider="dp", id=str(r["id"]), user=str(v.get("name") or r.get("what") or "аноним")[:40],
                    amount=_float(r.get("sum")), currency="RUB", message=str(message)[:500], created_ts=ts,
                    test=(status == "user"))
