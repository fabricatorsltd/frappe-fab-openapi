# FAB OpenAPI

Reusable OpenAPI backend services for FAB and other Frappe apps.

## Scope

`fab_openapi` isolates OpenAPI-specific transport and authentication concerns so
business-domain apps do not need to embed provider logic directly.

Current responsibilities include:

- reusable **OpenAPI Connection** configuration
- seeded **SDI Sandbox** and **SDI Production** connection records
- token handling and authenticated SDI API client utilities
- neutral request primitives that domain apps can build on

The app intentionally does **not** own invoice lifecycle, ERPNext custom
fields, or FAB-specific operator workflows.

## Branches

- `develop`: integration branch for testing against Frappe/ERPNext `develop`
- `version-16`: stable branch for Frappe/ERPNext 16

## Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/fabricatorsltd/frappe-fab-openapi.git --branch version-16
bench --site [site] install-app fab_openapi
```

## Development

```bash
cd apps/fab_openapi
pre-commit install
```

Pre-commit is configured for Ruff, ESLint, Prettier, and PyUpgrade.

## License

GNU Affero General Public License v3.0
