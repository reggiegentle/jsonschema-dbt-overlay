# JSON Schema dbt overlay

A standalone Python 3.10+ package with one runtime dependency, PyYAML. It reads a
landing contract and emits Snowflake staging SQL, dbt discriminator tests, and a
path manifest. It does not connect to a database or import an ingestion system.

## Packages

- The repository root contains the Python generator.
- [`ts/`](ts/) contains `drizzle-zod-contract`, which builds the JSON Schema landing
  contract the generator reads from Drizzle tables and Zod schemas.

From the package directory:

```sh
pip install -e .
python -m jsonschema_dbt_overlay generate --contract contract.json --config overlay.yml
python -m jsonschema_dbt_overlay check --contract contract.json --config overlay.yml
pip install pytest
pytest
```

The public API exports `render(contract, config)` (output path to text),
`generate(contract, config)` (write all outputs), and `check(contract, config)`
(list differing, missing, or unexpected artifacts). Invalid schemas/configuration
raise `SchemaError`; the CLI returns 1 for invalid inputs or drift and 0 on success.
Generation renders everything before writing, so schema errors leave files intact.
It does not remove obsolete models: `check` reports them for explicit review.

Contracts contain `tables[]` with `schema`, `table`, `columns[]` (`name`, `sqlType`),
and `jsonb` mapping column names to inline JSON Schema 2020-12 schemas. A model is
emitted for every table with a `jsonb` mapping; all tables enter the manifest.
References and composition other than `oneOf`/`anyOf` are rejected. Arrays retain
the source grain and are projected whole as VARIANT; their item paths are still
walked, type checked, and recorded. Open six-type values and open dictionaries
stay VARIANT. Nested objects combining named properties and open additional
properties are rejected. Root additional properties remain in the whole column.

`oneOf` and object `anyOf` branches contribute a union of paths. Discriminator accepted values are emitted
only when every non-null occurrence of that path is constant. A partially
constant union (for example a numeric range in one branch and const 0 in another)
therefore does not receive an incorrectly restrictive accepted-values test.
Accepted-values tests drop nulls, double single quotes in all-string values with `quote: true`, use `quote: false` for all-number (excluding booleans) or all-boolean values, and omit mixed or empty lists while preserving the original discriminator values in the manifest.
Every cast other than VARCHAR uses `try_cast(to_varchar(expr) as type)`; VARIANT
projections retain the original value. Unknown relational SQL types require an
explicit override rather than an inferred string cast.

Configuration contains:

- `model_prefix`, `shim_prefix`, `source_name`, `tags`, and `audit_columns`
  (output alias to SQL expression). The source name documents the separately
  authored RAW source; generated STG models reference shims.
- `output_dir` and `manifest`, resolved relative to the config file.
- `version_key` (default `"_v"`) and `version_column` (default `"version"`)
  are optional nonempty strings. A path segment equal to `version_key` becomes
  `version_column`; all other segments use snake_case. `version_column` must be
  a valid output SQL identifier.
- `tables`, keyed by table name, containing `exclude`, `rename`, `type`, and optional `key`.
  Excludes name relational columns. Renames map full paths such as
  `payload.someKey` to output column names. Type overrides map those paths to
  `{from: [integer, number], to: 'number(38,0)'}`; the exact source type set must
  continue to match. Relational column overrides use the column name and its
  exact `sqlType` in `from` (for example a named PostgreSQL enum).

SQL identifiers matching Snowflake or ANSI reserved keywords are rejected case-insensitively and must be renamed in config.

The manifest records a SHA-256 digest of each full JSONB schema serialized as
sorted-key JSON, included/excluded relational columns, and every JSON path,
including containers and array items (`payload.lines[].code`). Each path records
source types, explicit JSON nullability, branch memberships, per-branch type and
format observations, constant values, and any applied override. Non-discriminated
branches use their position and sorted required/const signature. This catches
changes confined to one version or branch even when generated SQL is unchanged.
Nullability describes allowed JSON nulls, not SQL nulls arising from absent keys.

Regenerate after reviewing contract changes and any exception updates, then
commit SQL, YAML, and manifest together with the contract and its upstream SHA.
Hand edits to generated artifacts fail `check`. Keep source-specific names and
exceptions in config; the package itself has no source-system dependencies.

Optional `include_tables` lists contract table names and limits both models and
manifest entries. Unknown or repeated names fail validation. Omit it to retain
all tables. Tables without JSONB still enter the manifest without a model.

Optional `current_state` requires `order_by` (a nonempty list of source column
names, each ordered descending) and `deleted_when` (a trusted SQL expression).
Every selected model must have a per-table `key`, a nonempty list of distinct
contract columns. Keys are source columns, even when excluded from projections.
The model applies `qualify row_number() over (partition by <key> order by
<order_by desc>) = 1 and not (<deleted_when>)` after projection. Delete filtering
therefore does not bring back an older row. Ordering columns may be landing
metadata absent from the contract. Without these options, output is unchanged.

```yaml
include_tables: [events]
current_state:
  order_by: [_change_seq, _change_sub_seq]
  deleted_when: "_change_op = 'd'"
tables:
  events:
    key: [id]
```
