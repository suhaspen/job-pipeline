"""Google Sheets v4 transport.

Every network call in the Sheets feature goes through `SheetsClient._request`.
That is deliberate and load-bearing rather than tidy: it is the single point the
test guard patches, so no test can reach the live spreadsheet however it is
written. A second HTTP path added elsewhere in this package would silently
defeat that, and the thing it protects is the one surface in this system that
holds data with no upstream copy.

`google-auth` mints the token; the API calls themselves are plain `requests`,
same as every other HTTP client here. The full `google-api-python-client` would
add a discovery layer and a second HTTP stack for four endpoints.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

import requests

from jobpipe.config import USER_AGENT

API = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
TIMEOUT_S = 20.0


class SheetsError(Exception):
    """Anything that stops a Sheets call completing.

    Callers in the run path catch this and carry on: the mirror is a
    convenience view, and a spreadsheet that is briefly unreachable must never
    cost a poll or a notification.
    """


def decode_key(raw: str) -> dict[str, Any]:
    """Service-account JSON from the secret, base64 or plain.

    Accepts both because the secret is pasted by hand: base64 is what the
    setup instructions produce, and a straight paste of the JSON is the
    obvious thing to do instead. Neither the key nor any part of it is ever
    logged - `SheetsError` messages here name the failure, never the value.
    """
    text = raw.strip()
    if not text:
        raise SheetsError("service-account key is empty")
    if not text.lstrip().startswith("{"):
        try:
            text = base64.b64decode(text, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise SheetsError(f"key is neither JSON nor valid base64: {type(exc).__name__}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SheetsError(f"decoded key is not JSON: {exc.msg}")
    missing = {"client_email", "private_key", "token_uri"} - set(data)
    if missing:
        raise SheetsError(f"service-account key is missing {sorted(missing)}")
    return data


class SheetsClient:
    def __init__(self, spreadsheet_id: str, key_material: str):
        self.spreadsheet_id = spreadsheet_id
        self._key = key_material
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT})
        self._creds = None

    # -- auth ---------------------------------------------------------------

    @property
    def service_account_email(self) -> str:
        """Who to share the sheet with. Printed by `jobpipe sheets doctor`."""
        return decode_key(self._key).get("client_email", "?")

    def _token(self) -> str:
        # Imported here rather than at module scope: the rest of the package,
        # and every test that does not touch Sheets, must import without it.
        try:
            import google.auth.transport.requests as ga_requests
            from google.oauth2 import service_account
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise SheetsError(f"google-auth is not installed: {exc}")

        if self._creds is None:
            self._creds = service_account.Credentials.from_service_account_info(
                decode_key(self._key), scopes=SCOPES
            )
        if not self._creds.valid:
            try:
                self._creds.refresh(ga_requests.Request())
            except Exception as exc:
                raise SheetsError(f"could not mint an access token: {type(exc).__name__}: {exc}")
        return self._creds.token

    # -- transport ----------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """The only place this package touches the network."""
        url = f"{API}/{self.spreadsheet_id}{path}"
        headers = {"Authorization": f"Bearer {self._token()}"}
        try:
            resp = self._session.request(
                method, url, headers=headers, timeout=TIMEOUT_S, **kwargs
            )
        except requests.RequestException as exc:
            raise SheetsError(f"{method} {path}: {type(exc).__name__}")
        if resp.status_code >= 400:
            # Google puts a usable reason in the body; the status alone does
            # not distinguish "not shared with the service account" from
            # "spreadsheet id is wrong", and those have different fixes.
            detail = ""
            try:
                detail = resp.json().get("error", {}).get("message", "")
            except ValueError:
                detail = resp.text[:200]
            raise SheetsError(f"{method} {path}: HTTP {resp.status_code} {detail}")
        return resp.json() if resp.content else {}

    # -- values -------------------------------------------------------------

    def read(self, a1: str) -> list[list[str]]:
        """Read a range. Rows are ragged - Sheets truncates trailing blanks."""
        data = self._request(
            "GET", f"/values/{a1}", params={"majorDimension": "ROWS"}
        )
        return data.get("values", [])

    def write(self, writes: list[tuple[str, list[list[Any]]]]) -> int:
        """Write explicit ranges, nothing else.

        `values.batchUpdate` writes exactly the ranges named. It is used in
        preference to `values.append` because append infers the extent of a
        "table" from surrounding data, and the columns beside this one are the
        user's - an inferred range is exactly the class of mistake this whole
        feature is arranged to avoid. There is no clear, no row insert and no
        dimension change anywhere in this module.
        """
        if not writes:
            return 0
        body = {
            # USER_ENTERED so a date lands as a date rather than a string.
            # Everything textual is escaped before it gets here; see mirror.py.
            "valueInputOption": "USER_ENTERED",
            "data": [{"range": a1, "values": values} for a1, values in writes],
        }
        result = self._request("POST", "/values:batchUpdate", json=body)
        return int(result.get("totalUpdatedCells", 0))

    # -- structure ----------------------------------------------------------

    def tabs(self) -> dict[str, int]:
        """Tab title -> sheetId."""
        return {t: p["sheetId"] for t, p in self.tab_properties().items()}

    def tab_properties(self) -> dict[str, dict[str, Any]]:
        """Tab title -> {sheetId, rows}.

        `rows` is the grid height, which is a hard ceiling on writes: a range
        past it is rejected outright rather than growing the sheet. A new
        spreadsheet is 1000 rows, and at ~70 new postings a day that is eight
        days of headroom.
        """
        data = self._request(
            "GET", "",
            params={"fields": "sheets.properties(sheetId,title,gridProperties.rowCount)"},
        )
        out: dict[str, dict[str, Any]] = {}
        for sheet in data.get("sheets", []):
            props = sheet["properties"]
            out[props["title"]] = {
                "sheetId": props["sheetId"],
                "rows": props.get("gridProperties", {}).get("rowCount", 0),
            }
        return out

    def batch_update(self, requests_: list[dict[str, Any]]) -> dict[str, Any]:
        """Structural changes: create a tab, add a conditional format rule.

        Never called from the poll path. Only `jobpipe sheets setup` uses it,
        and it is the caller's job never to put a delete or a dimension change
        in the list.
        """
        if not requests_:
            return {}
        return self._request("POST", ":batchUpdate", json={"requests": requests_})
