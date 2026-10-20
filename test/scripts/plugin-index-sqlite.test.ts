import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import { pathToFileURL } from "node:url";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { withEnvAsync } from "../../src/test-utils/env.js";
import { useAutoCleanupTempDirTracker } from "../helpers/temp-dir.js";

const MODULE_URL = pathToFileURL(path.resolve("scripts/e2e/lib/plugin-index-sqlite.mjs")).href;
let importCounter = 0;
const tempRoots = useAutoCleanupTempDirTracker(afterEach);

async function loadPluginIndex(env: Record<string, string> = {}) {
  return withEnvAsync(env, () => import(`${MODULE_URL}?case=${importCounter++}`));
}

function sqlitePath(root: string) {
  return path.join(root, "state", "openclaw.sqlite");
}

function openSqlite(root: string) {
  const dbPath = sqlitePath(root);
  mkdirSync(path.dirname(dbPath), { recursive: true });
  return new DatabaseSync(dbPath);
}

function writeLegacyIndex(root: string, text: string) {
  const file = path.join(root, "plugins", "installs.json");
  mkdirSync(path.dirname(file), { recursive: true });
  writeFileSync(file, text, "utf8");
}

function currentValueJson(installRecords: unknown) {
  return JSON.stringify({ revision: Date.now(), index: { installRecords } });
}

function writeCurrentSqliteIndex(root: string, valueJson: string, schemaVersion = 13) {
  const db = openSqlite(root);
  try {
    db.exec(`
      PRAGMA user_version = ${schemaVersion};
      CREATE TABLE IF NOT EXISTS config_machine_state (
        state_key TEXT NOT NULL PRIMARY KEY,
        value_json TEXT NOT NULL,
        updated_at_ms INTEGER NOT NULL
      );
    `);
    db.prepare(
      `
        INSERT INTO config_machine_state (state_key, value_json, updated_at_ms)
        VALUES (?, ?, ?)
        ON CONFLICT(state_key) DO UPDATE SET value_json = excluded.value_json
      `,
    ).run("plugins.installedIndex", valueJson, Date.now());
  } finally {
    db.close();
  }
}

function writePreV13SqliteIndex(
  root: string,
  installRecordsJson: string,
  schemaVersion = 12,
  pluginsJson = "[]",
) {
  const db = openSqlite(root);
  try {
    db.exec(`
      PRAGMA user_version = ${schemaVersion};
      CREATE TABLE IF NOT EXISTS installed_plugin_index (
        index_key TEXT NOT NULL PRIMARY KEY, version INTEGER NOT NULL,
        host_contract_version TEXT NOT NULL, compat_registry_version TEXT NOT NULL,
        migration_version INTEGER NOT NULL, policy_hash TEXT NOT NULL,
        generated_at_ms INTEGER NOT NULL, refresh_reason TEXT,
        install_records_json TEXT NOT NULL, plugins_json TEXT NOT NULL,
        diagnostics_json TEXT NOT NULL, warning TEXT, updated_at_ms INTEGER NOT NULL
      );
    `);
    db.prepare(
      `
        INSERT INTO installed_plugin_index (
          index_key, version, host_contract_version, compat_registry_version,
          migration_version, policy_hash, generated_at_ms, refresh_reason,
          install_records_json, plugins_json, diagnostics_json, warning, updated_at_ms
        ) VALUES ('installed-plugin-index', 1, '1', '1', 1, 'hash', ?, NULL,
          ?, ?, '[]', NULL, ?)
        ON CONFLICT(index_key) DO UPDATE SET
          install_records_json = excluded.install_records_json,
          plugins_json = excluded.plugins_json
      `,
    ).run(Date.now(), installRecordsJson, pluginsJson, Date.now());
  } finally {
    db.close();
  }
}

function readTableNames(root: string) {
  const db = new DatabaseSync(sqlitePath(root), { readOnly: true });
  try {
    return (
      db
        .prepare("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
        .all() as Array<{ name: string }>
    ).map((row) => row.name);
  } finally {
    db.close();
  }
}

describe("plugin index SQLite E2E helpers", () => {
  let root: string;
  let options: { stateDir: string; configPath: string };
  const readRecords = async () => (await loadPluginIndex()).readPluginInstallRecords(options);
  beforeEach(() => {
    root = tempRoots.make("openclaw-plugin-index-");
    options = { stateDir: root, configPath: path.join(root, "openclaw.json") };
  });
  it("reads legacy install records when SQLite index state is absent", async () => {
    writeLegacyIndex(
      root,
      JSON.stringify({ records: { demo: { installPath: "/tmp/demo", source: "npm" } } }),
    );

    expect(await readRecords()).toEqual({
      demo: { installPath: "/tmp/demo", source: "npm" },
    });
  });

  it("keeps malformed legacy install JSON as an empty fallback", async () => {
    writeLegacyIndex(root, "{not-json");
    expect(await readRecords()).toEqual({});
  });

  it("rejects oversized legacy install JSON before parsing it", async () => {
    writeLegacyIndex(root, JSON.stringify({ records: {}, filler: "x".repeat(128) }));

    const { readPluginInstallRecords } = await loadPluginIndex({
      OPENCLAW_PLUGIN_INDEX_JSON_MAX_BYTES: "64",
    });

    expect(() => readPluginInstallRecords(options)).toThrow(
      "plugin index JSON artifact exceeded 64 bytes",
    );
  });

  it("reads the current index for schema v13", async () => {
    writeCurrentSqliteIndex(root, currentValueJson({ current: { source: "npm" } }));

    expect(await readRecords()).toEqual({
      current: { source: "npm" },
    });
  });

  it("reads the retired index for pre-v13 schemas", async () => {
    writePreV13SqliteIndex(root, JSON.stringify({ legacy: { source: "npm" } }));
    const index = await loadPluginIndex();
    expect(index.readPluginInstallIndex(options)).toMatchObject({
      installRecords: { legacy: { source: "npm" } },
    });
  });

  it("prefers current state for unversioned databases", async () => {
    writePreV13SqliteIndex(root, JSON.stringify({ stale: { source: "npm" } }), 0);
    writeCurrentSqliteIndex(root, currentValueJson({ current: { source: "npm" } }), 0);

    expect(await readRecords()).toEqual({
      current: { source: "npm" },
    });
  });

  it("does not fall back to a retired row for schema v13", async () => {
    writePreV13SqliteIndex(root, JSON.stringify({ stale: { source: "npm" } }), 13);

    expect(await readRecords()).toEqual({});
  });

  it("does not read current state for pre-v13 schemas", async () => {
    writeCurrentSqliteIndex(root, currentValueJson({ future: { source: "npm" } }), 12);

    expect(await readRecords()).toEqual({});
  });

  it.each([
    {
      name: "current",
      write: () => writeCurrentSqliteIndex(root, "{not-json"),
    },
    {
      name: "pre-v13",
      write: () => writePreV13SqliteIndex(root, "{not-json"),
    },
  ])("keeps malformed $name SQLite state as an empty fallback", async ({ write }) => {
    write();

    expect(await readRecords()).toEqual({});
  });

  it.each([
    {
      name: "current",
      expected: "plugin index value_json exceeded 64 bytes",
      write: () => writeCurrentSqliteIndex(root, currentValueJson({ filler: "x".repeat(128) })),
    },
    {
      name: "pre-v13",
      expected: "plugin index install_records_json exceeded 64 bytes",
      write: () => writePreV13SqliteIndex(root, JSON.stringify({ filler: "x".repeat(128) })),
    },
  ])("rejects oversized $name SQLite state before parsing it", async ({ expected, write }) => {
    write();

    const { readPluginInstallIndex } = await loadPluginIndex({
      OPENCLAW_PLUGIN_INDEX_JSON_MAX_BYTES: "64",
    });

    expect(() => readPluginInstallIndex(options)).toThrow(expected);
  });

  it("measures the native Doctor budget without reading unrelated oversized catalogue metadata", async () => {
    const installRecords = {
      demo: {
        source: "npm",
        installPath: "/plugins/demo",
        version: "1.2.3",
        receipt: { unknownFutureKey: ["keep", { enabled: false }] },
      },
    };
    const plugins = [
      { pluginId: "off", enabled: false, rootDir: "/plugins/off" },
      { pluginId: "on", enabled: true, rootDir: "/plugins/on" },
      { pluginId: "duplicate-root", enabled: true, rootDir: "/plugins/on" },
      { pluginId: "null", enabled: null, rootDir: null },
      { pluginId: "missing" },
    ];
    writeCurrentSqliteIndex(
      root,
      JSON.stringify({
        revision: 1,
        index: {
          installRecords,
          plugins: plugins.map((plugin) => ({
            ...plugin,
            packageInstall: { metadataPadding: "x".repeat(300_000) },
          })),
        },
      }),
    );
    for (const agentId of ["main", "ops"]) {
      const file = path.join(root, "agents", agentId, "agent", "openclaw-agent.sqlite");
      mkdirSync(path.dirname(file), { recursive: true });
      const db = new DatabaseSync(file);
      try {
        db.exec("CREATE TABLE session_nodes(id); CREATE TABLE transcript_events(id);");
        db.exec(
          "INSERT INTO session_nodes VALUES (1); INSERT INTO transcript_events VALUES (1), (2);",
        );
      } finally {
        db.close();
      }
    }
    const db = openSqlite(root);
    try {
      db.exec("CREATE TABLE cron_jobs(id); INSERT INTO cron_jobs VALUES (1), (2), (3);");
    } finally {
      db.close();
    }
    const volumeUrl = pathToFileURL(
      path.resolve("scripts/e2e/lib/upgrade-survivor/sqlite-volume.mjs"),
    ).href;
    const { measureVolumeDoctorBudget } = await import(volumeUrl);
    // This production fixture consumer throws ETOOBIG on the unchanged base reader.
    expect(measureVolumeDoctorBudget(root)).toEqual({
      counts: { sessions: 2, events: 4, cronJobs: 3, pluginRoots: 1 },
      computedSeconds: 81,
    });
    const index = await loadPluginIndex();
    const summary = index.readPluginInstallSummary(options);
    expect(summary.installRecords).toEqual(installRecords);
    expect(summary.plugins).toEqual(plugins);
    expect(summary.readBytes.raw).toBeGreaterThan(1024 * 1024);
    expect(summary.readBytes.summary).toBeLessThan(1024 * 1024);
    expect(summary.readBytes.pluginCount).toBe(5);
    expect(() => index.readPluginInstallIndex(options)).toThrow(
      "plugin index value_json exceeded 1048576 bytes",
    );
  });

  it("projects retired schemas while retaining the full reader quota", async () => {
    const records = { demo: { source: "npm", future: { consent: [true, false, null] } } };
    const plugins = [
      { pluginId: "demo", enabled: false, rootDir: "/demo" },
      { pluginId: "missing" },
    ];
    writePreV13SqliteIndex(
      root,
      JSON.stringify(records),
      12,
      JSON.stringify(plugins.map((plugin) => ({ ...plugin, catalogue: "x".repeat(600_000) }))),
    );
    const index = await loadPluginIndex();
    expect(index.readPluginInstallSummary(options)).toMatchObject({
      installRecords: records,
      plugins,
    });
    expect(() => index.readPluginInstallIndex(options)).toThrow(
      "plugin index plugins_json exceeded 1048576 bytes",
    );
  });

  it.each(["receipts", "plugin roots"])(
    "rejects oversized required %s without returning the projection",
    async (field) => {
      writeCurrentSqliteIndex(
        root,
        JSON.stringify({
          revision: 1,
          index: {
            installRecords:
              field === "receipts" ? { demo: { future: "x".repeat(1024 * 1024) } } : {},
            plugins: [
              {
                pluginId: "demo",
                enabled: true,
                rootDir: field === "plugin roots" ? "x".repeat(1024 * 1024) : "/demo",
              },
            ],
          },
        }),
      );
      const index = await loadPluginIndex();
      expect(() => index.readPluginInstallSummary(options)).toThrow(
        "plugin index summary exceeded 1048576 bytes",
      );
    },
  );

  it("bounds required projection bytes rather than UTF-8 character count", async () => {
    const required = { installRecords: { demo: { future: "é".repeat(600_000) } }, plugins: [] };
    const text = JSON.stringify(required);
    expect(text.length).toBeLessThan(1024 * 1024);
    expect(Buffer.byteLength(text, "utf8")).toBeGreaterThan(1024 * 1024);
    writeCurrentSqliteIndex(root, JSON.stringify({ revision: 1, index: required }));
    const index = await loadPluginIndex();
    expect(() => index.readPluginInstallSummary(options)).toThrow(
      "plugin index summary exceeded 1048576 bytes",
    );
  });

  it("keeps summary reads on the committed WAL snapshot", async () => {
    writeCurrentSqliteIndex(root, currentValueJson({ stale: {} }));
    const writer = openSqlite(root);
    try {
      writer.exec("PRAGMA journal_mode=WAL;");
      writer.prepare("UPDATE config_machine_state SET value_json = ? WHERE state_key = ?").run(
        JSON.stringify({
          revision: 2,
          index: {
            installRecords: { committed: { future: "preserved" } },
            plugins: [],
            diagnostics: ["x".repeat(1024 * 1024)],
          },
        }),
        "plugins.installedIndex",
      );
      writer.exec("BEGIN IMMEDIATE;");
      writer
        .prepare("UPDATE config_machine_state SET value_json = ? WHERE state_key = ?")
        .run(currentValueJson({ uncommitted: {} }), "plugins.installedIndex");
      const index = await loadPluginIndex();
      expect(index.readPluginInstallSummary(options).installRecords).toEqual({
        committed: { future: "preserved" },
      });
      expect(() => index.readPluginInstallIndex(options)).toThrow(
        "plugin index value_json exceeded 1048576 bytes",
      );
    } finally {
      writer.exec("ROLLBACK");
      writer.close();
    }
  });

  it.each([
    { schema: 0, current: true, retired: true, expected: { current: {} } },
    { schema: 0, current: false, retired: true, expected: { retired: {} } },
    { schema: 12, current: true, retired: false, expected: {} },
    { schema: 13, current: false, retired: true, expected: {} },
  ])(
    "keeps summary schema boundaries for $schema/current=$current/retired=$retired",
    async ({ schema, current, retired, expected }) => {
      if (retired) {
        writePreV13SqliteIndex(root, JSON.stringify({ retired: {} }), schema);
      }
      if (current) {
        writeCurrentSqliteIndex(root, currentValueJson({ current: {} }), schema);
      }
      const index = await loadPluginIndex();
      expect(index.readPluginInstallSummary(options).installRecords).toEqual(expected);
    },
  );

  it("falls back from malformed SQLite without trusting legacy diagnostic fields", async () => {
    writeCurrentSqliteIndex(root, "{not-json");
    writeLegacyIndex(
      root,
      JSON.stringify({
        records: { legacy: { custom: [false, null] } },
        plugins: [
          { pluginId: "demo", enabled: false },
          { pluginId: "missing", rootDir: null },
        ],
        readBytes: { raw: "untrusted" },
      }),
    );
    const index = await loadPluginIndex();
    expect(index.readPluginInstallSummary(options)).toEqual({
      installRecords: { legacy: { custom: [false, null] } },
      plugins: [
        { pluginId: "demo", enabled: false },
        { pluginId: "missing", rootDir: null },
      ],
    });
  });

  it.each([
    { mode: "current", schemaVersion: 0, options: {}, current: true },
    { mode: "pre-v13", schemaVersion: 0, options: { storageMode: "pre-v13" }, current: false },
    ...[0, 12, 13].map((schemaVersion) => ({
      mode: `existing schema v${schemaVersion}`,
      schemaVersion,
      options: { storageMode: "existing-schema" },
      current: schemaVersion >= 13,
    })),
  ])(
    "writes only the $mode storage contract",
    async ({ current, options: writerOptions, schemaVersion }) => {
      const db = openSqlite(root);
      db.exec(`PRAGMA user_version = ${schemaVersion}`);
      db.close();
      const index = await loadPluginIndex();
      index.writePluginInstallIndexForE2E(
        { installRecords: { demo: { source: "npm" } } },
        { stateDir: root, ...writerOptions },
      );

      const tables = readTableNames(root);
      expect(tables).toContain(current ? "config_machine_state" : "installed_plugin_index");
      expect(tables).not.toContain(current ? "installed_plugin_index" : "config_machine_state");
      expect(index.readPluginInstallRecords({ stateDir: root })).toEqual({
        demo: { source: "npm" },
      });
    },
  );

  it("rejects unknown writer storage modes", async () => {
    const index = await loadPluginIndex();
    expect(() =>
      index.writePluginInstallIndexForE2E(
        { installRecords: {} },
        { stateDir: root, storageMode: "future" },
      ),
    ).toThrow("Unknown plugin index storage mode: future");
  });
});
