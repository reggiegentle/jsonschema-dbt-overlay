import { getTableColumns } from "drizzle-orm";
import { getTableConfig, type PgTable } from "drizzle-orm/pg-core";
import { z } from "zod";

type JsonType = NonNullable<z.core.JSONSchema.JSONSchema["type"]>;

export function toContractJsonSchema(
  schema: z.ZodType,
  options?: { customLeafSamples?: Map<z.core.$ZodType, string> },
) {
  return z.toJSONSchema(schema, {
    unrepresentable: "any",
    override: ({ zodSchema, jsonSchema, path }) => {
      const type = zodSchema._zod.def.type;
      if (type === "custom") {
        const sample = options?.customLeafSamples?.get(zodSchema);
        if (sample === undefined || !z.safeParse(zodSchema, sample).success) {
          throw new Error(`Unresolved custom JSON type at ${path.join(".")}`);
        }
        jsonSchema.type = "string";
      }
      if (type === "any" || type === "unknown") {
        // JSON Schema permits type arrays; Zod's override type only lists scalars.
        const leaf: { type?: JsonType | JsonType[] } = jsonSchema;
        leaf.type = ["string", "number", "boolean", "null", "object", "array"];
      }
      if (Object.keys(jsonSchema).length === 0) {
        throw new Error(`Untyped JSON schema leaf at ${path.join(".")}`);
      }
    },
  });
}

export function buildLandingContract(
  sources: {
    table: PgTable;
    jsonb: Record<string, z.core.JSONSchema.JSONSchema>;
    excludedColumns?: string[];
    derivedColumns?: { name: string; sqlType: string }[];
  }[],
) {
  return {
    tables: sources
      .map(({ table, jsonb, excludedColumns = [], derivedColumns = [] }) => {
        const config = getTableConfig(table);
        return {
          schema: config.schema ?? "public",
          table: config.name,
          columns: [
            ...Object.values(getTableColumns(table))
              .filter((column) => !excludedColumns.includes(column.name))
              .map((column) => ({
                name: column.name,
                sqlType: column.getSQLType(),
              })),
            ...derivedColumns,
          ].sort((a, b) => a.name.localeCompare(b.name)),
          jsonb: Object.fromEntries(
            Object.entries(jsonb).filter(
              ([name]) => !excludedColumns.includes(name),
            ),
          ),
        };
      })
      .sort((a, b) =>
        `${a.schema}.${a.table}`.localeCompare(`${b.schema}.${b.table}`),
      ),
  };
}

export function serializeLandingContract(
  contract: ReturnType<typeof buildLandingContract>,
): string {
  return `${JSON.stringify(
    contract,
    (_key, value) => {
      if (
        value !== null &&
        typeof value === "object" &&
        !Array.isArray(value)
      ) {
        return Object.fromEntries(
          Object.entries(value).sort(([a], [b]) => a.localeCompare(b)),
        );
      }
      return value;
    },
    2,
  )}\n`;
}
