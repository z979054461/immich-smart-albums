"""Strict, secret-safe configuration for the container service."""
from __future__ import annotations

import os
import math
import re
from pathlib import Path
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from croniter import croniter
from jsonpath_ng import parse


class ConfigError(ValueError):
    pass


class UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ConfigError("YAML keys must be unique strings")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _object(value, allowed, label):
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be a mapping")
    if set(value) - set(allowed):
        raise ConfigError(f"{label} contains unknown fields (check spelling)")


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} must be a nonempty string")
    return value.strip()


def _bool(value, label):
    if type(value) is not bool:
        raise ConfigError(f"{label} must be true or false")
    return value


def _int(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ConfigError(f"{label} must be an integer from {low} to {high}")
    return value


RULE_FIELDS = {f"{kind}_{mode}" for kind in ("people", "metadata", "smart", "local")
               for mode in ("any", "all")}
# Top-level search fields owned by pagination or unsafe to silently ignore.
RESERVED = {"page", "cursor", "size", "withExif", "resultLimit", "limit"}


# Explicit supported subset of Immich 3.3.5 fields. Typos must never become a broad query.
STRING_FIELDS = {"city", "state", "country", "make", "model", "lensModel", "description",
                 "originalFileName", "originalPath", "checksum", "encodedVideoPath", "ocr"}
BOOL_FIELDS = {"isFavorite", "isMotion", "isOffline", "isEncoded", "hasAlbums", "hasPeople", "hasTags"}
ID_FIELDS = {"id", "libraryId"}
IDS_FIELDS = {"personIds", "albumIds", "tagIds"}
DATE_FIELDS = {"takenAt", "createdAt", "updatedAt", "trashedAt"}
DATE_FLAT = {"takenBefore", "takenAfter", "createdBefore", "createdAfter", "updatedBefore",
             "updatedAfter", "trashedBefore", "trashedAfter"}
ENUMS = {"type": {"IMAGE", "VIDEO", "AUDIO", "OTHER"},
         "visibility": {"timeline", "hidden", "archive", "locked"}}


def _filter_branch(branch, allow_or=True):
    allowed = STRING_FIELDS | BOOL_FIELDS | ID_FIELDS | IDS_FIELDS | DATE_FIELDS | set(ENUMS) | {"rating", "fileSizeInBytes"}
    _object(branch, allowed | ({"or"} if allow_or else set()), "metadata.filter")
    if not branch:
        raise ConfigError("metadata.filter cannot be empty; use all_assets explicitly")
    for field, expression in branch.items():
        if field == "or":
            if not isinstance(expression, list) or not expression:
                raise ConfigError("filter.or must be nonempty")
            for item in expression:
                _filter_branch(item, allow_or=False)
            continue
        if field in BOOL_FIELDS:
            operators = {"eq"}
        elif field in IDS_FIELDS:
            operators = {"any", "all", "none"}
        elif field in ID_FIELDS:
            operators = {"eq", "ne"}
        elif field in DATE_FIELDS:
            operators = {"eq", "ne", "gt", "gte", "lt", "lte"}
        elif field in {"rating", "fileSizeInBytes"}:
            operators = {"eq", "ne", "gt", "gte", "lt", "lte", "in", "notIn"}
        elif field == "ocr":
            operators = {"matches"}
        elif field in {"description", "originalFileName", "originalPath"}:
            operators = {"eq", "ne", "in", "notIn", "like", "notLike", "startsWith", "endsWith"}
        else:
            operators = {"eq", "ne", "in", "notIn"}
        _object(expression, operators, "filter expression")
        if not expression:
            raise ConfigError("Filter expressions cannot be empty")
        for op, operand in expression.items():
            values = operand if op in {"in", "notIn", "any", "all", "none"} else [operand]
            if not isinstance(values, list) or not values:
                raise ConfigError("List filter operand must be a nonempty list")
            for value in values:
                if field in BOOL_FIELDS:
                    _bool(value, "boolean filter")
                elif field in {"rating", "fileSizeInBytes"}:
                    if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
                        raise ConfigError("Numeric filter operand required")
                elif field in ENUMS:
                    if not isinstance(value, str) or value not in ENUMS[field]:
                        raise ConfigError("Unknown enum filter value")
                elif value is not None:
                    _text(value, "string filter operand")


def validate_metadata(query):
    if "filter" in query or "orderBy" in query:
        _object(query, {"filter", "orderBy", "withPeople", "withStacked"}, "structured metadata query")
        if "filter" not in query:
            raise ConfigError("Structured metadata query requires filter")
        _filter_branch(query["filter"])
        if "orderBy" in query:
            _object(query["orderBy"], {"field", "direction"}, "orderBy")
            if (not isinstance(query["orderBy"].get("field"), str) or not isinstance(query["orderBy"].get("direction", "desc"), str)
                    or query["orderBy"].get("field") not in {"fileCreatedAt", "createdAt", "updatedAt"}
                    or query["orderBy"].get("direction", "desc") not in {"asc", "desc"}):
                raise ConfigError("Unsupported orderBy; omit it for the default order")
        for key in ("withPeople", "withStacked"):
            if key in query:
                _bool(query[key], key)
    else:
        allowed = STRING_FIELDS | ID_FIELDS | IDS_FIELDS | DATE_FLAT | (BOOL_FIELDS - {"hasAlbums", "hasPeople", "hasTags"}) | set(ENUMS) | {"rating", "isNotInAlbum"}
        _object(query, allowed, "legacy metadata query")
        for field, value in query.items():
            if field in BOOL_FIELDS or field == "isNotInAlbum":
                _bool(value, field)
            elif field in IDS_FIELDS:
                if not isinstance(value, list) or not value:
                    raise ConfigError("ID filters must be nonempty lists")
                for item in value:
                    _text(item, field)
            elif field in ENUMS:
                if not isinstance(value, str) or value not in ENUMS[field]:
                    raise ConfigError("Unknown enum filter value")
            elif field == "rating":
                if value is not None:
                    _int(value, 1, 5, field)
            else:
                _text(value, field)


def validate_rules(rules, label, include=False):
    _object(rules, RULE_FIELDS | ({"all_assets"} if include else set()), label)
    if "all_assets" in rules:
        _bool(rules["all_assets"], label + ".all_assets")
    for name, values in rules.items():
        if name == "all_assets":
            continue
        if not isinstance(values, list) or not values:
            raise ConfigError(f"{label}.{name} must be a nonempty list")
        for value in values:
            kind = name.split("_")[0]
            if kind == "people":
                _text(value, label + ".people")
            elif kind == "metadata":
                if not isinstance(value, dict) or not value or RESERVED.intersection(value):
                    raise ConfigError("metadata queries must be nonempty mappings without paging fields")
                validate_metadata(value)
            elif kind == "smart":
                _object(value, {"query", "limit"}, "smart query")
                _text(value.get("query"), "smart.query")
                _int(value.get("limit", 200), 1, 1000, "smart.limit")
            else:
                _object(value, {"path", "regex"}, "local filter")
                _text(value.get("path"), "local.path")
                try:
                    parse(value["path"])
                    if "regex" in value:
                        re.compile(_text(value["regex"], "local.regex"), re.IGNORECASE)
                except Exception:
                    # Parser exceptions may include original configuration; keep logs secret-safe.
                    raise ConfigError("Invalid local JSONPath or regex") from None
    if include and not rules.get("all_assets") and not any(
        rules.get(name) for name in RULE_FIELDS if not name.startswith("local_")
    ):
        raise ConfigError("Each job needs a people/metadata/smart include, or explicit all_assets: true")


def load_config(path):
    try:
        with open(path, encoding="utf-8") as stream:
            cfg = yaml.load(stream, Loader=UniqueLoader)
    except (OSError, UnicodeError, yaml.YAMLError):
        raise ConfigError("Cannot read configuration or invalid YAML") from None
    _object(cfg, {"server_url", "api_key_file", "timezone", "schedule", "run_on_startup",
                  "dry_run", "request_timeout", "max_pages", "jobs"}, "config")
    cfg["server_url"] = _text(os.getenv("IMMICH_SERVER_URL") or cfg.get("server_url"), "server_url").rstrip("/")
    try:
        parts = urlsplit(cfg["server_url"])
        parts.port  # Validate malformed/out-of-range ports as well as brackets.
    except ValueError:
        raise ConfigError("Invalid server_url") from None
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ConfigError("server_url must be HTTP(S), without credentials, query or fragment")
    if cfg["server_url"].endswith("/api"):
        cfg["server_url"] = cfg["server_url"][:-4]
    cfg["timezone"] = _text(cfg.get("timezone", "Asia/Shanghai"), "timezone")
    try:
        ZoneInfo(cfg["timezone"])
    except (ValueError, ZoneInfoNotFoundError):
        raise ConfigError("Unknown IANA timezone") from None
    cfg["schedule"] = _text(cfg.get("schedule", "0 */6 * * *"), "schedule")
    if len(cfg["schedule"].split()) != 5 or not croniter.is_valid(cfg["schedule"]):
        raise ConfigError("schedule must be a five-field cron expression")
    try:
        croniter(cfg["schedule"], datetime.now(ZoneInfo(cfg["timezone"])), max_years_between_matches=5).get_next(datetime)
    except (ValueError, OverflowError):
        raise ConfigError("Cron schedule has no valid date within five years") from None
    for field, default in (("dry_run", True), ("run_on_startup", True)):
        cfg[field] = _bool(cfg.get(field, default), field)
    cfg["request_timeout"] = _int(cfg.get("request_timeout", 60), 1, 300, "request_timeout")
    cfg["max_pages"] = _int(cfg.get("max_pages", 10000), 1, 100000, "max_pages")
    jobs = cfg.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise ConfigError("jobs must be a nonempty list")
    names = set()
    for job in jobs:
        _object(job, {"name", "enabled", "album", "include", "exclude", "max_matches", "dry_run"}, "job")
        job["name"] = _text(job.get("name"), "job.name")
        if job["name"] in names:
            raise ConfigError("Duplicate job names")
        names.add(job["name"])
        job["enabled"] = _bool(job.get("enabled", True), "job.enabled")
        job["album"] = _text(job.get("album"), "job.album")
        job["max_matches"] = _int(job.get("max_matches", 100000), 1, 10000000, "job.max_matches")
        if "dry_run" in job:
            _bool(job["dry_run"], "job.dry_run")
        validate_rules(job.get("include"), "job.include", include=True)
        validate_rules(job.get("exclude", {}), "job.exclude")
    if "api_key_file" in cfg:
        _text(cfg["api_key_file"], "api_key_file")
    return cfg


def load_api_key(cfg):
    key = os.environ.get("IMMICH_API_KEY")
    if not key:
        key_path = os.environ.get("IMMICH_API_KEY_FILE") or cfg.get("api_key_file", "/run/secrets/immich_api_key")
        try:
            key = Path(key_path).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            raise ConfigError("API key is missing: set IMMICH_API_KEY or mount api_key_file") from None
    if not key or any(c.isspace() for c in key):
        raise ConfigError("API key must be a single nonempty value without whitespace")
    return key
