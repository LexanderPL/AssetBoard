// The category donuts (F2.7) and net-worth line chart (F5.3). Every [data-chart] element holds
// its data in a <script type="application/json"> block (amounts in minor units with labels
// formatted by the server). With ECharts available the chart is drawn in [data-chart-canvas];
// otherwise the table stays visible. Donut legends are HTML under the chart ([data-chart-legend],
// shown with it), so they wrap onto several lines; the line chart's legend is ECharts' own and
// toggles series. Colours come from the CSS colour tokens (read at runtime). Charts re-render
// when the light/dark theme changes (the "dark" class on <html>) and on resize. CSP-safe: no
// eval, no style attributes in markup, and tooltips are drawn on the canvas (renderMode
// "richText").
(() => {
  "use strict";

  const charts = new Map();

  function token(name) {
    // Tokens hold "R G B" triples (static/css/input.css).
    const value = getComputedStyle(document.documentElement).getPropertyValue(`--color-${name}`).trim();
    if (!value) return undefined;
    return `rgb(${value.split(/\s+/).join(", ")})`;
  }

  function readData(element) {
    const source = document.getElementById(element.getAttribute("data-chart-source") || "");
    if (!source) return null;
    try {
      return JSON.parse(source.textContent || "null");
    } catch {
      return null;
    }
  }

  function donutOptions(data) {
    const ink = token("ink");
    const surface = token("surface");
    const line = token("line");
    const other = token("line-strong");
    const slices = data.slices.map((slice, index) => ({
      name: slice.name,
      value: slice.value, // minor units: only the proportions matter to the ring
      itemStyle: { color: slice.color || other },
      amount: slice.amount,
      share: slice.share,
      index,
    }));
    return {
      animation: false,
      aria: { enabled: false }, // the adjacent table is the accessible version
      tooltip: {
        trigger: "item",
        renderMode: "richText",
        backgroundColor: surface,
        borderColor: line,
        textStyle: { color: ink },
        formatter: (params) => `${params.data.name}\n${params.data.amount} (${params.data.share})`,
      },
      series: [
        {
          type: "pie",
          radius: ["50%", "86%"],
          center: ["50%", "50%"],
          avoidLabelOverlap: true,
          label: { show: false },
          labelLine: { show: false },
          emphasis: {
            label: {
              show: true,
              color: ink,
              fontWeight: "bold",
              formatter: (params) => `${params.data.share}`,
            },
          },
          itemStyle: { borderColor: surface, borderWidth: 2 },
          data: slices,
        },
      ],
    };
  }

  function lineTooltip(data, params) {
    if (!Array.isArray(params) || params.length === 0) return "";
    const index = params[0].dataIndex;
    const lines = [params[0].axisValue];
    params.forEach((param) => {
      const series = data.series[param.seriesIndex];
      if (series) lines.push(`${series.name}: ${series.amounts[index]}`);
    });
    const extra = data.tooltips[index] || [];
    if (extra.length) lines.push("", ...extra);
    return lines.join("\n");
  }

  function lineOptions(data) {
    const ink = token("ink");
    const surface = token("surface");
    const line = token("line");
    return {
      animation: false,
      aria: { enabled: false }, // the adjacent table is the accessible version
      legend: {
        top: 0,
        textStyle: { color: ink },
        data: data.series.map((series) => series.name),
      },
      grid: { left: 8, right: 16, top: 42, bottom: 8, containLabel: true },
      tooltip: {
        trigger: "axis",
        renderMode: "richText",
        backgroundColor: surface,
        borderColor: line,
        textStyle: { color: ink },
        formatter: (params) => lineTooltip(data, params),
      },
      xAxis: {
        type: "category",
        data: data.labels,
        boundaryGap: false,
        axisLabel: { color: ink },
        axisLine: { lineStyle: { color: line } },
      },
      yAxis: {
        type: "value",
        min: data.y_min,
        max: data.y_max,
        interval: data.y_interval,
        // Every tick is formatted on the server; the browser only looks the integer up.
        axisLabel: { color: ink, formatter: (value) => data.y_labels[String(value)] || "" },
        splitLine: { lineStyle: { color: line } },
      },
      series: data.series.map((series) => ({
        name: series.name,
        type: "line",
        data: series.values,
        // A single snapshot has no line; keep its point visible until the series is long.
        showSymbol: data.labels.length <= 24,
        symbolSize: 4,
        lineStyle: { width: 2, color: token(series.color) },
        itemStyle: { color: token(series.color) },
        emphasis: { focus: "series" },
      })),
    };
  }

  function draw(element) {
    const echarts = window.echarts;
    const canvas = element.querySelector("[data-chart-canvas]");
    const data = readData(element);
    const kind = element.getAttribute("data-chart");
    let option;
    if (!echarts || !canvas || !data) return;
    if (kind === "donut" && Array.isArray(data.slices)) {
      option = donutOptions(data);
      const legend = element.querySelector("[data-chart-legend]");
      if (legend) legend.hidden = false;
      element.querySelector("[data-chart-table]")?.classList.add("sr-only");
    } else if (kind === "line" && Array.isArray(data.series)) {
      option = lineOptions(data);
    } else {
      return;
    }
    canvas.hidden = false;
    let chart = charts.get(element);
    if (!chart) {
      chart = echarts.init(canvas, null, { renderer: "canvas" });
      charts.set(element, chart);
    }
    chart.setOption(option, true);
    element.setAttribute("data-chart-ready", "true");
  }

  function drawAll() {
    document.querySelectorAll('[data-chart="donut"], [data-chart="line"]').forEach(draw);
  }

  function start() {
    drawAll();
    // theme.js and the toggle switch the "dark" class on <html>: redraw with the new tokens.
    new MutationObserver(drawAll).observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class", "data-theme"],
    });
    let pending = 0;
    window.addEventListener("resize", () => {
      window.cancelAnimationFrame(pending);
      pending = window.requestAnimationFrame(() => charts.forEach((chart) => chart.resize()));
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
