const ARTIFACT_KINDS = new Set(["table", "line", "bar"]);
const RESERVED_KEYS = new Set(["__proto__", "constructor", "prototype"]);
const SERIES_COLORS = ["#2563eb", "#a84213", "#087c6a", "#823caf", "#566918", "#ab3266"];
const AXIS_FORMAT = new Intl.NumberFormat("zh-CN", {
  notation: "compact",
  maximumFractionDigits: 2,
});
const MARKDOWN_NUMBER = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
const MARKDOWN_PREVIEW_ROWS = 60;

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isLabel(value) {
  return typeof value === "string" && value.trim() !== "";
}

function isKey(value) {
  return isLabel(value) && !RESERVED_KEYS.has(value) && !value.startsWith("__v_");
}

function isCell(value) {
  return typeof value === "string" || typeof value === "boolean" || Number.isFinite(value);
}

function normalizeArtifact(item) {
  if (!isObject(item) || !isLabel(item.id) || !isLabel(item.title) ||
      !ARTIFACT_KINDS.has(item.kind) || !Array.isArray(item.columns) ||
      !item.columns.length || !Array.isArray(item.rows) || !item.rows.every(isObject)) {
    return null;
  }

  const columns = [];
  const columnKeys = new Set();
  for (const column of item.columns) {
    if (!isObject(column) || !isKey(column.key) || !isLabel(column.label) ||
        columnKeys.has(column.key)) {
      return null;
    }
    const clean = { key: column.key, label: column.label };
    if (isLabel(column.unit)) clean.unit = column.unit;
    columns.push(clean);
    columnKeys.add(clean.key);
  }

  const artifact = { id: item.id, kind: item.kind, title: item.title, columns };
  if (["supporting", "requested"].includes(item.presentation)) artifact.presentation = item.presentation;
  const seriesKeys = new Set();
  if (item.kind !== "table") {
    if (!columnKeys.has(item.xKey) || !Array.isArray(item.series) || !item.series.length) {
      return null;
    }
    artifact.xKey = item.xKey;
    artifact.series = [];
    for (const series of item.series) {
      if (!isObject(series) || !columnKeys.has(series.key) || series.key === item.xKey ||
          !isLabel(series.label) || seriesKeys.has(series.key)) {
        return null;
      }
      artifact.series.push({ key: series.key, label: series.label });
      seriesKeys.add(series.key);
    }
  }

  artifact.rows = item.rows.map((row) =>
    Object.fromEntries(columns.map(({ key }) => {
      const value = Object.prototype.hasOwnProperty.call(row, key) ? row[key] : null;
      const valid = seriesKeys.has(key) ? Number.isFinite(value) : isCell(value);
      return [key, valid ? value : null];
    })),
  );
  if (Array.isArray(item.notes)) artifact.notes = item.notes.filter(isLabel);
  return artifact;
}

export function normalizeArtifacts(items) {
  if (!Array.isArray(items)) return [];
  const artifacts = [];
  const ids = new Set();
  for (const item of items) {
    const artifact = normalizeArtifact(item);
    if (!artifact || ids.has(artifact.id)) continue;
    artifacts.push(artifact);
    ids.add(artifact.id);
  }
  return artifacts;
}

export function columnLabel({ label, unit }) {
  return unit ? label + "（" + unit + "）" : label;
}

function lineSegments(points) {
  const segments = [];
  let segment = [];
  for (const point of points) {
    if (point === null) {
      if (segment.length) segments.push(segment);
      segment = [];
    } else {
      segment.push(point);
    }
  }
  if (segment.length) segments.push(segment);
  return segments;
}

function chartLabel(value) {
  const label = String(value ?? "—");
  // Chinese characters take roughly twice the width of ASCII numerals.
  let width = 0;
  let short = "";
  for (const char of label) {
    width += char.codePointAt(0) <= 127 ? 1 : 2;
    if (width > 7) return short + "…";
    short += char;
  }
  return label;
}

export function buildChart(artifact) {
  if (artifact.kind === "table") return null;
  const { rows, xKey, series, columns } = artifact;
  const seriesUnits = new Set(series.map(({ key }) => columns.find((c) => c.key === key).unit || ""));
  if (seriesUnits.size > 1) return null;
  const unit = [...seriesUnits][0];

  let min = 0;
  let max = 0;
  let hasValues = false;
  for (const row of rows) {
    for (const { key } of series) {
      const value = row[key];
      if (!Number.isFinite(value)) continue;
      min = Math.min(min, value);
      max = Math.max(max, value);
      hasValues = true;
    }
  }
  if (!hasValues) return null;

  const width = Math.min(2400, Math.max(300, 66 + rows.length * 39));
  const height = 192;
  const left = 58;
  const right = width - 8;
  const top = unit ? 26 : 12;
  const bottom = height - 26;
  const spacing = (right - left) / rows.length;
  const barWidth = Math.min(18, spacing * 0.78 / series.length);
  // Scaling first keeps the plotted coordinates finite for opposite-sign extremes.
  const magnitude = Math.max(Math.abs(min), Math.abs(max)) || 1;
  const axisMin = min / magnitude;
  const axisMax = min === max ? 1 : max / magnitude;
  const positionY = (value) => bottom - (value - axisMin) * (bottom - top) / (axisMax - axisMin);
  const ticks = Array.from({ length: 5 }, (_, index) => {
    const value = axisMin * (1 - index / 4) + axisMax * (index / 4);
    return { y: positionY(value), label: AXIS_FORMAT.format(value * magnitude) };
  });
  const categories = rows.map((row, index) => ({
    x: left + (index + 0.5) * spacing,
    title: String(row[xKey] ?? "无记录"),
    label: chartLabel(row[xKey]),
  }));
  const tickInterval = Math.ceil(38 / spacing);
  const chartSeries = series.map(({ key, label }, index) => {
    const points = rows.map((row, rowIndex) => {
      const value = row[key];
      if (!Number.isFinite(value)) return null;
      const { x, title } = categories[rowIndex];
      return {
        x,
        y: positionY(value / magnitude),
        barX: x + (index - series.length / 2) * barWidth,
        value,
        category: title,
      };
    });
    return {
      key,
      label,
      color: SERIES_COLORS[index % SERIES_COLORS.length],
      dash: index % 2 ? "5 3" : undefined,
      points,
      segments: artifact.kind === "line" ? lineSegments(points) : [],
    };
  });
  return {
    width, height, left, right, unit, ticks, barWidth,
    zeroY: positionY(0),
    categories: categories.filter((_, index) => index % tickInterval === 0),
    series: chartSeries,
  };
}

function markdownCell(value, literal = false) {
  if (value === null) return "未知";
  if (typeof value === "number") return MARKDOWN_NUMBER.format(value);
  if (typeof value === "boolean") return value ? "是" : "否";
  const text = value.replace(/[\r\n]/g, " ");
  if (literal) {
    if (!text.trim()) return "";
    const runs = text.match(/`+/g) ?? [];
    const fence = "`".repeat(1 + Math.max(0, ...runs.map((run) => run.length)));
    return fence + " " + text.replaceAll("|", "\\|") + " " + fence;
  }
  return text.replaceAll("\\", "\\\\").replaceAll("|", "\\|").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

function markdownPreview(markdown, item) {
  const tableRow = (cells) => "| " + cells.join(" | ") + " |";
  const header = [
    "### " + markdownCell(item.title), "",
    tableRow(item.columns.map((column) => markdownCell(columnLabel(column)))),
    tableRow(item.columns.map(() => "---")),
  ];
  const rows = item.rows.slice(0, MARKDOWN_PREVIEW_ROWS);
  const tables = [true, false].map((literal) => markdown.parse([
    ...header, ...rows.map((row) => tableRow(item.columns.map(({ key }) => markdownCell(row[key], literal)))),
  ].join("\n"), {}));
  const overflow = item.rows.length > MARKDOWN_PREVIEW_ROWS ? "其余记录见完整数据表下载。" : "";
  const notes = (item.notes ?? []).map((note) => markdownCell(note)).join("\n");
  return {
    tables,
    overflow: markdown.parse(overflow, {}),
    notes: markdown.parse(notes, {}),
  };
}

function tokensMatch(tokens, index, expected) {
  return expected.every(({ type, tag, level, content }, offset) => {
    const actual = tokens[index + offset];
    return actual?.type === type && actual.tag === tag &&
      actual.level === level && actual.content === content;
  });
}

export function renderAnalysisMarkdown(markdown, text, artifacts) {
  if (!artifacts.length) return markdown.render(text);
  const env = {};
  const tokens = markdown.parse(text, env);
  const previews = artifacts.map((item) => markdownPreview(markdown, item));
  // Look from the end because the backend appends the fallback after the narrative.
  for (let index = tokens.length - 1; index >= 0; index -= 1) {
    const token = tokens[index];
    if (token.type !== "heading_open" || token.tag !== "h3" || token.level !== 0 ||
        tokens[index + 3]?.type !== "table_open") continue;
    const matches = (table) => table.some((part) => part.type === "table_close") && tokensMatch(tokens, index, table);
    const match = previews.findIndex(({ tables }) => tables.some(matches));
    if (match < 0) continue;
    const [{ tables, overflow, notes }] = previews.splice(match, 1);
    let end = index + tables.find(matches).length;
    if (tokensMatch(tokens, end, overflow)) end += overflow.length;
    if (tokensMatch(tokens, end, notes)) end += notes.length;
    tokens.splice(index, end - index);
    if (!previews.length) break;
  }
  return markdown.renderer.render(tokens, markdown.options, env);
}

function csvCell(value) {
  if (value === null) return "";
  if (typeof value !== "string") return String(value);
  const protectedText = /^[=+\-@＝＋－＠\s\p{Cc}\p{Cf}]/u.test(value) ? "'" + value : value;
  return '"' + protectedText.replaceAll('"', '""') + '"';
}

export function serializeCsv({ columns, rows }) {
  const header = columns.map((column) => csvCell(columnLabel(column))).join(",");
  const lines = rows.map((row) => columns.map(({ key }) => csvCell(row[key])).join(","));
  return [header, ...lines].join("\r\n") + "\r\n";
}

export function exportFilename(title, extension) {
  const base = Array.from(title.replace(/[\p{Cc}\p{Cf}]/gu, "")
    .replace(/[<>:"/\\|?*]+/g, "-")
    .replace(/^[\s.-]+|[\s.-]+$/g, ""))
    .slice(0, 80).join("").replace(/[\s.]+$/g, "");
  const reserved = /^(?:con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(?:\.|$)/i;
  return (!base || reserved.test(base) ? "账单分析" : base) + "." + extension;
}
