# Tax rules and calculator

Tax rules are optional models of taxes or levies. AssetBoard does not provide tax rates or tax
advice: rates are entered by you, and calculator results are estimates for planning. The tax
pages show this notice once; close it with **×** and it stays hidden for your account.

Create a rule with an effect (`added` or `withheld`), a calculation base (`net` or `gross`), and
an initial effective-dated value. Added rules always use net as their base. A value is either a
percentage (entered in your locale's number format with at most two decimals, e.g. `23` or
`12,75`, and stored as integer basis points) or a fixed amount in the workspace's base currency.

Rate versions are append-only. A new version must start after the latest version; old versions
remain unchanged and the calculator uses the latest version effective on the selected date.
Rule effect and base are fixed at creation so earlier versions keep their meaning. Create a
separate rule for a different effect or base. Archiving hides a rule from new calculations but
preserves its versions.

The calculator takes a net amount, date and recording mode:

- **Cash received (gross):** ledger amount is gross; generated payment estimates include added and
  withheld taxes.
- **Net:** ledger amount is net; generated payment estimates include withheld taxes only.
- **Take-home:** ledger amount is net after withheld taxes; no tax payment is additionally planned.

Each percentage is rounded half up to the currency's minor unit, once per rule. Added taxes are
calculated on net; withheld taxes use their selected net or gross base. Fixed values use the
workspace base currency. Rules without a version effective on the selected date are omitted from
the calculation and listed as missing.

Tax rules can be attached to income sources in F3.4. The F3.3 calculator therefore accepts a
manual net amount; it does not yet select an income source or generate scheduled tax-payment
occurrences.
