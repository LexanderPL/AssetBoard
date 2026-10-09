# Vendored frontend assets

These browser libraries are pinned and served locally. The application does not load them from a
CDN at runtime.

| Asset | Version | License | Source |
|---|---:|---|---|
| HTMX | 2.0.4 | BSD-2-Clause | https://github.com/bigskysoftware/htmx |
| Alpine.js CSP build | 3.14.8 | MIT | https://github.com/alpinejs/alpine |
| Apache ECharts | 5.6.0 | Apache-2.0 | https://github.com/apache/echarts |
| Tailwind CSS standalone CLI | 3.4.17 | MIT | https://github.com/tailwindlabs/tailwindcss |

The Tailwind CLI is fetched from its versioned upstream release during `make css` and cached in
`.cache/`; it is a build tool and is not used by the running application.
