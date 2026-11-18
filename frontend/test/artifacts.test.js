import assert from "node:assert/strict";
import test from "node:test";
import MarkdownIt from "markdown-it";
import {
  buildChart,
  exportFilename,
  normalizeArtifacts,
  renderAnalysisMarkdown,
  serializeCsv,
} from "../src/artifacts.js";

const monthlyIncome = {
  id: "monthly-income",
  kind: "line",
  title: "月收入趋势",
  columns: [
    { key: "month", label: "月份" },
    { key: "pay", label: "工资", unit: "元" },
    { key: "other", label: "其他", unit: "元" },
  ],
  xKey: "month",
  series: [{ key: "pay", label: "工资" }, { key: "other", label: "其他" }],
  rows: [
    { month: "2026-08", pay: 2000, other: -200 },
    { month: "2026-09", pay: null, other: 0 },
    { month: "2026-10", pay: 3200, other: 100 },
    { month: "2026-11", pay: 3500, other: 80 },
  ],
  notes: ["九月工资数据缺失。"],
};

test("validated artifact transport keeps chart labels, units, values and notes", () => {
  assert.deepEqual(normalizeArtifacts(JSON.parse(JSON.stringify([monthlyIncome]))), [monthlyIncome]);
});

test("malformed artifacts cannot shadow own column keys or add unknown fields", () => {
  const pollution = JSON.parse('{"month":"A","__proto__":{"polluted":true},"pay":500,"other":90,"hidden":"private"}');
  const [result] = normalizeArtifacts([
    { ...monthlyIncome, kind: "svg" },
    { ...monthlyIncome, xKey: "missing" },
    { ...monthlyIncome, series: [{ key: "missing", label: "未知" }] },
    { ...monthlyIncome, columns: [...monthlyIncome.columns, monthlyIncome.columns[0]] },
    { ...monthlyIncome, rows: [null] },
    { ...monthlyIncome, rows: [pollution], debug: "private" },
    monthlyIncome,
  ]);
  assert.deepEqual(result.rows, [{ month: "A", pay: 500, other: 90 }]);
  assert.equal(Object.hasOwn(result, "debug"), false);
  assert.equal(normalizeArtifacts([monthlyIncome, monthlyIncome]).length, 1);
  assert.deepEqual(normalizeArtifacts(undefined), []);
  assert.deepEqual(normalizeArtifacts({ rows: [] }), []);
});

test("line geometry breaks across unknown values and retains exact source amounts", () => {
  const [artifact] = normalizeArtifacts([monthlyIncome]);
  const chart = buildChart(artifact);
  const salary = chart.series[0];
  assert.deepEqual(salary.points.map((point) => point?.value ?? null), [2000, null, 3200, 3500]);
  assert.deepEqual(salary.segments.map((segment) => segment.map((point) => point.value)), [[2000], [3200, 3500]]);
  assert.deepEqual(chart.series[1].points.map((point) => point.value), [-200, 0, 100, 80]);
});

test("non-finite, object and string chart amounts become gaps, while zero remains a value", () => {
  const [artifact] = normalizeArtifacts([{
    ...monthlyIncome,
    rows: [
      { month: "A", pay: "100", other: 0 },
      { month: "B", pay: Infinity, other: { value: 12 } },
      { month: "C", pay: true, other: NaN },
    ],
  }]);
  assert.deepEqual(artifact.rows, [
    { month: "A", pay: null, other: 0 },
    { month: "B", pay: null, other: null },
    { month: "C", pay: null, other: null },
  ]);
  assert.deepEqual(buildChart(artifact).series[1].points.map((point) => point?.value ?? null), [0, null, null]);
});

test("grouped bar geometry places positive and negative values on either side of zero", () => {
  const [artifact] = normalizeArtifacts([{ ...monthlyIncome, kind: "bar" }]);
  const chart = buildChart(artifact);
  assert.ok(chart.series[0].points[0].y < chart.zeroY);
  assert.ok(chart.series[1].points[0].y > chart.zeroY);
  assert.equal(chart.series[1].points[1].y, chart.zeroY);
  assert.equal(chart.series[0].points[1], null);
  assert.ok(chart.series[0].points[0].barX + chart.barWidth <= chart.series[1].points[0].barX);
});

test("legacy replies and unrelated Markdown tables survive artifact fallback replacement", () => {
  const markdown = new MarkdownIt({ html: false });
  const text = [
    "### 说明", "| 月份 | 变动 |", "| --- | --- |", "| 2026-08 | 新增 |",
    "", "### 月收入趋势", "", "| 月份 | 工资（元） | 其他（元） |", "| --- | --- | --- |",
    "| 2026-08 | 2,000 | -200 |", "| 2026-09 | 未知 | 0 |", "| 2026-10 | 3,200 | 100 |", "| 2026-11 | 3,500 | 80 |",
    "", "下个月继续观察。",
  ].join("\n");
  assert.equal(renderAnalysisMarkdown(markdown, text, []), markdown.render(text));
  const rendered = renderAnalysisMarkdown(markdown, text, normalizeArtifacts([monthlyIncome]));
  assert.match(rendered, /<td>新增<\/td>/);
  assert.match(rendered, /下个月继续观察/);
  assert.doesNotMatch(rendered, /月收入趋势|工资（元）/);
  const differentValues = text.replace("3,500", "4,500");
  assert.match(renderAnalysisMarkdown(markdown, differentValues, normalizeArtifacts([monthlyIncome])), /4,500/);
});

test("an incomplete Markdown fallback and a same-title table with different columns remain readable", () => {
  const markdown = new MarkdownIt({ html: false });
  const artifacts = normalizeArtifacts([monthlyIncome]);
  const text = "### 月收入趋势\n\n| 月份 | 预测 |\n| --- | --- |\n| 2026-08 | 2000 |";
  const incomplete = "### 月收入趋势\n\n| 月份 | 工资（元） | 其他（元） |\n| --- | --- | --- |";
  assert.equal(renderAnalysisMarkdown(markdown, text, artifacts), markdown.render(text));
  assert.equal(renderAnalysisMarkdown(markdown, incomplete, artifacts), markdown.render(incomplete));
});

test("the matching Markdown preview and its notes appear only once in an artifact reply", () => {
  const markdown = new MarkdownIt({ html: false });
  const artifacts = normalizeArtifacts([{
    id: "ledger", kind: "table", title: "带 | 符号的明细",
    columns: [{ key: "note", label: "备注" }],
    rows: Array.from({ length: 61 }, () => ({ note: "<原文>|测试" })),
    notes: ["依据原始备注。", "共展示六十行。"],
  }]);
  const fallback = [
    "### 带 \\| 符号的明细", "", "| 备注 |", "| --- |",
    ...Array.from({ length: 60 }, () => "| &lt;原文&gt;\\|测试 |"),
    "", "其余记录见完整数据表下载。", "", "依据原始备注。", "共展示六十行。", "", "其他说明。",
  ].join("\n");
  assert.equal(renderAnalysisMarkdown(markdown, fallback, artifacts), "<p>其他说明。</p>\n");
});

test("literal ledger links and their code fences stay inert and deduplicate in the chat", () => {
  const markdown = new MarkdownIt({ html: false, linkify: true });
  const note = "[账户](https://example.invalid/a) ![图](https://example.invalid/b) https://example.invalid/c `内` | 末\n下一行";
  const artifacts = normalizeArtifacts([{
    id: "ledger", kind: "table", title: "备注表",
    columns: [{ key: "note", label: "备注" }, { key: "amount", label: "金额", unit: "元" }],
    rows: [{ note, amount: 42.25 }],
  }]);
  const fallback = [
    "账单备注如下。", "", "### 备注表", "", "| 备注 | 金额（元） |", "| --- | --- |",
    "| `` [账户](https://example.invalid/a) ![图](https://example.invalid/b) https://example.invalid/c `内` \\| 末 下一行 `` | 42.25 |",
  ].join("\n");
  const fallbackHtml = renderAnalysisMarkdown(markdown, fallback, []);
  assert.doesNotMatch(fallbackHtml, /<(?:a|img)\b/);
  assert.match(fallbackHtml, /<code>\[账户\]\(https:\/\/example\.invalid\/a\)/);
  assert.equal(renderAnalysisMarkdown(markdown, fallback, artifacts), "<p>账单备注如下。</p>\n");
});

test("CSV quotes delimiters, newlines and embedded quotes while preserving numbers", () => {
  const [artifact] = normalizeArtifacts([{
    id: "ledger", kind: "table", title: "收入",
    columns: [{ key: "amount", label: "金额", unit: "元" }, { key: "note", label: "备注" }],
    rows: [{ amount: -42.5, note: '第一行,"原文"\n第二行' }, { amount: null, note: null }],
  }]);
  assert.equal(serializeCsv(artifact), '"金额（元）","备注"\r\n-42.5,"第一行,""原文""\n第二行"\r\n,\r\n');
});

test("CSV makes formula-like headers and strings inert, including control prefixes", () => {
  const [artifact] = normalizeArtifacts([{
    id: "ledger", kind: "table", title: "收入",
    columns: [{ key: "note", label: "@列" }],
    rows: ["=1+1", "+1", "-5", "@sum", " =1+1", "\t=1+1", "\u0001=1+1", "＝1+1", -5].map((note) => ({ note })),
  }]);
  assert.equal(
    serializeCsv(artifact),
    '"\'@列"\r\n"\'=1+1"\r\n"\'+1"\r\n"\'-5"\r\n"\'@sum"\r\n"\' =1+1"\r\n"\'\t=1+1"\r\n"\'\u0001=1+1"\r\n"\'＝1+1"\r\n-5\r\n',
  );
});

test("export filenames keep Chinese titles and strip path and system filename syntax", () => {
  assert.equal(exportFilename("../月收入:/2026-08?\u202e", "png"), "月收入-2026-08.png");
  assert.equal(exportFilename("CON", "csv"), "账单分析.csv");
});
