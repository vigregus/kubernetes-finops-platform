# api/

Per `docs/messenger/07-engineering-standard.md` Часть 6: types are not hand-written.
The client is generated from `packages/contracts/openapi.yaml` at build time into
`api/generated/` (gitignored) using the `typescript-fetch` generator with
`modelPropertyNaming=camelCase`, `paramNaming=camelCase`.

This directory currently holds no generated code — that wiring is G3-005 work and
needs a running backend contract to generate against. Until then, feature code reads
fixtures from `shared/lib/mock-data.ts`, typed by `shared/lib/types.ts` (itself a
stand-in for the generated types, to be deleted once codegen lands).

Thin wrappers around the generated client (auth header injection, error mapping)
belong here too, alongside the generated output.
