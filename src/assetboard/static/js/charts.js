// The category donuts (F2.7). Every [data-chart="donut"] element holds its data in a
// <script type="application/json"> block (amounts in minor units with labels formatted by the
// server) and a table. With ECharts available the chart is drawn in [data-chart-canvas] and the
// table becomes screen-reader-only; otherwise the table stays visible. Colours come from the CSS
// colour tokens (read at runtime), slices from the category colour. Charts re-render when the
// light/dark theme changes (the "dark" class on <html>) and on resize. CSP-safe: no eval, no
// style attributes in markup, and the tooltip is drawn on the canvas (renderMode "richText").
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

  function options(data) {
    const ink = token("ink");
    const muted = token("muted");
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
      legend: {
        type: "scroll",
        orient: "horizontal",
        bottom: 0,
        textStyle: { color: muted },
        pageTextStyle: { color: muted },
        pageIconColor: ink,
        pageIconInactiveColor: line,
      },
      series: [
        {
          type: "pie",
          radius: ["48%", "72%"],
          center: ["50%", "44%"],
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

  function draw(element) {
    const echarts = window.echarts;
    const canvas = element.querySelector("[data-chart-canvas]");
    const data = readData(element);
    if (!echarts || !canvas || !data || !Array.isArray(data.slices)) return;
    canvas.hidden = false;
    element.querySelector("[data-chart-table]")?.classList.add("sr-only");
    let chart = charts.get(element);
    if (!chart) {
      chart = echarts.init(canvas, null, { renderer: "canvas" });
      charts.set(element, chart);
    }
    chart.setOption(options(data), true);
    element.setAttribute("data-chart-ready", "true");
  }

  function drawAll() {
    document.querySelectorAll('[data-chart="donut"]').forEach(draw);
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
