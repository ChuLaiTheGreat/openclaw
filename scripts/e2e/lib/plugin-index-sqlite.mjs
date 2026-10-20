// SQLite readers for plugin install indexes produced during E2E scenarios.
import fs from "node:fs";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import { readPositiveIntEnv } from "./env-limits.mjs";
import {
  resolveOpenClawConfigPath as configPath,
  resolveOpenClawStateDir as stateDir,
} from "./openclaw-state-paths.mjs";
import { readTextFileBounded } from "./text-file-utils.mjs";

const STATE_KEY = "plugins.installedIndex";
const PRE_V13_INDEX_KEY = "installed-plugin-index";
const ERROR_DETAIL_TAIL_BYTES = 16 * 1024;
const JSON_ARTIFACT_MAX_BYTES = readPositiveIntEnv(
  "OPENCLAW_PLUGIN_INDEX_JSON_MAX_BYTES",
  1024 * 1024,
);

function readJsonMaybe(file) {
  try {
    const text = readTextFileBounded(file, "plugin index JSON artifact", JSON_ARTIFACT_MAX_BYTES, {
      tailBytes: ERROR_DETAIL_TAIL_BYTES,
    });
    return JSON.parse(text);
  } catch (error) {
    if (error?.code === "ETOOBIG") {
      throw error;
    }
    return {};
  }
}

function textTooLargeError(message) {
  return Object.assign(new Error(message), { code: "ETOOBIG" });
}

function parseIndexJsonText(text, label) {
  assertIndexJsonByteLength(Buffer.byteLength(text, "utf8"), label);
  return JSON.parse(text);
}

function assertIndexJsonByteLength(bytesRaw, label) {
  const bytes = Number(bytesRaw);
  if (!Number.isFinite(bytes) || bytes < 0) {
    throw new Error(`${label} byte length was invalid: ${String(bytesRaw)}`);
  }
  if (bytes > JSON_ARTIFACT_MAX_BYTES) {
    throw textTooLargeError(`${label} exceeded ${JSON_ARTIFACT_MAX_BYTES} bytes (${bytes} bytes)`);
  }
}

function sqlitePath(root = stateDir()) {
  return path.join(root, "state", "openclaw.sqlite");
}

function legacyIndexPath(root = stateDir()) {
  return path.join(root, "plugins", "installs.json");
}

function tableExists(db, tableName) {
  return Boolean(
    db.prepare("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?").get(tableName),
  );
}

function readBoundedIndexRow(db, table, keyColumn, key, jsonColumns, columns) {
  if (!tableExists(db, table)) {
    return undefined;
  }
  const source = `FROM ${table} WHERE ${keyColumn} = ?`;
  const lengths = db
    .prepare(
      `SELECT ${jsonColumns.map((column) => `octet_length(${column}) AS ${column}_bytes`).join(", ")} ${source}`,
    )
    .get(key);
  if (!lengths) {
    return undefined;
  }
  for (const column of jsonColumns) {
    assertIndexJsonByteLength(lengths[`${column}_bytes`], `plugin index ${column}`);
  }
  return db.prepare(`SELECT ${columns} ${source}`).get(key);
}

function readCurrentSqlitePluginIndex(db) {
  const row = readBoundedIndexRow(
    db,
    "config_machine_state",
    "state_key",
    STATE_KEY,
    ["value_json"],
    "value_json",
  );
  if (!row) {
    return {};
  }
  const value = parseIndexJsonText(row.value_json, "plugin index value_json");
  return value?.index && typeof value.index === "object" ? value.index : {};
}

function readPreV13SqlitePluginIndex(db) {
  const row = readBoundedIndexRow(
    db,
    "installed_plugin_index",
    "index_key",
    PRE_V13_INDEX_KEY,
    ["install_records_json", "plugins_json", "diagnostics_json"],
    `version, warning, host_contract_version, compat_registry_version,
     migration_version, policy_hash, generated_at_ms, refresh_reason,
     install_records_json, plugins_json, diagnostics_json`,
  );
  if (!row) {
    return {};
  }
  return {
    version: Number(row.version),
    ...(row.warning ? { warning: row.warning } : {}),
    hostContractVersion: row.host_contract_version,
    compatRegistryVersion: row.compat_registry_version,
    migrationVersion: Number(row.migration_version),
    policyHash: row.policy_hash,
    generatedAtMs: Number(row.generated_at_ms),
    ...(row.refresh_reason ? { refreshReason: row.refresh_reason } : {}),
    installRecords: parseIndexJsonText(
      row.install_records_json,
      "plugin index install_records_json",
    ),
    plugins: parseIndexJsonText(row.plugins_json, "plugin index plugins_json"),
    diagnostics: parseIndexJsonText(row.diagnostics_json, "plugin index diagnostics_json"),
  };
}

// These two consumers inspect install receipts and enabled roots, not catalogue metadata.
// Keep the full reader's aggregate cap for callers that compare the complete index.
function pluginSummarySql(plugins) {
  return `CASE WHEN json_type(${plugins}) = 'array' THEN (
    SELECT json_group_array(json((
      SELECT json_group_object(field.key, json(plugin.value -> ('$.' || field.key)))
      FROM json_each(plugin.value) AS field
      WHERE field.key IN ('pluginId', 'enabled', 'rootDir')
    ))) FROM (
      SELECT key, value FROM json_each(${plugins}) ORDER BY CAST(key AS INTEGER)
    ) AS plugin
  ) ELSE json(${plugins}) END`;
}

function readBoundedIndexSummary(db, table, keyColumn, key, records, plugins, diagnostics, raw) {
  if (!tableExists(db, table)) {
    return {};
  }
  const summary = `json_object(
    'installRecords', json(${records}),
    'plugins', json(${pluginSummarySql(plugins)})
  )`;
  // The CASE refuses an oversized required projection before returning its text to Node.
  const row = db
    .prepare(`
    WITH projected AS (
      SELECT ${summary} AS summary_json,
        ${raw} AS raw_bytes,
        octet_length(${records}) AS install_records_bytes,
        octet_length(${plugins}) AS plugins_bytes,
        octet_length(${diagnostics}) AS diagnostics_bytes,
        json_array_length(${plugins}) AS plugin_count
      FROM ${table} WHERE ${keyColumn} = ?
    )
    SELECT octet_length(summary_json) AS summary_bytes,
      CASE WHEN octet_length(summary_json) <= ? THEN summary_json END AS summary_json,
      raw_bytes, install_records_bytes, plugins_bytes, diagnostics_bytes, plugin_count
    FROM projected
  `)
    .get(key, JSON_ARTIFACT_MAX_BYTES);
  if (!row) {
    return {};
  }
  assertIndexJsonByteLength(row.summary_bytes, "plugin index summary");
  return {
    ...parseIndexJsonText(row.summary_json, "plugin index summary"),
    readBytes: {
      raw: Number(row.raw_bytes),
      summary: Number(row.summary_bytes),
      installRecords: Number(row.install_records_bytes ?? 0),
      plugins: Number(row.plugins_bytes ?? 0),
      diagnostics: Number(row.diagnostics_bytes ?? 0),
      pluginCount: Number(row.plugin_count ?? 0),
    },
  };
}

function readCurrentSqlitePluginSummary(db) {
  return readBoundedIndexSummary(
    db,
    "config_machine_state",
    "state_key",
    STATE_KEY,
    "json_extract(value_json, '$.index.installRecords')",
    "json_extract(value_json, '$.index.plugins')",
    "json_extract(value_json, '$.index.diagnostics')",
    "octet_length(value_json)",
  );
}

function readPreV13SqlitePluginSummary(db) {
  return readBoundedIndexSummary(
    db,
    "installed_plugin_index",
    "index_key",
    PRE_V13_INDEX_KEY,
    "install_records_json",
    "plugins_json",
    "diagnostics_json",
    "octet_length(install_records_json) + octet_length(plugins_json) + octet_length(diagnostics_json)",
  );
}

function readSqlitePluginIndex(
  root = stateDir(),
  readCurrent = readCurrentSqlitePluginIndex,
  readPreV13 = readPreV13SqlitePluginIndex,
) {
  const dbPath = sqlitePath(root);
  if (!fs.existsSync(dbPath)) {
    return {};
  }
  let db;
  try {
    db = new DatabaseSync(dbPath, { readOnly: true });
    const schemaVersion = Number(db.prepare("PRAGMA user_version").get()?.user_version ?? 0);
    // v13 retired the wide row. Versioned databases must never cross-read the
    // other schema; only unversioned E2E fixtures may probe current then legacy.
    if (schemaVersion >= 13) {
      return readCurrent(db);
    }
    if (schemaVersion >= 1) {
      return readPreV13(db);
    }
    const current = readCurrent(db);
    return current.installRecords ? current : readPreV13(db);
  } catch (error) {
    if (error?.code === "ETOOBIG") {
      throw error;
    }
    return {};
  } finally {
    db?.close();
  }
}

function readPluginInstallIndexWithReader(options, readSqlite) {
  const root = options.stateDir ?? stateDir();
  // Failure diagnostics may inspect the persisted index without consulting config.
  const config =
    options.configPath === null ? {} : readJsonMaybe(options.configPath ?? configPath());
  const sqliteIndex = readSqlite(root);
  if (sqliteIndex.installRecords) {
    return sqliteIndex;
  }
  const legacyIndex = readJsonMaybe(legacyIndexPath(root));
  const installRecords =
    legacyIndex.installRecords ??
    legacyIndex.records ??
    options.fallbackRecords ??
    config.plugins?.installs ??
    {};
  return {
    ...legacyIndex,
    installRecords,
  };
}

export function readPluginInstallIndex(options = {}) {
  return readPluginInstallIndexWithReader(options, readSqlitePluginIndex);
}

/** Complete install receipts plus only the plugin fields asserted by upgrade fixtures. */
export function readPluginInstallSummary(options = {}) {
  let readBytes;
  const index = readPluginInstallIndexWithReader(options, (root) => {
    const summary = readSqlitePluginIndex(
      root,
      readCurrentSqlitePluginSummary,
      readPreV13SqlitePluginSummary,
    );
    if (summary.installRecords) {
      readBytes = summary.readBytes;
    }
    return summary;
  });
  return {
    installRecords: index.installRecords,
    plugins: (index.plugins ?? []).map((plugin) =>
      Object.fromEntries(
        ["pluginId", "enabled", "rootDir"]
          .filter((key) => Object.hasOwn(plugin, key))
          .map((key) => [key, plugin[key]]),
      ),
    ),
    ...(readBytes ? { readBytes } : {}),
  };
}

export function readPluginInstallRecords(options = {}) {
  return readPluginInstallIndex(options).installRecords;
}

export function writePluginInstallIndexForE2E(index, options = {}) {
  const root = options.stateDir ?? stateDir();
  const dbPath = sqlitePath(root);
  fs.mkdirSync(path.dirname(dbPath), { recursive: true });
  const db = new DatabaseSync(dbPath);
  try {
    const now = Date.now();
    const storageMode =
      options.storageMode === "existing-schema"
        ? Number(db.prepare("PRAGMA user_version").get()?.user_version ?? 0) >= 13
          ? "current"
          : "pre-v13"
        : (options.storageMode ?? "current");
    if (storageMode === "current") {
      db.exec(`
        CREATE TABLE IF NOT EXISTS config_machine_state (
          state_key TEXT NOT NULL PRIMARY KEY,
          value_json TEXT NOT NULL,
          updated_at_ms INTEGER NOT NULL
        );
      `);
      const persisted = {
        revision: now,
        index: {
          version: index.version ?? 1,
          warning:
            index.warning ??
            "DO NOT EDIT. This row is generated by OpenClaw plugin registry commands.",
          hostContractVersion: index.hostContractVersion ?? "docker-e2e",
          compatRegistryVersion: index.compatRegistryVersion ?? "docker-e2e",
          migrationVersion: index.migrationVersion ?? 1,
          policyHash: index.policyHash ?? "docker-e2e",
          generatedAtMs: index.generatedAtMs ?? now,
          ...(index.workspaceDir ? { workspaceDir: index.workspaceDir } : {}),
          ...(index.refreshReason ? { refreshReason: index.refreshReason } : {}),
          installRecords: index.installRecords ?? {},
          plugins: index.plugins ?? [],
          diagnostics: index.diagnostics ?? [],
        },
      };
      db.prepare(
        `
          INSERT INTO config_machine_state (state_key, value_json, updated_at_ms)
          VALUES (?, ?, ?)
          ON CONFLICT(state_key) DO UPDATE SET
            value_json = excluded.value_json,
            updated_at_ms = excluded.updated_at_ms
        `,
      ).run(STATE_KEY, JSON.stringify(persisted), now);
      return;
    }
    if (storageMode !== "pre-v13") {
      throw new Error(`Unknown plugin index storage mode: ${String(storageMode)}`);
    }
    db.exec(`
      CREATE TABLE IF NOT EXISTS installed_plugin_index (
        index_key TEXT NOT NULL PRIMARY KEY,
        version INTEGER NOT NULL,
        host_contract_version TEXT NOT NULL,
        compat_registry_version TEXT NOT NULL,
        migration_version INTEGER NOT NULL,
        policy_hash TEXT NOT NULL,
        generated_at_ms INTEGER NOT NULL,
        refresh_reason TEXT,
        install_records_json TEXT NOT NULL,
        plugins_json TEXT NOT NULL,
        diagnostics_json TEXT NOT NULL,
        warning TEXT,
        updated_at_ms INTEGER NOT NULL
      );
    `);
    db.prepare(
      `
        INSERT INTO installed_plugin_index (
          index_key, version, host_contract_version, compat_registry_version,
          migration_version, policy_hash, generated_at_ms, refresh_reason,
          install_records_json, plugins_json, diagnostics_json, warning, updated_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(index_key) DO UPDATE SET
          version = excluded.version,
          host_contract_version = excluded.host_contract_version,
          compat_registry_version = excluded.compat_registry_version,
          migration_version = excluded.migration_version,
          policy_hash = excluded.policy_hash,
          generated_at_ms = excluded.generated_at_ms,
          refresh_reason = excluded.refresh_reason,
          install_records_json = excluded.install_records_json,
          plugins_json = excluded.plugins_json,
          diagnostics_json = excluded.diagnostics_json,
          warning = excluded.warning,
          updated_at_ms = excluded.updated_at_ms
      `,
    ).run(
      PRE_V13_INDEX_KEY,
      index.version ?? 1,
      index.hostContractVersion ?? "docker-e2e",
      index.compatRegistryVersion ?? "docker-e2e",
      index.migrationVersion ?? 1,
      index.policyHash ?? "docker-e2e",
      index.generatedAtMs ?? now,
      index.refreshReason ?? null,
      JSON.stringify(index.installRecords ?? {}),
      JSON.stringify(index.plugins ?? []),
      JSON.stringify(index.diagnostics ?? []),
      index.warning ?? "DO NOT EDIT. This row is generated by OpenClaw plugin registry commands.",
      now,
    );
  } finally {
    db.close();
  }
}
