# drizzle-zod-contract

Build deterministic landing contracts from Drizzle PostgreSQL tables and Zod
schemas. Callers supply the tables, JSONB schemas, exclusions, derived columns,
and any custom string samples. No database connection is needed.

- `toContractJsonSchema(schema, options?)` converts a Zod schema to JSON Schema.
  Custom leaves require a matching, successfully parsed string in
  `options.customLeafSamples`. Any and unknown leaves accept all six JSON types;
  unresolved custom leaves and untyped leaves throw.
- `buildLandingContract(sources)` extracts SQL column names and types, applies
  optional exclusions and derived columns, and returns `{ tables }` sorted by
  schema/table and column name. Each source supplies `table` and `jsonb`.
- `serializeLandingContract(contract)` recursively sorts object keys and produces
  two-space JSON with a trailing newline, preserving array order. It runs no
  external formatter.

Example from a source checkout:

```ts
import { jsonb, pgTable, text } from "drizzle-orm/pg-core";
import { z } from "zod";
import {
  buildLandingContract,
  serializeLandingContract,
  toContractJsonSchema,
} from "./src/index.ts";

const widget = pgTable("widget", {
  label: text("label"),
  details: jsonb("details"),
});
const details = z.object({ color: z.string(), weight: z.number() });
const contract = buildLandingContract([
  { table: widget, jsonb: { details: toContractJsonSchema(details) } },
]);
console.log(serializeLandingContract(contract));
```

Drizzle ORM and Zod are peer dependencies. This source package is currently
private; its workspace test command runs the unit test file through the shared
runner.
