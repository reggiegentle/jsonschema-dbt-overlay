import copy
import json
from pathlib import Path

import pytest
import yaml

from jsonschema_dbt_overlay import SchemaError, check, generate, render
from jsonschema_dbt_overlay.walker import resolve, walk


def obj(**properties):
    return {"type": "object", "properties": properties, "additionalProperties": False}


def const(value, kind="number"):
    return {"type": kind, "const": value}


@pytest.fixture
def versions():
    return {"oneOf": [obj(_v=const(1), someKey={"type": "string"}),
                      obj(_v=const(2), anotherKey={"type": "boolean"})]}


@pytest.fixture
def objects():
    return {"anyOf": [obj(common={"type": "string"}, first={"type": "number"}),
                      obj(common={"type": "string"}, second={"type": "boolean"})]}


def paths(schema, exceptions=None):
    return {p["path"]: p for p in resolve(walk(schema, "payload"), exceptions or {})}


def test_root_versions(versions):
    result = paths(versions)
    assert result["payload._v"]["column"] == "payload_version"
    assert result["payload._v"]["discriminator_values"] == [1, 2]
    assert result["payload.someKey"]["branch_membership"] == [[{
        "path": "payload", "union": "oneOf", "discriminators": {"_v": 1}}]]


def test_second_discriminator():
    result = paths({"oneOf": [obj(_v=const(1), shape=const("weekly", "string")),
                              obj(_v=const(1), shape=const("extra", "string"))]})
    assert result["payload.shape"]["discriminator_values"] == ["extra", "weekly"]
    assert result["payload.shape"]["column"] == "payload_shape"


def test_single_branch_discriminator():
    assert paths(obj(_v=const(1)))["payload._v"]["discriminator_values"] == [1]


def test_nested_oneof(versions):
    result = paths(obj(inputs=versions))
    assert result["payload.inputs._v"]["discriminator_values"] == [1, 2]
    assert result["payload.inputs.someKey"]["branch_membership"][0][0]["path"] == "payload.inputs"


def test_nullable_anyof():
    item = paths(obj(value={"anyOf": [{"type": "string", "format": "date"}, {"type": "null"}]}))["payload.value"]
    assert item["nullable"] is True
    assert item["json_types"] == ["null", "string"]
    assert item["sql_type"] == "date"


def test_object_anyof_tracks_branch_membership(objects):
    before = paths(obj(value=objects))
    mutated = copy.deepcopy(objects)
    mutated["anyOf"][1]["properties"]["first"] = {"type": "number"}
    after = paths(obj(value=mutated))
    assert before["payload.value.first"]["column"] == after["payload.value.first"]["column"]
    assert before["payload.value.first"]["branch_membership"] != after["payload.value.first"]["branch_membership"]
    assert len(before["payload.value.common"]["branch_membership"]) == 2


def test_nondiscriminated_branch_signature():
    schema = {"anyOf": [{**obj(a={"type": "string"}), "required": ["a"]}, obj(b={"type": "string"})]}
    signature = paths(schema)["payload.a"]["branch_membership"][0][0]["signature"]
    assert signature == '0:{"const":{},"required":["a"]}'


def test_open_values_and_root_additional_properties():
    opened = {"type": ["string", "number", "boolean", "null", "object", "array"]}
    schema = {**obj(value=opened, facts={"type": "object", "additionalProperties": opened}),
              "additionalProperties": opened}
    result = paths(schema)
    assert "column" not in result["payload"]
    assert result["payload.value"]["sql_type"] == "variant"
    assert result["payload.facts"]["sql_type"] == "variant"
    assert len(result) == 3


def test_array_items_are_tracked_not_emitted():
    schema = obj(lines={"type": "array", "items": obj(productCode={"type": "string"})})
    result = paths(schema)
    assert result["payload.lines"]["sql_type"] == "variant"
    assert "column" not in result["payload.lines[].productCode"]
    assert result["payload.lines[].productCode"]["json_types"] == ["string"]


def test_camelcase_names():
    result = paths(obj(shipment=obj(source=obj(trackingId={"type": "string"}))))
    assert result["payload.shipment.source.trackingId"]["column"] == "payload_shipment_source_tracking_id"


@pytest.mark.parametrize("schema,path", [
    ({"oneOf": [obj(value={"type": "string"}), obj(value={"type": "number"})]}, "payload.value"),
    (obj(value={}), "payload.value"),
    (obj(value={"type": "object", "properties": {"x": {"type": "string"}}, "additionalProperties": True}), "payload.value"),
])
def test_generation_failures_name_path(schema, path):
    with pytest.raises(SchemaError, match=path):
        paths(schema)


def test_type_override_checks_source_types():
    schema = {"oneOf": [obj(value={"type": "string"}), obj(value={"type": "number"})]}
    override = {"type": {"payload.value": {"from": ["number", "string"], "to": "varchar"}}}
    assert paths(schema, override)["payload.value"]["sql_type"] == "varchar"
    schema["oneOf"][1]["properties"]["value"]["type"] = "boolean"
    with pytest.raises(SchemaError, match="stale type override"):
        paths(schema, override)


def test_partial_const_is_not_accepted_values_constraint():
    schema = {"anyOf": [obj(schedule={"type": "number"}), obj(schedule=const(0))]}
    assert "discriminator_values" not in paths(schema)["payload.schedule"]


@pytest.fixture
def files(tmp_path, versions):
    contract = {"tables": [{"schema": "app", "table": "events", "columns": [
        {"name": "id", "sqlType": "text"}, {"name": "payload", "sqlType": "jsonb"}],
        "jsonb": {"payload": versions}}]}
    config = {"model_prefix": "stg_demo__", "shim_prefix": "raw_demo__", "source_name": "demo",
              "output_dir": "models", "manifest": "manifest.json", "tags": ["layer:stg"],
              "audit_columns": {"processed_at": "current_timestamp()"}, "tables": {}}
    cp, yp = tmp_path / "contract.json", tmp_path / "config.yml"
    cp.write_text(json.dumps(contract)); yp.write_text(yaml.safe_dump(config))
    return cp, yp, contract, config


def save(files):
    cp, yp, contract, config = files
    cp.write_text(json.dumps(contract)); yp.write_text(yaml.safe_dump(config))
    return cp, yp


def test_generate_check_sql_and_yaml(files):
    cp, yp, _, _ = files
    generate(cp, yp)
    assert check(cp, yp) == []
    sql = (cp.parent / "models/stg_demo__events.sql").read_text()
    assert 'payload:"someKey"::varchar as payload_some_key' in sql
    assert 'try_cast(to_varchar(payload:"_v") as float) as payload_version' in sql
    assert "ref('raw_demo__events')" in sql
    assert "payload as payload" in sql
    models = yaml.safe_load((cp.parent / "models/_models.yml").read_text())
    assert models["models"][0]["columns"][0]["data_tests"][0]["accepted_values"]["arguments"]["values"] == [1, 2]
    for path in render(cp, yp):
        path.write_text("edited")
    assert len(check(cp, yp)) == 3


def test_property_double_quote_uses_snowflake_path_escaping(files):
    cp, _, contract, config = files
    contract["tables"][0]["jsonb"]["payload"] = obj(**{'a"b': {"type": "string"}})
    config["tables"] = {"events": {"rename": {'payload.a"b': "payload_a_b"}}}
    sql = render(*save(files))[cp.parent / "models/stg_demo__events.sql"]
    assert 'payload:"a""b"::varchar as payload_a_b' in sql


def test_collision_and_rename(files):
    _, _, contract, config = files
    contract["tables"][0]["jsonb"]["payload"] = obj(fooBar={"type": "string"}, foo_bar={"type": "string"})
    with pytest.raises(SchemaError, match="column collision"):
        render(*save(files))
    config["tables"] = {"events": {"rename": {"payload.fooBar": "payload_original_camel"}}}
    assert "payload_original_camel" in next(iter(render(*save(files)).values()))


@pytest.mark.parametrize("exception", [
    {"exclude": ["absent"]}, {"rename": {"payload.absent": "missing"}},
    {"type": {"payload.absent": {"from": ["string"], "to": "varchar"}}},
])
def test_stale_exceptions_fail(files, exception):
    files[3]["tables"] = {"events": exception}
    with pytest.raises(SchemaError, match="stale"):
        render(*save(files))


def test_version_membership_drift_without_sql_change(files):
    cp, yp, contract, _ = files
    generate(cp, yp)
    schema = contract["tables"][0]["jsonb"]["payload"]
    schema["oneOf"][1]["properties"]["someKey"] = {"type": "string"}
    diffs = check(*save(files))
    assert len(diffs) == 1 and "manifest.json" in diffs[0]


def test_array_item_type_drift_without_sql_change(files):
    cp, yp, contract, _ = files
    schema = obj(lines={"type": "array", "items": obj(code={"type": "string"})})
    contract["tables"][0]["jsonb"]["payload"] = schema
    generate(*save(files))
    schema["properties"]["lines"]["items"]["properties"]["code"]["type"] = "number"
    diffs = check(*save(files))
    assert len(diffs) == 1 and "manifest.json" in diffs[0]


def test_audit_collision(files):
    files[3]["audit_columns"] = {"id": "current_timestamp()"}
    with pytest.raises(SchemaError, match="column collision"):
        render(*save(files))


def test_unexpected_model(files):
    cp, yp, _, _ = files
    generate(cp, yp)
    (cp.parent / "models/stg_demo__obsolete.sql").write_text("select 1")
    assert "Unexpected:" in check(cp, yp)[0]


def test_array_item_format_drift(files):
    cp, yp, contract, _ = files
    item = {"type": "string", "format": "date"}
    contract["tables"][0]["jsonb"]["payload"] = obj(lines={"type": "array", "items": item})
    generate(*save(files))
    item["format"] = "date-time"
    assert "manifest.json" in check(*save(files))[0]


def test_null_only_leaf():
    item = paths(obj(value={"type": "null"}))["payload.value"]
    assert item["sql_type"] == "variant" and item["nullable"]


def test_missing_output(files):
    cp, yp, _, _ = files
    assert len(check(cp, yp)) == 3
    assert all(item.startswith("Missing:") for item in check(cp, yp))


def test_top_level_override_drift(files):
    cp, yp, contract, config = files
    contract["tables"][0]["columns"][0]["sqlType"] = "custom_enum"
    config["tables"] = {"events": {"type": {"id": {"from": ["custom_enum"], "to": "varchar"}}}}
    generate(*save(files))
    contract["tables"][0]["columns"][0]["sqlType"] = "text"
    with pytest.raises(SchemaError, match="stale type override"):
        check(*save(files))


def test_partial_const_union_uses_positional_signatures():
    schema = {"anyOf": [obj(schedule={"type": "number"}), obj(schedule=const(0))]}
    memberships = paths(schema)["payload.schedule"]["branch_membership"]
    assert {m[0]["signature"] for m in memberships} == {
        '0:{"const":{},"required":[]}', '1:{"const":{"schedule":0},"required":[]}'}


@pytest.mark.parametrize("encoding", ["anyOf", "type"])
def test_nullable_object_does_not_make_child_nullable(encoding):
    parent = obj(child={"type": "string"})
    if encoding == "anyOf":
        parent = {"anyOf": [parent, {"type": "null"}]}
    else:
        parent["type"] = ["object", "null"]
    result = paths(obj(parent=parent))
    assert result["payload.parent"]["nullable"] is True
    assert result["payload.parent.child"]["nullable"] is False
    assert all(not o["nullable"] for o in result["payload.parent.child"]["observations"])


@pytest.mark.parametrize("union", ["oneOf", "anyOf"])
@pytest.mark.parametrize("sibling", [
    {"properties": {"hidden": {"type": "string"}}},
    {"items": {"type": "string"}}, {"type": "object"},
    {"required": ["hidden"]}, {"additionalProperties": False},
    {"other_union": [obj()]},
])
def test_union_structural_siblings_fail(union, sibling):
    if "other_union" in sibling:
        sibling = {"anyOf" if union == "oneOf" else "oneOf": sibling["other_union"]}
    with pytest.raises(SchemaError, match="payload: .* has unsupported siblings"):
        paths({union: [obj(child={"type": "string"})], **sibling})


@pytest.mark.parametrize("union", ["oneOf", "anyOf"])
def test_union_annotations_allowed(union):
    schema = {union: [obj(child={"type": "string"})],
              "$schema": "https://json-schema.org/draft/2020-12/schema",
              "$comment": "annotation", "title": "Example", "description": "Example",
              "default": {}, "examples": [], "deprecated": False,
              "readOnly": True, "writeOnly": False}
    assert paths(schema)["payload.child"]["sql_type"] == "varchar"


@pytest.mark.parametrize("key", ["a.b", "a[]", "[]a"])
def test_ambiguous_property_names_fail(key):
    with pytest.raises(SchemaError, match="payload: unsupported property name"):
        paths(obj(**{key: {"type": "string"}}))


def test_stable_json_sorts_nested_keys():
    from jsonschema_dbt_overlay.walker import stable_json

    assert stable_json({"b": {"z": 1, "a": 2}, "a": 3}) == '{"a":3,"b":{"a":2,"z":1}}'


@pytest.mark.parametrize("constraint,before,after", [
    ("minimum", 0, 1), ("maximum", 10, 9), ("pattern", "^a", "^b"),
    ("enum", ["a", "b"], ["a"]), ("additionalProperties", False, True),
])
def test_constraint_only_drift_without_model_change(files, constraint, before, after):
    cp, yp, contract, _ = files
    schema = obj(value={"type": "number" if constraint in {"minimum", "maximum"} else "string"})
    target = schema if constraint == "additionalProperties" else schema["properties"]["value"]
    target[constraint] = before
    contract["tables"][0]["jsonb"]["payload"] = schema
    generate(*save(files))
    manifest_path = cp.parent / "manifest.json"
    old_digest = json.loads(manifest_path.read_text())["tables"][0]["jsonb_schema_sha256"]["payload"]
    target[constraint] = after
    assert check(*save(files)) == [f"Different: {manifest_path}"]
    new_manifest = json.loads(render(cp, yp)[manifest_path])
    new_digest = new_manifest["tables"][0]["jsonb_schema_sha256"]["payload"]
    assert len(new_digest) == 64 and new_digest != old_digest


def test_schema_digest_ignores_key_order(files):
    cp, yp, contract, _ = files
    generate(cp, yp)
    cp.write_text(json.dumps(contract, sort_keys=True))
    assert check(cp, yp) == []


def test_current_state_sql(files):
    cp, _, contract, config = files
    contract["tables"][0]["columns"].append({"name": "revision", "sqlType": "integer"})
    config["tables"] = {"events": {"key": ["id", "revision"]}}
    config["current_state"] = {"order_by": ["_change_seq", "_change_sub_seq"], "deleted_when": "_change_op = 'd'"}
    sql = render(*save(files))[cp.parent / "models/stg_demo__events.sql"]
    assert sql.endswith("qualify row_number() over (partition by id, revision "
                        "order by _change_seq desc, _change_sub_seq desc) = 1\n"
                        "    and not (_change_op = 'd')\n")
    assert "where " not in sql


def test_include_tables_limits_all_outputs(files):
    cp, _, contract, config = files
    other = copy.deepcopy(contract["tables"][0])
    other["table"] = "other"
    contract["tables"].append(other)
    config["include_tables"] = ["events"]
    outputs = render(*save(files))
    assert {p.name for p in outputs} == {"stg_demo__events.sql", "_models.yml", "manifest.json"}
    assert [t["table"] for t in json.loads(outputs[cp.parent / "manifest.json"])["tables"]] == ["events"]
    assert [m["name"] for m in yaml.safe_load(outputs[cp.parent / "models/_models.yml"])["models"]] == ["stg_demo__events"]


@pytest.mark.parametrize("key", [["absent"], [], ["id", "id"], "id"])
def test_invalid_key(files, key):
    files[3]["tables"] = {"events": {"key": key}}
    with pytest.raises(SchemaError, match="key"):
        render(*save(files))


@pytest.mark.parametrize("include", [["absent"], ["events", "events"], "events",
                                     None, [], ["events", "unknown"]])
def test_invalid_include_tables(files, include):
    files[3]["include_tables"] = include
    with pytest.raises(SchemaError, match="include_tables"):
        render(*save(files))


def test_current_state_requires_key(files):
    files[3]["current_state"] = {"order_by": ["_change_seq"], "deleted_when": "_change_op = 'd'"}
    with pytest.raises(SchemaError, match="requires a key"):
        render(*save(files))


@pytest.mark.parametrize("state", [None, {}, {"order_by": [], "deleted_when": "true"},
                                   {"order_by": ["_change_seq"], "deleted_when": ""}])
def test_invalid_current_state(files, state):
    files[3]["current_state"] = state
    with pytest.raises(SchemaError, match="current_state"):
        render(*save(files))


def test_scalar_table_opt_in_and_derived_hash(files):
    cp, _, contract, config = files
    contract["tables"][0]["jsonb"] = {}
    contract["tables"][0]["columns"] = [{"name": "id", "sqlType": "text"}]
    config["tables"]["events"] = {
        "generate": True,
        "derived_columns": {"evt_hash": "{{ generate_hash('evt', ['id']) }}"},
    }
    sql = render(*save(files))[cp.parent / "models/stg_demo__events.sql"]
    assert "id::varchar as id" in sql
    assert "{{ generate_hash('evt', ['id']) }} as evt_hash" in sql
    config["tables"]["events"]["derived_columns"] = {"id": "'collision'"}
    with pytest.raises(SchemaError, match="column collision"):
        render(*save(files))


@pytest.mark.parametrize("values,expected", [
    (["O'Reilly"], {"values": ["O''Reilly"], "quote": True}),
    (["alpha\\nbeta"], {"values": ["alpha\\\\nbeta"], "quote": True}),
    (["alpha\\"], {"values": ["alpha\\\\"], "quote": True}),
    (["a", 1], None),
    (["a", None], {"values": ["a"], "quote": True}),
    ([1, 2], {"values": [1, 2], "quote": False}),
    ([True, False], {"values": [False, True], "quote": False}),
    ([None], None),
])
def test_discriminator_accepted_values(files, values, expected):
    cp, _, contract, config = files
    kinds = {str: "string", int: "number", bool: "boolean", type(None): "null"}
    contract["tables"][0]["jsonb"]["payload"] = {
        "oneOf": [obj(kind=const(value, kinds[type(value)])) for value in values]}
    if len({kinds[type(value)] for value in values} - {"null"}) > 1:
        config["tables"] = {"events": {"type": {"payload.kind": {
            "from": sorted({kinds[type(value)] for value in values}), "to": "variant"}}}}
    outputs = render(*save(files))
    columns = yaml.safe_load(outputs[cp.parent / "models/_models.yml"])["models"][0]["columns"]
    assert columns == ([] if expected is None else [{
        "name": "payload_kind", "data_tests": [{"accepted_values": {"arguments": expected}}]}])
    manifest = json.loads(outputs[cp.parent / "manifest.json"])
    item = next(p for p in manifest["tables"][0]["paths"] if p["path"] == "payload.kind")
    assert item["discriminator_values"] == sorted(values, key=json.dumps)


@pytest.mark.parametrize("name", ["order", "case", "join", "schema"])
def test_reserved_relational_column(files, name):
    files[2]["tables"][0]["columns"].append({"name": name, "sqlType": "text"})
    with pytest.raises(SchemaError, match=f"Reserved SQL identifier '{name}'; exclude it in config"):
        render(*save(files))


def test_excluded_reserved_relational_column(files):
    cp, _, contract, config = files
    contract["tables"][0]["columns"].append({"name": "order", "sqlType": "text"})
    config["tables"] = {"events": {"exclude": ["order"]}}
    outputs = render(*save(files))
    assert " as order" not in outputs[cp.parent / "models/stg_demo__events.sql"]
    manifest = json.loads(outputs[cp.parent / "manifest.json"])
    entry = next(c for c in manifest["tables"][0]["columns"] if c["name"] == "order")
    assert entry["included"] is False


def test_reserved_output_column_and_rename(files):
    cp, _, _, config = files
    config["tables"] = {"events": {"rename": {"payload.someKey": "order"}}}
    with pytest.raises(SchemaError, match="Reserved SQL identifier 'order'; rename it in config"):
        render(*save(files))
    config["tables"]["events"]["rename"]["payload.someKey"] = "order_name"
    sql = render(*save(files))[cp.parent / "models/stg_demo__events.sql"]
    assert 'payload:"someKey"::varchar as order_name' in sql


def test_custom_version_names(files):
    cp, _, contract, config = files
    config.update(version_key="_rev", version_column="revision")
    contract["tables"][0]["jsonb"]["payload"] = obj(
        nestedValue=obj(_rev=const(1), _v=const(2), someKey={"type": "string"}))
    outputs = render(*save(files))
    sql = outputs[cp.parent / "models/stg_demo__events.sql"]
    assert 'payload:"nestedValue"."_rev") as float) as payload_nested_value_revision' in sql
    assert 'payload:"nestedValue"."_v") as float) as payload_nested_value__v' in sql
    assert 'payload:"nestedValue"."someKey"::varchar as payload_nested_value_some_key' in sql
    manifest = json.loads(outputs[cp.parent / "manifest.json"])
    item = next(p for p in manifest["tables"][0]["paths"] if p["path"] == "payload.nestedValue._rev")
    assert item["column"] == "payload_nested_value_revision"


@pytest.mark.parametrize("key", ["version_key", "version_column"])
@pytest.mark.parametrize("value", ["", "   ", None, 1, False, [], {}])
def test_invalid_version_option(files, key, value):
    files[3][key] = value
    with pytest.raises(SchemaError, match=key):
        render(*save(files))


@pytest.mark.parametrize("value", ["select", "ORDER", "has-dash", "CamelCase", "1version"])
def test_invalid_version_column_identifier(files, value):
    files[3]["version_column"] = value
    with pytest.raises(SchemaError, match="SQL identifier"):
        render(*save(files))
