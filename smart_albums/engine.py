"""Add-only selection with deterministic AND/OR/exclusion semantics.

Inspired by tahnau/immich-smart-albums; distributed under the upstream GPL-3.0.
"""
from __future__ import annotations

import re
from jsonpath_ng import parse
from .api import APIError


def _combine(sets, mode):
    if not sets:
        return set()
    return set.union(*sets) if mode == "any" else set.intersection(*sets)


def select_assets(client, job):
    assets = {}
    cache = {}

    def search(query, kind="metadata"):
        import json
        key = kind + json.dumps(query, sort_keys=True, ensure_ascii=False)
        if key not in cache:
            rows = client.search(query, kind)
            assets.update({a["id"]: a for a in rows})
            cache[key] = {a["id"] for a in rows}
        return cache[key]

    def remote_sets(rules):
        groups = []
        for kind in ("people", "metadata", "smart"):
            for mode in ("any", "all"):
                values = rules.get(f"{kind}_{mode}", [])
                if not values:
                    continue
                sets = []
                for value in values:
                    if kind == "people":
                        person_id = client.resolve_person(value)
                        sets.append(search({"filter": {"personIds": {"any": [person_id]}}}))
                    elif kind == "metadata":
                        sets.append(search(value))
                    else:
                        sets.append(search(value, "smart"))
                groups.append(_combine(sets, mode))
        return groups

    include = job["include"]
    exclude = job.get("exclude", {})
    groups = remote_sets(include)
    if include.get("all_assets"):
        groups.append(search({"filter": {}}))
    if not groups:
        raise APIError("No remote inclusion; refusing an unbounded accidental selection")
    chosen = set.intersection(*groups)
    excluded = set.union(set(), *remote_sets(exclude))

    def local_sets(rules):
        groups = []
        for mode in ("any", "all"):
            filters = rules.get(f"local_{mode}", [])
            if not filters:
                continue
            sets = []
            for rule in filters:
                path = parse(rule["path"])
                regex = re.compile(rule["regex"], re.IGNORECASE) if "regex" in rule else None
                matches = set()
                for aid in chosen:
                    values = [m.value for m in path.find(assets[aid])]
                    if values and (regex is None or any(v is not None and regex.search(str(v)) for v in values)):
                        matches.add(aid)
                sets.append(matches)
            groups.append(_combine(sets, mode))
        return groups

    for matching in local_sets(include):
        chosen &= matching
    excluded.update(set.union(set(), *local_sets(exclude)))
    result = sorted(chosen - excluded)
    if len(result) > job.get("max_matches", 100000):
        raise APIError("Selection exceeds max_matches; no writes performed for this job")
    return result


def run_job(client, job, dry_run=False):
    # Validate the destination before any search or write. Missing/duplicate names fail closed.
    album_id = client.resolve_album(job["album"])
    selected = select_assets(client, job)
    result = {"matched": len(selected), "dry_run": dry_run, "added": 0, "duplicate": 0, "failed": 0}
    if selected and not dry_run:
        result.update(client.add_assets(album_id, selected))
    result["status"] = "failed" if result["failed"] else "ok"
    return result
