from __future__ import annotations


SUPPORTED_FACT_PREDICATES: frozenset[str] = frozenset(
    {
        "DEFINED_IN",
        "IMPLEMENTS",
        "IMPORTS",
        "CALLS",
        "EXPOSES_ENDPOINT",
        "CALLS_ENDPOINT",
        "DOCUMENTS_ENDPOINT",
        "REFERENCES_DOMAIN",
        "REFERENCES_ENV_VAR",
        "REFERENCES_EVENT_CHANNEL",
        "ROUTES_DOMAIN_TO_DEPLOY",
        "DEPLOYS_VIA_CONFIG",
        "PRODUCES_EVENT",
        "CONSUMES_EVENT",
    }
)


SUPPORTED_SUPPORT_FACT_PREDICATES: frozenset[str] = frozenset(
    {
        "DECLARES_FIELD",
        "RELATES_TO_MODEL",
        "SERIALIZES_MODEL",
        "HANDLES_MODEL",
        "TASK_USES_MODEL",
        "DEFINES_AUTHZ_POLICY",
        "APPLIES_AUTHZ_POLICY",
        "USES_AUTHZ_CHECK",
        "HANDLES_ENDPOINT",
        "code_risk_signal",
    }
)


SUPPORTED_ENTITY_KINDS: frozenset[str] = frozenset(
    {
        "Service",
        "Repo",
        "CodeModule",
        "CodeSymbol",
        "ExternalPackage",
        "ExternalSymbol",
        "Endpoint",
        "Domain",
        "EnvVar",
        "DeployTarget",
        "EventChannel",
    }
)


BYTES_REF_OPTIONAL_SOURCE_SYSTEMS: frozenset[str] = frozenset(
    {
        "git",
        "pyproject",
        "package_json",
        "python_runtime",
    }
)
