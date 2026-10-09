# Vendored frontend assets

These browser libraries are pinned and served locally. The application does not load them from a
CDN at runtime.

| Asset | Version | License | SHA-256 | Source |
|---|---:|---|---|---|
| `htmx.min.js` | 2.0.4 | BSD-2-Clause | `69caefd0da92269066e725d7fe175e26b9d50c962e3056459c0c477154cdb9d3` | https://github.com/bigskysoftware/htmx |
| `alpinejs-csp.min.js` | 3.14.8 | MIT | `c98f46291087f0f93c9cdefb682d120fe1d2b3dcb649de83cfc1ad5b1fac58e4` | https://github.com/alpinejs/alpine |
| `echarts.min.js` | 5.6.0 | Apache-2.0 | `bf4a223524e40b77c304bec67e1222cf551f14880cf42c69dc046558e11c07b1` | https://github.com/apache/echarts |
| Tailwind CSS standalone CLI | 3.4.17 | MIT | https://github.com/tailwindlabs/tailwindcss |

The Tailwind CLI is fetched from its versioned upstream release during `make css` and cached in
`.cache/`; it is a build tool and is not used by the running application.
