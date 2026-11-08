"""Immich 3.3.5 API adapter: bounded reads, no silent partial results/writes."""
from __future__ import annotations

import time
from uuid import UUID

import requests


class APIError(RuntimeError):
    """Messages deliberately exclude URLs, response bodies and credentials."""


def is_uuid(value):
    try:
        return str(UUID(value)) == value.lower()
    except (ValueError, TypeError, AttributeError):
        return False


def _records(value, label):
    if not isinstance(value, list) or any(not isinstance(row, dict) or not is_uuid(row.get("id")) for row in value):
        raise APIError(f"Invalid {label} response")
    return value


class ImmichClient:
    def __init__(self, server_url, api_key, timeout=60, max_pages=10000, session=None):
        self.base = server_url.rstrip("/") + "/api"
        self.timeout = timeout
        self.max_pages = max_pages
        self.session = session or requests.Session()
        self.session.headers.update({"x-api-key": api_key, "Accept": "application/json"})
        # Do not pick up host .netrc credentials or an unrelated HTTP proxy.
        self.session.trust_env = False
        self._people = None
        self._albums = None

    def close(self):
        self.session.close()

    def request(self, method, path, *, params=None, payload=None):
        readonly = method == "GET" or (method == "POST" and path in ("/search/metadata", "/search/smart"))
        for attempt in range(3 if readonly else 1):
            try:
                response = self.session.request(method, self.base + path, params=params, json=payload,
                                                timeout=(10, self.timeout), allow_redirects=False)
            except requests.RequestException:
                if readonly and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                note = "Read failed" if readonly else "Write outcome uncertain; check album before retrying"
                raise APIError(note + " (network or TLS error)") from None
            if readonly and response.status_code in (429, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            if 300 <= response.status_code < 400:
                raise APIError("Redirect refused to avoid forwarding API key; use final Immich URL")
            if not 200 <= response.status_code < 300:
                raise APIError(f"Immich API returned HTTP {response.status_code}")
            try:
                return response.json()
            except ValueError:
                raise APIError("Immich returned invalid JSON") from None
        raise APIError("Read retries exhausted")

    def people(self):
        if self._people is not None:
            return self._people
        people = {}
        for page in range(1, self.max_pages + 1):
            data = self.request("GET", "/people", params={"page": page, "size": 500, "withHidden": "true"})
            if not isinstance(data, dict):
                raise APIError("Invalid people response")
            rows = _records(data.get("people"), "people")
            if rows and all(row["id"] in people for row in rows):
                raise APIError("People pagination repeated; refusing partial lookup")
            people.update({row["id"]: row for row in rows})
            has_next = data.get("hasNextPage")
            if has_next is not None and type(has_next) is not bool:
                raise APIError("Invalid people pagination flag")
            if has_next is False or (has_next is None and len(rows) < 500):
                self._people = list(people.values())
                return self._people
            if not rows:
                raise APIError("Empty people page with more pages indicated")
        raise APIError("People pagination limit reached; refusing partial lookup")

    def albums(self):
        if self._albums is None:
            self._albums = _records(self.request("GET", "/albums"), "albums")
        return self._albums

    @staticmethod
    def _resolve(identifier, rows, key, label):
        matches = [row for row in rows if (row["id"].lower() == identifier.lower() if is_uuid(identifier)
                                          else row.get(key) == identifier)]
        if len(matches) != 1:
            raise APIError(f"{label} missing or ambiguous; use a unique UUID from list command")
        return matches[0]["id"]

    def resolve_person(self, identifier):
        return self._resolve(identifier, self.people(), "name", "Person")

    def resolve_album(self, identifier):
        return self._resolve(identifier, self.albums(), "albumName", "Album")

    def search(self, query, kind="metadata"):
        if kind not in ("metadata", "smart"):
            raise APIError("Unsupported search kind")
        query = dict(query)
        limit = query.pop("limit", None)
        if kind == "smart":
            # 3.3.5 smart results are bounded nearest matches, not exhaustive membership.
            query["size"] = limit or 200
            query["withExif"] = True
            data = self.request("POST", "/search/smart", payload=query)
            rows = self._search_rows(data)
            return rows[:query["size"]]
        structured = "filter" in query or "orderBy" in query
        assets = {}
        next_token = None
        seen = set()
        for page in range(1, self.max_pages + 1):
            payload = {**query, "size": 500, "withExif": True}
            if structured:
                if next_token is not None:
                    payload["cursor"] = next_token
            else:
                payload["page"] = page if next_token is None else int(next_token)
            data = self.request("POST", "/search/metadata", payload=payload)
            rows = self._search_rows(data)
            if rows and all(row["id"] in assets for row in rows):
                raise APIError("Search page repeated; refusing partial result")
            assets.update({row["id"]: row for row in rows})
            field = "nextCursor" if structured else "nextPage"
            if field not in data["assets"]:
                raise APIError("Missing pagination field; refusing partial result")
            next_token = data["assets"][field]
            if next_token is None:
                return list(assets.values())
            if not rows or not isinstance(next_token, str) or not next_token or next_token in seen:
                raise APIError("Invalid or repeated pagination token")
            if not structured and (not next_token.isdigit() or int(next_token) <= int(payload["page"])):
                raise APIError("Invalid page number")
            seen.add(next_token)
        raise APIError("Search pagination limit reached; refusing partial result")

    @staticmethod
    def _search_rows(data):
        if not isinstance(data, dict) or not isinstance(data.get("assets"), dict):
            raise APIError("Invalid search response")
        return _records(data["assets"].get("items"), "search")

    def add_assets(self, album_id, asset_ids):
        counts = {"added": 0, "duplicate": 0, "failed": 0}
        ids = sorted(set(asset_ids))
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            result = self.request("PUT", f"/albums/{album_id}/assets", payload={"ids": chunk})
            if (not isinstance(result, list) or len(result) != len(chunk)
                    or any(not isinstance(row, dict) for row in result)
                    or {row.get("id") for row in result} != set(chunk)):
                raise APIError("Invalid album write response; outcome uncertain, inspect album")
            for row in result:
                if row.get("success") is True:
                    counts["added"] += 1
                elif row.get("success") is False and row.get("error") == "duplicate":
                    counts["duplicate"] += 1
                else:
                    counts["failed"] += 1
            if counts["failed"]:
                # Earlier chunks may already be committed; never claim atomicity or retry automatically.
                break
        return counts
