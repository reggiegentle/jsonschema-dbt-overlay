import hashlib
import json
import re
from pathlib import Path

import yaml

from .contract import read_inputs
from .walker import RESERVED_KEYWORDS, SchemaError, identifier, resolve, stable_json, walk


def sql_type(source):
    if source in ("text", "varchar", "uuid"):
        return "varchar"
    if source in ("integer", "bigint", "smallint"):
        return "number(38,0)"
    if source in ("double precision", "real", "numeric"):
        return "float"
    if source in ("boolean", "date"):
        return source
    if re.fullmatch(r"timestamp(?: \(\d+\))? with time zone", source):
        return "timestamp_tz"
    if source in ("json", "jsonb"):
        return "variant"
    raise SchemaError(f"Unsupported SQL source type {source!r}; add a type override")


def cast(expression, dtype):
    if dtype == "variant":
        return expression
    if dtype == "varchar":
        return f"{expression}::varchar"
    return f"try_cast(to_varchar({expression}) as {dtype})"


def render(contract_path, config_path):
    contract, config, base = read_inputs(contract_path, config_path)
    output_dir = (base / config["output_dir"]).resolve()
    manifest_path = (base / config["manifest"]).resolve()
    outputs, manifest, models = {}, [], []
    for table in sorted(contract["tables"], key=lambda t: (t["schema"], t["table"])):
        if "include_tables" in config and table["table"] not in config["include_tables"]:
            continue
        name = identifier(table["table"])
        settings = config.get("tables", {}).get(name, {})
        excludes = set(settings.get("exclude", []))
        records = {}
        for column, schema in sorted(table.get("jsonb", {}).items()):
            records.update(walk(schema, column))
        columns = {c["name"]: c for c in table["columns"]}
        top_overrides = {k: v for k, v in settings.get("type", {}).items() if k in columns and k not in records}
        path_settings = {**settings, "type": {k: v for k, v in settings.get("type", {}).items()
                                              if k not in top_overrides}}
        paths = resolve(records, path_settings, config.get("version_key", "_v"),
                        config.get("version_column", "version"))
        column_manifest = []
        expressions, used = [], {}

        def add(alias, expression, source):
            identifier(alias)
            if alias in used:
                raise SchemaError(f"{name}.{source}: column collision {alias} with {used[alias]}")
            used[alias] = source
            expressions.append(f"    {expression} as {alias}")

        for col, info in sorted(columns.items()):
            entry = {**info, "included": col not in excludes}
            if col in top_overrides:
                override = top_overrides[col]
                if set(override) != {"from", "to"} or override["from"] != [info["sqlType"]]:
                    raise SchemaError(f"{name}.{col}: stale type override")
                if override["to"] not in {"varchar", "number(38,0)", "float", "boolean", "date", "timestamp_tz", "variant"}:
                    raise SchemaError(f"{name}.{col}: unsupported override SQL type")
                entry["override"] = override
            column_manifest.append(entry)
            if col in excludes:
                continue
            if col.upper() in RESERVED_KEYWORDS:
                raise SchemaError(f"Reserved SQL identifier {col!r}; exclude it in config "
                                  "(relational columns cannot be renamed)")
            identifier(col)
            if not (table.get("jsonb") or settings.get("generate")):
                continue
            dtype = top_overrides[col]["to"] if col in top_overrides else sql_type(info["sqlType"])
            add(col, cast(col, dtype), col)
        tests = []
        for item in paths:
            if "column" not in item or item["path"].split(".")[0] in excludes:
                continue
            column, *keys = item["path"].split(".")
            expression = column + ":" + ".".join('"' + k.replace('"', '""') + '"' for k in keys)
            add(item["column"], cast(expression, item["sql_type"]), item["path"])
            if "discriminator_values" in item:
                values = [v for v in item["discriminator_values"] if v is not None]
                if not values:
                    continue
                quote = all(isinstance(v, str) for v in values)
                if quote:
                    values = [v.replace("\\", "\\\\").replace("'", "''") for v in values]
                elif not (all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values)
                          or all(isinstance(v, bool) for v in values)):
                    continue
                tests.append({"name": item["column"], "data_tests": [{"accepted_values": {
                    "arguments": {"values": values, "quote": quote}}}]})
        manifest.append({"schema": table["schema"], "table": name,
                         "columns": column_manifest, "paths": paths,
                         "jsonb_schema_sha256": {
                             column: hashlib.sha256(stable_json(schema).encode("utf-8")).hexdigest()
                             for column, schema in sorted(table.get("jsonb", {}).items())}})
        if not (table.get("jsonb") or settings.get("generate")):
            continue
        for alias, expression in settings.get("derived_columns", {}).items():
            add(alias, expression, f"derived:{alias}")
        for alias, expression in config["audit_columns"].items():
            add(alias, expression, f"audit:{alias}")
        model = identifier(config["model_prefix"] + name)
        shim = identifier(config["shim_prefix"] + name)
        header = "{{ config(materialized='view', tags=" + repr(config["tags"]) + ") }}\n\n"
        outputs[output_dir / f"{model}.sql"] = header + "select\n" + ",\n".join(expressions) + "\nfrom {{ ref('" + shim + "') }}\n"
        if config.get("current_state") is not None:
            state = config["current_state"]
            key = ", ".join(identifier(column) for column in settings["key"])
            order = ", ".join(identifier(column) + " desc" for column in state["order_by"])
            outputs[output_dir / f"{model}.sql"] += (
                f"qualify row_number() over (partition by {key} order by {order}) = 1\n"
                f"    and not ({state['deleted_when']})\n"
            )
        models.append({"name": model, "columns": tests})
    outputs[output_dir / "_models.yml"] = yaml.safe_dump({"version": 2, "models": models}, sort_keys=False)
    outputs[manifest_path] = json.dumps({"tables": manifest}, indent=2, sort_keys=True) + "\n"
    return outputs


def generate(contract, config):
    outputs = render(contract, config)
    for path, content in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return list(outputs)


def check(contract, config):
    outputs = render(contract, config)
    differences = []
    for path, content in outputs.items():
        if not path.exists():
            differences.append(f"Missing: {path}")
        elif path.read_text() != content:
            differences.append(f"Different: {path}")
    _, settings, base = read_inputs(contract, config)
    directory = (base / settings["output_dir"]).resolve()
    for path in sorted(directory.glob(settings["model_prefix"] + "*.sql")):
        if path not in outputs:
            differences.append(f"Unexpected: {path}")
    return differences
