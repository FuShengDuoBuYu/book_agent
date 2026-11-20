import assert from "node:assert/strict";
import test from "node:test";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createServer } from "vite";
import { fileURLToPath } from "node:url";
import { normalizeArtifacts } from "../src/artifacts.js";

const table = { id: "example", kind: "table", title: "月度收支依据",
  columns: [{ key: "net", label: "结余", unit: "元" }], rows: [{ net: 3200 }] };

test("supporting data stays collapsed while an explicitly requested table can open", async () => {
  const server = await createServer({ root: fileURLToPath(new URL("../", import.meta.url)), server: { middlewareMode: true, hmr: false }, logLevel: "silent" });
  try {
    const { default: Component } = await server.ssrLoadModule("/src/AnalysisArtifacts.vue");
    const support = await renderToString(createSSRApp(Component, { items: [{ ...table, presentation: "supporting" }] }));
    const requested = await renderToString(createSSRApp(Component, { items: [{ ...table, presentation: "requested" }] }));
    const tag = support.match(/<details[^>]*class="analysis-evidence"[^>]*>/)?.[0];
    assert.ok(tag, "evidence should have a single expandable boundary");
    assert.doesNotMatch(tag, /\bopen(?:[ =>])/);
    assert.match(requested.match(/<details[^>]*class="analysis-evidence"[^>]*>/)?.[0] || "", /\bopen(?:[ =>])/);
    assert.match(support, /3200/);
    assert.match(support, /CSV/);
  } finally {
    await server.close();
  }
});

test("requested evidence presentation survives history normalization without private metadata", () => {
  const [result] = normalizeArtifacts([{ ...table, presentation: "requested", privatePlan: { secret: "hidden" } }]);
  assert.equal(result.presentation, "requested");
  assert.equal(Object.hasOwn(result, "privatePlan"), false);
  const [forged] = normalizeArtifacts([{ ...table, presentation: "invalid" }]);
  assert.notEqual(forged.presentation, "requested");
});

test("an opened requested table does not also open unrelated supporting tables", async () => {
  const server = await createServer({ root: fileURLToPath(new URL("../", import.meta.url)), server: { middlewareMode: true, hmr: false }, logLevel: "silent" });
  try {
    const { default: Component } = await server.ssrLoadModule("/src/AnalysisArtifacts.vue");
    const html = await renderToString(createSSRApp(Component, { items: [
      { ...table, presentation: "requested" },
      { ...table, id: "mapping", title: "原始备注映射", presentation: "supporting" },
    ] }));
    const cards = [...html.matchAll(/<details[^>]*class="analysis-card"[^>]*>/g)].map(match => match[0]);
    assert.equal(cards.length, 2);
    assert.match(cards[0], /\bopen(?:[ =>])/);
    assert.doesNotMatch(cards[1], /\bopen(?:[ =>])/);
  } finally { await server.close(); }
});
