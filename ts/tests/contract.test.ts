import { expect, test } from "bun:test";
import { integer, jsonb, pgSchema, pgTable, text } from "drizzle-orm/pg-core";
import { z } from "zod";
import {
  buildLandingContract,
  serializeLandingContract,
  toContractJsonSchema,
} from "../src/index";

test("custom leaves require a sample accepted by that exact schema", () => {
  const code = z.custom<string>((value) => value === "sample");
  const schema = z.object({ code });
  expect(
    toContractJsonSchema(schema, {
      customLeafSamples: new Map([[code, "sample"]]),
    }).properties?.code,
  ).toEqual({ type: "string" });
  for (const customLeafSamples of [
    undefined,
    new Map<z.core.$ZodType, string>(),
    new Map<z.core.$ZodType, string>([[code, "invalid"]]),
    new Map<z.core.$ZodType, string>([[z.custom(), "sample"]]),
  ]) {
    expect(() => toContractJsonSchema(schema, { customLeafSamples })).toThrow(
      "Unresolved custom JSON type at properties.code",
    );
  }
});

test("any and unknown leaves describe all six JSON types", () => {
  const schema = toContractJsonSchema(
    z.object({ any: z.any(), unknown: z.unknown() }),
  );
  const leaf: z.core.JSONSchema.JSONSchema = {
    type: ["string", "number", "boolean", "null", "object", "array"],
  };
  expect(schema.properties?.any).toEqual(leaf);
  expect(schema.properties?.unknown).toEqual(leaf);
});

test("unrepresentable leaves cannot silently become untyped", () => {
  expect(() => toContractJsonSchema(z.object({ date: z.date() }))).toThrow(
    "Untyped JSON schema leaf at properties.date",
  );
});

test("extracts SQL column names and types with exclusions and derived columns", () => {
  const widget = pgTable("widget", {
    title: text("display_title"),
    count: integer("count"),
    hidden: jsonb("hidden"),
  });
  const details = toContractJsonSchema(z.object({ color: z.string() }));
  expect(
    buildLandingContract([
      {
        table: widget,
        jsonb: { details, hidden: details },
        excludedColumns: ["hidden"],
        derivedColumns: [{ name: "details", sqlType: "jsonb" }],
      },
    ]),
  ).toEqual({
    tables: [
      {
        schema: "public",
        table: "widget",
        columns: [
          { name: "count", sqlType: "integer" },
          { name: "details", sqlType: "jsonb" },
          { name: "display_title", sqlType: "text" },
        ],
        jsonb: { details },
      },
    ],
  });
});

test("default schema survives contract serialization", () => {
  const contract = buildLandingContract([
    { table: pgTable("widget", { id: integer("id") }), jsonb: {} },
  ]);
  expect(contract.tables[0]?.schema).toBe("public");
  expect(JSON.parse(serializeLandingContract(contract)).tables[0].schema).toBe(
    "public",
  );
});

test("sorts tables by schema then name and columns by SQL name", () => {
  const alpha = pgSchema("alpha");
  const zeta = pgSchema("zeta");
  const tables = buildLandingContract([
    {
      table: zeta.table("apple", { z: text("z"), a: integer("a") }),
      jsonb: {},
    },
    {
      table: alpha.table("zebra", { z: text("z"), a: integer("a") }),
      jsonb: {},
    },
    {
      table: alpha.table("apple", { z: text("z"), a: integer("a") }),
      jsonb: {},
    },
  ]).tables;
  expect(tables.map(({ schema, table }) => `${schema}.${table}`)).toEqual([
    "alpha.apple",
    "alpha.zebra",
    "zeta.apple",
  ]);
  for (const table of tables)
    expect(table.columns.map(({ name }) => name)).toEqual(["a", "z"]);
});

test("serialization sorts nested object keys, preserves arrays, and ends with a newline", () => {
  const contract = {
    tables: [
      {
        table: "widget",
        schema: "inventory",
        columns: [{ sqlType: "text", name: "label" }],
        jsonb: {
          metadata: {
            type: "object" as const,
            properties: {
              z: { enum: ["z", "a", null] },
              a: { type: "string" as const },
            },
          },
        },
      },
    ],
  };
  const sorted = {
    tables: [
      {
        columns: [{ name: "label", sqlType: "text" }],
        jsonb: {
          metadata: {
            properties: {
              a: { type: "string" },
              z: { enum: ["z", "a", null] },
            },
            type: "object",
          },
        },
        schema: "inventory",
        table: "widget",
      },
    ],
  };
  expect(serializeLandingContract(contract)).toBe(
    `${JSON.stringify(sorted, null, 2)}\n`,
  );
});
