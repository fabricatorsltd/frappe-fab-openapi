# fab OpenAPI

Reusable OpenAPI backend services for fab and other Frappe apps.

## Scope

`fab_openapi` isolates OpenAPI-specific transport and authentication concerns so
business-domain apps do not need to embed provider logic directly.

Current responsibilities include:

- reusable **OpenAPI Connection** configuration
- seeded **SDI Sandbox** and **SDI Production** connection records
- token handling and authenticated SDI API client utilities
- neutral request primitives that domain apps can build on

The app intentionally does **not** own invoice lifecycle, ERPNext custom
fields, or fab-specific operator workflows.

## How it works

- An **OpenAPI Connection** record holds the environment, base URLs, and
  credentials (Password fields, never plain text). SDI Sandbox and SDI
  Production records are seeded on install; start every rollout against
  Sandbox.
- `clients/sdi.py` wraps the openapi.it SDI API: authentication, token
  caching, send and receive primitives for FatturaPA payloads.
- `integrations/fab_italy_edi.py` exposes the client as a transport channel
  for `fab_italy_edi`, which keeps the invoice lifecycle and operator
  workflows on its side of the boundary.

## Branches

- `develop`: integration branch for testing against Frappe/ERPNext `develop`
- `version-16`: stable branch for Frappe/ERPNext 16

## Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/fabricatorsltd/frappe-fab-openapi.git --branch version-16
bench --site [site] install-app fab_openapi
```

## Contributing

Follow the official Frappe contribution guidelines:

- <https://github.com/frappe/erpnext/wiki/Contribution-Guidelines>

Contributions should track the upstream Frappe process for proposals, coding
standards, pull request quality, and documentation updates.

## Development

```bash
cd apps/fab_openapi
pre-commit install
```

Pre-commit is configured for Ruff, ESLint, Prettier, and PyUpgrade.

## License

GNU Affero General Public License v3.0
