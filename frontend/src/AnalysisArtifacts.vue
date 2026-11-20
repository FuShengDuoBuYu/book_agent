<template>
  <details class="analysis-evidence" :open="items.some((item) => item.presentation === 'requested')">
    <summary>查看数据依据（{{ items.length }}项）</summary>
    <div class="analysis-artifacts">
    <section v-for="{ artifact, chart } in cards" :key="artifact.id" class="analysis-card">
      <header class="analysis-heading">
        <h3>{{ artifact.title }}</h3>
        <div class="analysis-downloads">
          <button type="button" :aria-label="artifact.title + '，下载 CSV'" @click="downloadCsv(artifact)">
            CSV
          </button>
          <button
            v-if="chart"
            type="button"
            :aria-label="artifact.title + '，下载 PNG'"
            :disabled="exporting !== null"
            @click="downloadPng(artifact, chart)"
          >
            {{ exporting === artifact.id ? "生成中…" : "PNG" }}
          </button>
        </div>
      </header>

      <template v-if="chart">
        <ul class="chart-legend" aria-label="图例">
          <li v-for="series in chart.series" :key="series.key">
            <span
              aria-hidden="true"
              :style="{ borderColor: series.color, borderTopStyle: artifact.kind === 'line' && series.dash ? 'dashed' : 'solid' }"
            ></span>
            {{ series.label }}
          </li>
        </ul>
        <div class="analysis-scroll" tabindex="0" role="region" :aria-label="artifact.title + '，图表'">
          <svg
            :ref="(element) => setChartElement(artifact.id, element)"
            xmlns="http://www.w3.org/2000/svg"
            :viewBox="['0', '0', chart.width, chart.height].join(' ')"
            :width="chart.width"
            :height="chart.height"
            :style="{ minWidth: chart.width + 'px' }"
            class="analysis-chart"
            role="img"
            :aria-label="artifact.title + '，' + (artifact.kind === 'line' ? '折线图' : '柱状图')"
            font-family="system-ui, sans-serif"
            font-size="10"
            fill="#52525b"
          >
            <title>{{ artifact.title }}</title>
            <desc>展开数据表可查看数值和缺失记录。</desc>
            <text v-if="chart.unit" :x="chart.left" y="13">{{ chart.unit }}</text>
            <g v-for="(tick, index) in chart.ticks" :key="index">
              <line :x1="chart.left" :x2="chart.right" :y1="tick.y" :y2="tick.y" stroke="#e4e4e7" />
              <text :x="chart.left - 7" :y="tick.y" text-anchor="end" dominant-baseline="middle">{{ tick.label }}</text>
            </g>
            <line :x1="chart.left" :x2="chart.right" :y1="chart.zeroY" :y2="chart.zeroY" stroke="#a1a1aa" />
            <g v-for="(category, index) in chart.categories" :key="index">
              <text :x="category.x" :y="chart.height - 8" text-anchor="middle">{{ category.label }}</text>
              <title>{{ category.title }}</title>
            </g>
            <g v-for="series in chart.series" :key="series.key">
              <template v-if="artifact.kind === 'line'">
                <polyline
                  v-for="(segment, index) in series.segments.filter((part) => part.length > 1)"
                  :key="index"
                  :points="segment.map(({ x, y }) => x + ',' + y).join(' ')"
                  :stroke="series.color"
                  :stroke-dasharray="series.dash"
                  stroke-width="2"
                  stroke-linejoin="round"
                  fill="none"
                />
              </template>
              <template v-for="(point, index) in series.points" :key="index">
                <template v-if="point">
                  <circle v-if="artifact.kind === 'line'" :cx="point.x" :cy="point.y" r="2.8" :fill="series.color">
                    <title>{{ point.category }} · {{ series.label }} {{ point.value }} {{ chart.unit }}</title>
                  </circle>
                  <rect
                    v-else
                    :x="point.barX"
                    :y="point.value === 0 ? chart.zeroY - 0.75 : Math.min(point.y, chart.zeroY)"
                    :width="chart.barWidth"
                    :height="point.value === 0 ? 1.5 : Math.abs(point.y - chart.zeroY)"
                    :fill="series.color"
                  >
                    <title>{{ point.category }} · {{ series.label }} {{ point.value }} {{ chart.unit }}</title>
                  </rect>
                </template>
              </template>
            </g>
          </svg>
        </div>
      </template>

      <component :is="chart ? 'details' : 'div'" class="analysis-data">
        <summary v-if="chart">查看数据表</summary>
        <div v-if="artifact.rows.length" class="analysis-scroll" tabindex="0" role="region" :aria-label="artifact.title + '，数据表'">
          <table :aria-label="artifact.title">
            <thead>
              <tr>
                <th v-for="column in artifact.columns" :key="column.key" scope="col">{{ columnLabel(column) }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(row, index) in artifact.rows" :key="index">
                <td
                  v-for="column in artifact.columns"
                  :key="column.key"
                  :class="{ 'analysis-number': typeof row[column.key] === 'number' }"
                >
                  {{ cellText(row[column.key]) }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-else class="analysis-empty">暂无数据</p>
      </component>
      <ul v-if="artifact.notes?.length" class="analysis-notes">
        <li v-for="(note, index) in artifact.notes" :key="index">{{ note }}</li>
      </ul>
    </section>
    </div>
  </details>
</template>

<script setup>
import { computed, ref } from "vue";
import { buildChart, columnLabel, exportFilename, serializeCsv } from "./artifacts.js";

const props = defineProps({ items: { type: Array, required: true } });
const emit = defineEmits(["download-error"]);
const chartElements = new Map();
const exporting = ref(null);
const cards = computed(() => props.items.map((artifact) => ({ artifact, chart: buildChart(artifact) })));

function cellText(value) {
  if (value === null) return "未知";
  if (typeof value === "boolean") return value ? "是" : "否";
  return value;
}

function setChartElement(id, element) {
  if (element) chartElements.set(id, element);
  else chartElements.delete(id);
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  try {
    link.click();
  } finally {
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 30_000);
  }
}

function downloadCsv(artifact) {
  try {
    const csv = new Blob(["\uFEFF", serializeCsv(artifact)], { type: "text/csv;charset=utf-8" });
    downloadBlob(csv, exportFilename(artifact.title, "csv"));
  } catch {
    emit("download-error", "CSV 下载失败，请重试。");
  }
}

async function downloadPng(artifact, chart) {
  const svg = chartElements.get(artifact.id);
  if (!svg || exporting.value !== null) return;
  exporting.value = artifact.id;
  try {
    const source = new XMLSerializer().serializeToString(svg)
      .replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F]/g, "");
    const image = new Image();
    await new Promise((resolve, reject) => {
      image.onload = resolve;
      image.onerror = reject;
      image.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(source);
    });

    const padding = 12;
    const chartTop = 34 + chart.series.length * 18;
    const canvas = document.createElement("canvas");
    const scale = 2;
    canvas.width = (chart.width + padding * 2) * scale;
    canvas.height = (chart.height + chartTop + padding) * scale;
    const ctx = canvas.getContext("2d");
    ctx.scale(scale, scale);
    ctx.fillStyle = "#fff";
    ctx.fillRect(0, 0, canvas.width / scale, canvas.height / scale);
    ctx.fillStyle = "#18181b";
    ctx.font = "600 16px system-ui, sans-serif";
    ctx.fillText(artifact.title, padding, 21, chart.width);
    ctx.font = "12px system-ui, sans-serif";
    chart.series.forEach((series, index) => {
      const y = 35 + index * 18;
      ctx.fillStyle = series.color;
      ctx.fillRect(padding, y - 6, 14, 3);
      ctx.fillStyle = "#52525b";
      ctx.fillText(series.label, padding + 22, y, chart.width - 22);
    });
    ctx.drawImage(image, padding, chartTop, chart.width, chart.height);
    const png = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
    if (!png) throw new Error("Image encoding failed");
    downloadBlob(png, exportFilename(artifact.title, "png"));
  } catch {
    emit("download-error", "PNG 下载失败，请重试。");
  } finally {
    exporting.value = null;
  }
}
</script>

<style scoped>
 .analysis-evidence {
  border: 1px solid var(--border, #e4e4e7);
  border-radius: 12px;
  padding: 12px;
}
.analysis-evidence > summary {
  cursor: pointer;
  color: var(--muted, #52525b);
  font-size: 13px;
}
.analysis-evidence[open] > summary { margin-bottom: 12px; }

.analysis-artifacts {
  display: grid;
  gap: 12px;
  margin-top: 10px;
}

.analysis-card {
  min-width: 0;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 14px;
  background: var(--bg);
}

.analysis-heading,
.analysis-downloads {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}

.analysis-heading {
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
}

.analysis-heading h3 {
  flex: 1 1 140px;
  margin: 0;
  font-size: 15px;
  line-height: 1.4;
  overflow-wrap: anywhere;
}

.analysis-downloads button {
  min-width: 44px;
  min-height: 34px;
  padding: 6px 10px;
  border-radius: 8px;
  background: var(--soft);
  color: #3f3f46;
  font-size: 13px;
  font-weight: 650;
}

.analysis-downloads button:disabled {
  opacity: 0.55;
}

.chart-legend,
.analysis-notes {
  margin: 0;
  color: #52525b;
  font-size: 12px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.chart-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  padding: 0 0 6px;
  list-style: none;
}

.chart-legend li span {
  display: inline-block;
  width: 14px;
  margin-right: 3px;
  border-top: 3px solid;
  vertical-align: middle;
}

.analysis-scroll {
  max-width: 100%;
  overflow: auto;
}

.analysis-chart {
  display: block;
  height: auto;
}

.analysis-data summary {
  width: fit-content;
  padding: 8px 0;
  color: #3f3f46;
  font-size: 13px;
  cursor: pointer;
}

.analysis-data table {
  min-width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.analysis-data th,
.analysis-data td {
  padding: 7px 10px;
  border: 1px solid var(--line);
  text-align: left;
  white-space: pre-line;
}

.analysis-data th {
  background: var(--soft);
  white-space: nowrap;
}

.analysis-data .analysis-number {
  text-align: right;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.analysis-notes {
  margin-top: 6px;
  padding-left: 16px;
}

.analysis-empty {
  padding: 8px 0;
  color: var(--muted);
  font-size: 13px;
}
</style>
