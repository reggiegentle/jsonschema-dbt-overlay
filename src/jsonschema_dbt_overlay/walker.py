import json
import re


class SchemaError(ValueError):
    pass


OPEN_TYPES = {"string", "number", "boolean", "null", "object", "array"}
SQL_TYPES = {"string": "varchar", "integer": "number(38,0)", "number": "float",
             "boolean": "boolean", "object": "variant", "array": "variant", "null": "variant"}


RESERVED_KEYWORDS = frozenset("""
    ACCOUNT ALL ALTER AND ANY AS ASC BETWEEN BY CASE CAST CHECK COLUMN CONNECT
    CONNECTION CONSTRAINT CREATE CROSS CURRENT CURRENT_DATE CURRENT_TIME
    CURRENT_TIMESTAMP CURRENT_USER DATABASE DELETE DESC DISTINCT DROP ELSE EXISTS
    FALSE FOLLOWING FOR FROM FULL GRANT GROUP GSCLUSTER HAVING ILIKE IN INCREMENT
    INNER INSERT INTERSECT INTO IS ISSUE JOIN LATERAL LEFT LIKE LOCALTIME
    LOCALTIMESTAMP MINUS NATURAL NOT NULL OF ON OR ORDER ORGANIZATION QUALIFY
    REGEXP REVOKE RIGHT RLIKE ROW ROWS SAMPLE SCHEMA SELECT SET SOME START TABLE
    TABLESAMPLE THEN TO TRIGGER TRUE TRY_CAST UNION UNIQUE UPDATE USING VALUES
    VIEW WHEN WHENEVER WHERE WINDOW WITH
""".split())


def identifier(value):
    if value.upper() in RESERVED_KEYWORDS:
        raise SchemaError(f"Reserved SQL identifier {value!r}; rename it in config")
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", value):
        raise SchemaError(f"Invalid SQL identifier {value!r}")
    return value


def snake(value):
    value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", value)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value).lower()


def stable_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def is_open(schema):
    return schema is True or (isinstance(schema, dict) and
                             set(schema.get("type", []) if isinstance(schema.get("type"), list)
                                 else []) == OPEN_TYPES)


def branch_id(schema, position, discriminators):
    consts = {k: v["const"] for k, v in sorted(schema.get("properties", {}).items())
              if "const" in v}
    if discriminators:
        return {"discriminators": {k: consts[k] for k in sorted(discriminators)}}
    return {"signature": f"{position}:" + stable_json({
        "required": sorted(schema.get("required", [])), "const": consts})}


def walk(schema, column):
    records = {}

    def visit(node, path, branches, emit=True, nullable=False):
        if not isinstance(node, dict) or not node:
            raise SchemaError(f"{path}: untyped schema")
        if any(k in node for k in ("$ref", "allOf", "not", "if", "then", "else", "patternProperties", "prefixItems")):
            raise SchemaError(f"{path}: unsupported schema structure")
        for union in ("oneOf", "anyOf"):
            if union in node:
                siblings = set(node) - {union, "$schema", "$comment", "title", "description",
                                        "default", "examples", "deprecated", "readOnly", "writeOnly"}
                if siblings:
                    raise SchemaError(f"{path}: {union} has unsupported siblings {sorted(siblings)}")
                options = node[union]
                null = any(b.get("type") == "null" for b in options)
                const_keys = [{k for k, v in b.get("properties", {}).items() if "const" in v}
                              for b in options]
                discriminators = set.intersection(*const_keys) if const_keys else set()
                for i, option in enumerate(options):
                    context = branches + [{"path": path, "union": union,
                                            **branch_id(option, i, discriminators)}]
                    visit(option, path, context, emit, nullable or null)
                return
        kind = node.get("type")
        kinds = kind if isinstance(kind, list) else [kind]
        if any(k not in SQL_TYPES for k in kinds):
            raise SchemaError(f"{path}: untyped or unsupported leaf {kind!r}")
        opened = is_open(node)
        if isinstance(kind, list) and not opened:
            for k in kinds:
                visit({**node, "type": k}, path, branches, emit, nullable or "null" in kinds)
            return
        rec = records.setdefault(path, {"path": path, "json_types": set(), "nullable": False,
                                        "memberships": {}, "sql_types": set(), "consts": {},
                                        "emitted": False, "nonconst": False, "observations": {}})
        rec["json_types"].update(kinds)
        rec["nullable"] |= nullable or "null" in kinds
        rec["memberships"][stable_json(branches)] = branches
        observation = {"branches": branches, "json_types": sorted(kinds),
                       "nullable": nullable or "null" in kinds}
        for key in ("format", "const", "enum", "required"):
            if key in node:
                observation[key] = node[key]
        rec["observations"][stable_json(observation)] = observation
        if "const" not in node and kind != "null":
            rec["nonconst"] = True
        if "const" in node:
            rec["consts"][stable_json(node["const"])] = node["const"]
        props = node.get("properties", {})
        extra = node.get("additionalProperties", False)
        if kind == "object" and props and extra is not False and path != column:
            raise SchemaError(f"{path}: nested properties with additionalProperties")
        leaf = opened or kind != "object" or not props
        if kind == "object" and not props and extra is not False and not is_open(extra):
            raise SchemaError(f"{path}: unsupported additionalProperties")
        if leaf and kind != "null":
            sql = "variant" if opened else SQL_TYPES[kind]
            if kind == "string":
                sql = {"date-time": "timestamp_tz", "date": "date"}.get(node.get("format"), sql)
            rec["sql_types"].add(sql)
            rec["emitted"] |= emit and path != column
        if kind == "object" and not opened:
            for key, child in sorted(props.items()):
                if "." in key or "[]" in key:
                    raise SchemaError(f"{path}: unsupported property name {key!r}")
                visit(child, f"{path}.{key}", branches, emit)
        if kind == "array" and not opened:
            visit(node.get("items", {}), path + "[]", branches, False)

    visit(schema, column, [])
    for path, rec in records.items():
        if rec["json_types"] == {"null"} and path != column and "[]" not in path:
            rec["emitted"] = True
            rec["sql_types"].add("variant")
    return records


def resolve(records, exceptions, version_key="_v", version_column="version"):
    renames = exceptions.get("rename", {})
    overrides = exceptions.get("type", {})
    for key in set(renames) | set(overrides):
        if key not in records:
            raise SchemaError(f"{key}: stale rename/type exception")
    output = []
    for path, rec in sorted(records.items()):
        types = sorted(rec["json_types"])
        sql_types = rec["sql_types"]
        override = overrides.get(path)
        if override:
            if set(override) != {"from", "to"} or sorted(override["from"]) != types:
                raise SchemaError(f"{path}: stale type override: source types are {types}")
            if override["to"] not in set(SQL_TYPES.values()) | {"date", "timestamp_tz"}:
                raise SchemaError(f"{path}: unsupported override SQL type")
            sql_types = {override["to"]}
        if len(sql_types) > 1:
            raise SchemaError(f"{path}: conflicting leaf types {sorted(sql_types)}")
        item = {"path": path, "json_types": types, "nullable": rec["nullable"],
                "branch_membership": [rec["memberships"][k] for k in sorted(rec["memberships"])],
                "observations": [rec["observations"][k] for k in sorted(rec["observations"])]}
        if rec["consts"] and not rec["nonconst"]:
            item["discriminator_values"] = [rec["consts"][k] for k in sorted(rec["consts"])]
        if rec["emitted"]:
            parts = path.split(".")
            parts = [version_column if p == version_key else snake(p) for p in parts]
            name = renames.get(path, "_".join(parts))
            if not re.fullmatch(r"[a-z_][a-z0-9_]*", name):
                raise SchemaError(f"{path}: invalid output column {name!r}")
            item.update(column=name, sql_type=next(iter(sql_types), "variant"))
        elif path in renames:
            raise SchemaError(f"{path}: rename targets a non-emitted path")
        if override:
            item["override"] = override
        output.append(item)
    return output
