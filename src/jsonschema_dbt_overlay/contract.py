import json
from pathlib import Path

import yaml

from .walker import SchemaError, identifier


def read_inputs(contract, config):
    contract = json.loads(Path(contract).read_text())
    config_path = Path(config).resolve()
    config = yaml.safe_load(config_path.read_text())
    allowed = {"model_prefix", "shim_prefix", "source_name", "output_dir", "manifest", "tags",
               "audit_columns", "tables", "include_tables", "current_state", "version_key", "version_column"}
    if set(config) - allowed:
        raise SchemaError(f"Unknown config keys: {sorted(set(config) - allowed)}")
    for key in ("version_key", "version_column"):
        if key in config and (not isinstance(config[key], str) or not config[key].strip()):
            raise SchemaError(f"{key} must be a nonempty string")
    identifier(config.get("version_column", "version"))
    tables = contract["tables"]
    names = [t["table"] for t in tables]
    if len(names) != len(set(names)):
        raise SchemaError("Duplicate table names")
    include = config.get("include_tables")
    if "include_tables" in config:
        if not isinstance(include, list) or any(not isinstance(n, str) for n in include):
            raise SchemaError("include_tables must be a list of table names")
        if not include:
            raise SchemaError("include_tables must be a non-empty list of table names")
        if len(include) != len(set(include)) or set(include) - set(names):
            raise SchemaError("include_tables contains duplicate or absent tables")
    current_state = config.get("current_state")
    if "current_state" in config:
        if not isinstance(current_state, dict) or set(current_state) != {"order_by", "deleted_when"}:
            raise SchemaError("current_state requires order_by and deleted_when")
        order = current_state["order_by"]
        if not isinstance(order, list) or not order or any(not isinstance(c, str) for c in order):
            raise SchemaError("current_state.order_by must be a nonempty list of columns")
        if not isinstance(current_state["deleted_when"], str) or not current_state["deleted_when"].strip():
            raise SchemaError("current_state.deleted_when must be a nonempty SQL expression")
    for name, settings in config.get("tables", {}).items():
        if name not in names:
            raise SchemaError(f"{name}: stale table exception")
        if set(settings) - {"exclude", "rename", "type", "key", "generate", "derived_columns"}:
            raise SchemaError(f"{name}: unknown exception keys")
        if "generate" in settings and not isinstance(settings["generate"], bool):
            raise SchemaError(f"{name}: generate must be boolean")
        derived = settings.get("derived_columns", {})
        if not isinstance(derived, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) or not v.strip()
            for k, v in derived.items()
        ):
            raise SchemaError(f"{name}: derived_columns must map names to SQL expressions")
    for table in tables:
        columns = [c["name"] for c in table["columns"]]
        if len(columns) != len(set(columns)):
            raise SchemaError(f"{table['table']}: duplicate contract columns")
        settings = config.get("tables", {}).get(table["table"], {})
        key = settings.get("key")
        selected = include is None or table["table"] in include
        if key is not None:
            if not isinstance(key, list) or not key or any(not isinstance(c, str) for c in key):
                raise SchemaError(f"{table['table']}: key must be a nonempty list of columns")
            if len(key) != len(set(key)) or set(key) - set(columns):
                raise SchemaError(f"{table['table']}: duplicate or absent key column")
        elif current_state is not None and selected and (table.get("jsonb") or settings.get("generate")):
            raise SchemaError(f"{table['table']}: current_state requires a key")
        missing = set(table.get("jsonb", {})) - set(columns)
        excluded = set(config.get("tables", {}).get(table["table"], {}).get("exclude", []))
        if missing or excluded - set(columns):
            raise SchemaError(f"{table['table']}: absent jsonb column or stale exclude: "
                              f"{sorted(missing | (excluded - set(columns)))}")
    return contract, config, config_path.parent
