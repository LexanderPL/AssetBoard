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
    // HTMX swaps strip <script> tags, so a chart rendered into an HTMX fragment carries its JSON
    // in an attribute. Pages with a script source keep working exactly as before.
    const inline = element.getAttribute("data-chart-json");
    if (inline !== null) {
      try {
        return JSON.parse(inline);
      } catch {
        return null;
      }
    }
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


  function performanceOptions(data) {
    const ink = token("ink");
    const surface = token("surface");
    const line = token("line");
    const investedColor = token("muted");
    const valueColor = token("primary-ink");
    const gainColor = token("success");
    const gainSegments = data.labels.map((_label, index) => index);
    return {
      animation: false,
      aria: { enabled: false }, // the plotted-data table is the accessible version
      legend: {
        top: 0,
        textStyle: { color: ink },
        data: [data.legend.value, data.legend.invested],
      },
      grid: { left: 8, right: 16, top: 42, bottom: 8, containLabel: true },
      tooltip: {
        trigger: "axis",
        renderMode: "richText",
        backgroundColor: surface,
        borderColor: line,
        textStyle: { color: ink },
        formatter: (params) => {
          if (!Array.isArray(params) || params.length === 0) return "";
          const index = params[0].dataIndex;
          return [data.labels[index], ...(data.tooltips[index] || [])].join("\n");
        },
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
        axisLabel: { color: ink, formatter: (value) => data.y_labels[String(value)] || "" },
        splitLine: { lineStyle: { color: line } },
      },
      series: [
        {
          // The two lines are independent: a null point is a gap, never a zero drop. The gain
          // band is drawn separately so stacking cannot turn a missing point into 0.
          name: data.legend.invested,
          type: "line",
          data: data.invested,
          connectNulls: false,
          showSymbol: false,
          z: 3,
          lineStyle: { width: 2, color: investedColor },
          itemStyle: { color: investedColor },
        },
        {
          name: data.legend.value,
          type: "line",
          data: data.value,
          connectNulls: false,
          showSymbol: false,
          z: 4,
          lineStyle: { width: 2, color: valueColor },
          itemStyle: { color: valueColor },
        },
        {
          // The area between value and invested is the gain. A segment touching a missing
          // point is skipped, so the band gaps with the lines.
          type: "custom",
          name: "gain",
          data: gainSegments,
          silent: true,
          z: 1,
          renderItem(params, api) {
            const index = params.dataIndex;
            const next = index + 1;
            if (next >= data.value.length) return null;
            const value = data.value[index];
            const nextValue = data.value[next];
            const invested = data.invested[index];
            const nextInvested = data.invested[next];
            if (
              value == null ||
              nextValue == null ||
              invested == null ||
              nextInvested == null
            ) {
              return null;
            }
            return {
              type: "polygon",
              shape: {
                points: [
                  api.coord([index, value]),
                  api.coord([next, nextValue]),
                  api.coord([next, nextInvested]),
                  api.coord([index, invested]),
                ],
              },
              style: { fill: gainColor, opacity: 0.2 },
            };
          },
        },
      ],
    };
  }

  function markLineData(data, ink) {
    const line = token("line");
    return (data.markers || []).map((marker) => ({
      xAxis: data.labels[marker.index],
      name: marker.text,
      label: {
        show: true,
        formatter: marker.label,
        position: "insideEndTop",
        color: ink,
        fontSize: 10,
      },
      lineStyle: { color: line, type: "dashed", width: 1 },
    }));
  }

  function lineOptions(data) {
    const ink = token("ink");
    const surface = token("surface");
    const line = token("line");
    const markers = markLineData(data, ink);
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
      series: data.series.map((series, index) => ({
        name: series.name,
        type: "line",
        data: series.values,
        // A single snapshot has no line; keep its point visible until the series is long.
        showSymbol: data.labels.length <= 24,
        symbolSize: 4,
        lineStyle: { width: 2, color: token(series.color) },
        itemStyle: { color: token(series.color) },
        emphasis: { focus: "series" },
        markLine:
          index === 0 && markers.length
            ? { symbol: "none", silent: true, data: markers }
            : undefined,
        markPoint:
          index === 0 && Array.isArray(data.mark_points) && data.mark_points.length
            ? {
                symbol: "pin",
                symbolSize: 42,
                itemStyle: { color: token("primary-ink") },
                label: {
                  show: true,
                  color: token("on-primary"),
                  fontSize: 10,
                  fontWeight: "bold",
                  formatter: (params) => params.data.labelText || "",
                },
                data: data.mark_points.map((point) => ({
                  name: point.text,
                  labelText: point.label,
                  coord: [data.labels[point.index], point.value],
                })),
              }
            : undefined,
      })),
    };
  }

  function loanPaymentsOptions(data) {
    const ink = token("ink");
    const surface = token("surface");
    const line = token("line");
    const markers = markLineData(data, ink);
    return {
      animation: false,
      aria: { enabled: false }, // the plotted-data table is the accessible version
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
        formatter: (params) => {
          if (!Array.isArray(params) || params.length === 0) return "";
          const index = params[0].dataIndex;
          return [params[0].axisValue, ...(data.tooltips[index] || [])].join("\n");
        },
      },
      xAxis: {
        type: "category",
        data: data.labels,
        axisLabel: { color: ink },
        axisLine: { lineStyle: { color: line } },
      },
      yAxis: {
        type: "value",
        min: data.y_min,
        max: data.y_max,
        interval: data.y_interval,
        axisLabel: { color: ink, formatter: (value) => data.y_labels[String(value)] || "" },
        splitLine: { lineStyle: { color: line } },
      },
      series: data.series.map((series, index) => ({
        name: series.name,
        type: "bar",
        stack: "payments",
        data: series.values,
        itemStyle: { color: token(series.color) },
        emphasis: { focus: "series" },
        markLine:
          index === 0 && markers.length
            ? { symbol: "none", silent: true, data: markers }
            : undefined,
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
    } else if (kind === "performance" && Array.isArray(data.value)) {
      option = performanceOptions(data);
    } else if (kind === "loan-payments" && Array.isArray(data.series)) {
      option = loanPaymentsOptions(data);
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

  function disposeDetached() {
    // A swap replaces the chart's container; the old ECharts instance would otherwise keep its
    // canvas, listeners and data alive for the rest of the page.
    charts.forEach((chart, element) => {
      if (!element.isConnected || !element.querySelector("[data-chart-canvas]")) {
        charts.delete(element);
        chart.dispose();
      }
    });
  }

  function drawAll() {
    disposeDetached();
    document.querySelectorAll('[data-chart="donut"], [data-chart="line"], [data-chart="performance"], [data-chart="loan-payments"]').forEach(draw);
  }

  function start() {
    drawAll();
    // theme.js and the toggle switch the "dark" class on <html>: redraw with the new tokens.
    new MutationObserver(drawAll).observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class", "data-theme"],
    });
    // HTMX swaps can replace a whole chart or a fragment that contains one (the FIRE live
    // preview). Redraw after the DOM is in place; disposeDetached drops the replaced instances.
    for (const eventName of ["htmx:afterSwap", "htmx:oobAfterSwap", "htmx:load"]) {
      document.addEventListener(eventName, drawAll);
    }
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
