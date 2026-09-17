# Regenerates lib/api/schema.d.ts from the backend's OpenAPI document.
#
# The backend (llm-observe) publishes openapi.json as a CI artifact. Set
# OPENAPI_URL to pull the current one; with it unset, the copy already
# committed at the repo root is used as-is.
#
# Never hand-edit lib/api/schema.d.ts. If a type is missing, the backend
# spec is missing it too — fix it there and re-run this.

OPENAPI_FILE ?= openapi.json
SCHEMA_OUT   ?= lib/api/schema.d.ts

.PHONY: types
types:
ifdef OPENAPI_URL
	@echo "==> fetching $(OPENAPI_URL) -> $(OPENAPI_FILE)"
	@curl -fsSL "$(OPENAPI_URL)" -o "$(OPENAPI_FILE)"
else
	@echo "==> using committed $(OPENAPI_FILE) (set OPENAPI_URL to refetch)"
endif
	@echo "==> generating $(SCHEMA_OUT)"
	@pnpm exec openapi-typescript "$(OPENAPI_FILE)" -o "$(SCHEMA_OUT)"
	@echo "==> typechecking against the new contract"
	@pnpm exec tsc --noEmit
