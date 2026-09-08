#!/usr/bin/env python3
"""Staging login readiness gate (SHU-50).

Checks that the Authentik blueprint and the gateway env template agree with each
other AND with what the merged gateway actually requires. Every rule below
corresponds to a real failure mode, named in its message.

This validates the CONTRACT, not Authentik's schema. It cannot tell you the
blueprint applies cleanly against a particular Authentik version -- only a real
apply does that, and that is SHU-30's operator step. Rules that would need a
live instance are deliberately absent rather than faked.

Exit 0 = every rule holds. Exit 1 = at least one failure, all of them printed.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT = ROOT / "blueprints" / "studenthub-staging-oidc.yaml"
ENV_TEMPLATE = ROOT / "env" / "studenthub-gateway.staging.env.example"

# createRuntimeLoginFromEnv() treats these as all-or-nothing.
REQUIRED_ENV = [
    "DATABASE_URL",
    "OIDC_ISSUER",
    "OIDC_CLIENT_ID",
    "OIDC_CLIENT_SECRET",
    "OIDC_CALLBACK_URL",
    "OIDC_AUTHORIZATION_URL",
    "OIDC_TOKEN_URL",
    "OIDC_JWKS_URL",
    "LOGIN_ALLOWED_RETURN_URLS",
]
EXACT_HTTPS_ENV = [
    "OIDC_ISSUER",
    "OIDC_CALLBACK_URL",
    "OIDC_AUTHORIZATION_URL",
    "OIDC_TOKEN_URL",
    "OIDC_JWKS_URL",
]
# Endpoints that must live on the issuer's own origin. The gateway posts the
# client secret and the authorization code to OIDC_TOKEN_URL, and trusts signing
# keys from OIDC_JWKS_URL. Valid HTTPS is not enough: a syntactically fine URL on
# someone else's host is a credential handoff to that host. The callback is
# deliberately NOT in this list -- it is the gateway's own origin, not the IdP's.
ISSUER_ORIGIN_ENV = [
    "OIDC_AUTHORIZATION_URL",
    "OIDC_TOKEN_URL",
    "OIDC_JWKS_URL",
]
SECRET_ENV = ["DATABASE_URL", "OIDC_CLIENT_SECRET"]
PLACEHOLDER = "REPLACE_ME"

# Field names that match the credential heuristic below but provably carry no
# credential. Kept explicit and short: widening this set is how a real secret
# eventually slips through, so each entry states what it actually holds.
NON_SECRET_FIELDS = frozenset({
    "access_token_validity",   # duration, e.g. "minutes=10"
    "refresh_token_validity",  # duration, e.g. "days=30"
    "access_code_validity",    # duration, e.g. "minutes=1"
})

# UNIVERSE_SUBJECT_POLICY == USER_EMAIL_SUBJECT_POLICY in @bawes/actor-assertion.
SUBJECT_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
REQUIRED_SUB_MODE = "user_email"


class Tagged:
    """Preserves an Authentik custom tag (!Env / !Context / !KeyOf) and its value."""

    def __init__(self, tag: str, value: object) -> None:
        """Record the tag name and the value it decorated."""
        self.tag = tag
        self.value = value

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"{self.tag} {self.value!r}"


def _loader() -> type[yaml.SafeLoader]:
    """A SafeLoader that keeps Authentik's custom tags instead of rejecting them.

    safe_load() raises on !Env / !Context / !KeyOf. Preserving them as Tagged is
    what lets the credential rule distinguish an !Env reference from a literal.
    """
    class BlueprintLoader(yaml.SafeLoader):
        pass

    def keep(loader: yaml.Loader, tag_suffix: str, node: yaml.Node) -> Tagged:
        """Construct any custom-tagged node as a Tagged wrapper."""
        if isinstance(node, yaml.ScalarNode):
            value: object = loader.construct_scalar(node)
        elif isinstance(node, yaml.SequenceNode):
            value = loader.construct_sequence(node)
        else:
            value = loader.construct_mapping(node)
        return Tagged(f"!{tag_suffix}", value)

    BlueprintLoader.add_multi_constructor("!", keep)
    return BlueprintLoader


def parse_env(text: str) -> dict[str, str]:
    """Read NAME=value lines, skipping blanks and comments.

    A bare `NAME=` yields an empty string, which is present-but-invalid rather
    than absent -- the callers distinguish the two.
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        values[name.strip()] = value.strip()
    return values


def resolve(value: object, context: dict[str, object]) -> object:
    """Follow a !Context reference to its value; leave anything else alone."""
    if isinstance(value, Tagged) and value.tag == "!Context":
        return context.get(str(value.value))
    return value


def check_exact_https(failures: list[str], label: str, raw: str) -> None:
    """Mirrors exactHttpsUrl() in apps/gateway/src/login-runtime.ts."""
    if raw != raw.strip():
        failures.append(f"{label}: surrounding whitespace; the gateway rejects it")
        return
    parts = urlsplit(raw)
    if parts.scheme != "https":
        failures.append(f"{label}: must use https, got {parts.scheme or '(none)'}")
    if parts.username or parts.password:
        failures.append(f"{label}: must not embed credentials")
    if parts.fragment:
        failures.append(f"{label}: must not contain a fragment")


def main() -> int:
    """Run every rule, print all failures, and return a process exit code.

    Collects rather than short-circuits: an operator should see the whole set in
    one run, not fix one and rediscover the next.
    """
    failures: list[str] = []

    blueprint = yaml.load(BLUEPRINT.read_text(), Loader=_loader())
    env = parse_env(ENV_TEMPLATE.read_text())
    context = blueprint.get("context") or {}
    entries = blueprint.get("entries") or []

    def entry(model: str) -> dict:
        """Return the blueprint entry for a model, recording a failure if absent."""
        for item in entries:
            if item.get("model") == model:
                return item
        failures.append(f"blueprint: no `{model}` entry")
        return {}

    provider = entry("authentik_providers_oauth2.oauth2provider").get("attrs") or {}

    # 1. The env template is complete and carries no real secrets.
    for name in REQUIRED_ENV:
        if name not in env:
            failures.append(
                f"env template: missing {name}; the nine login variables are "
                "all-or-nothing and the gateway throws on a partial set"
            )
        elif not env[name].strip():
            # `NAME=` parses as present with an empty value. The gateway rejects
            # that at startup; the gate should reject it here, before deployment.
            failures.append(
                f"env template: {name} is present but empty; supply a value or "
                "a placeholder, never a bare assignment"
            )
    for name in SECRET_ENV:
        # Membership, not truthiness: an empty value must not skip this rule.
        if name in env and env[name].strip() and env[name] != PLACEHOLDER:
            failures.append(
                f"env template: {name} must stay {PLACEHOLDER}; this file is committed"
            )

    # 2. Every URL the gateway parses with exactHttpsUrl() survives it.
    for name in EXACT_HTTPS_ENV:
        if name in env:
            check_exact_https(failures, f"env {name}", env[name])
    for item in [part for part in env.get("LOGIN_ALLOWED_RETURN_URLS", "").split(",") if part]:
        check_exact_https(failures, "env LOGIN_ALLOWED_RETURN_URLS entry", item.strip())

    # 2b. Every IdP endpoint sits on the issuer's origin. Syntactically valid
    #     HTTPS on a foreign host would hand the client secret and the
    #     authorization code to that host.
    issuer_raw = env.get("OIDC_ISSUER", "")
    if issuer_raw:
        issuer_parts = urlsplit(issuer_raw)
        issuer_origin = (issuer_parts.scheme, issuer_parts.netloc)
        for name in ISSUER_ORIGIN_ENV:
            if name not in env:
                continue
            parts = urlsplit(env[name])
            if (parts.scheme, parts.netloc) != issuer_origin:
                failures.append(
                    f"env {name}: origin {parts.scheme}://{parts.netloc} does not match "
                    f"the issuer origin {issuer_parts.scheme}://{issuer_parts.netloc}; "
                    "the gateway posts the client secret and authorization code to the "
                    "token endpoint and trusts signing keys from the JWKS endpoint"
                )

    # 3. The callback path is not free-form: the gateway refuses to start otherwise.
    callback = env.get("OIDC_CALLBACK_URL", "")
    if callback and urlsplit(callback).path != "/login/callback":
        failures.append(
            "env OIDC_CALLBACK_URL: pathname must be exactly /login/callback"
        )

    # 4. Blueprint and env agree. A drift here is a login that fails in staging
    #    while both files look individually correct.
    pairs = [
        ("client_id", "OIDC_CLIENT_ID"),
    ]
    for attr, env_name in pairs:
        blueprint_value = resolve(provider.get(attr), context)
        if blueprint_value != env.get(env_name):
            failures.append(
                f"drift: blueprint provider {attr}={blueprint_value!r} but "
                f"{env_name}={env.get(env_name)!r}"
            )

    redirects = provider.get("redirect_uris") or []
    redirect_urls = [str(resolve(item.get("url"), context)) for item in redirects if isinstance(item, dict)]
    if callback and callback not in redirect_urls:
        failures.append(
            f"drift: OIDC_CALLBACK_URL={callback!r} is not among the provider's "
            f"redirect_uris {redirect_urls!r}; Authentik would refuse the callback"
        )
    for item in redirects:
        if isinstance(item, dict) and item.get("matching_mode") != "strict":
            failures.append(
                f"provider redirect_uris: matching_mode must be strict, got "
                f"{item.get('matching_mode')!r}; a looser mode widens the exact-callback control"
            )

    issuer_context = context.get("studenthub_issuer")
    if issuer_context != env.get("OIDC_ISSUER"):
        failures.append(
            f"drift: blueprint context studenthub_issuer={issuer_context!r} but "
            f"OIDC_ISSUER={env.get('OIDC_ISSUER')!r}; `iss` is compared by exact string equality"
        )

    # 5. The subject mode the gateway's policy actually accepts.
    sub_mode = provider.get("sub_mode")
    if sub_mode != REQUIRED_SUB_MODE:
        failures.append(
            f"provider sub_mode={sub_mode!r}, must be {REQUIRED_SUB_MODE!r}: "
            "UNIVERSE_SUBJECT_POLICY requires an email-shaped `sub`, and "
            "Authentik's default (hashed_user_id) fails subjectPolicy AFTER a "
            "successful token exchange, with a generic rejection"
        )
    if not SUBJECT_PATTERN.match("operator@example.invalid"):  # guards the pattern itself
        failures.append("internal: subject pattern does not accept an email-shaped subject")

    # 6. No secret material in the blueprint, at any depth.
    secret = provider.get("client_secret")
    if not isinstance(secret, Tagged) or secret.tag != "!Env":
        failures.append(
            f"provider client_secret must be an !Env reference, got {secret!r}; "
            "a literal here would commit a credential"
        )

    def scan(node: object, path: str) -> None:
        """Flag a plain string sitting under a credential-shaped field name.

        A !Env / !KeyOf reference is a Tagged instance, not a str, so it never
        reaches the string branch -- which is exactly the distinction we want.
        """
        if isinstance(node, dict):
            for key, value in node.items():
                scan(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                scan(value, f"{path}[{index}]")
        elif isinstance(node, str):
            field = path.rsplit(".", 1)[-1].lower()
            suspicious = any(word in field for word in ("secret", "password", "token", "key"))
            if suspicious and field not in NON_SECRET_FIELDS:
                failures.append(
                    f"blueprint: possible inline credential at {path} "
                    f"(add to NON_SECRET_FIELDS only if it provably carries no credential)"
                )

    scan({"entries": entries, "context": context}, "blueprint")

    # 6b. The blueprint never creates key material. An `entry` for a keypair
    #     defaults to `state: present`, which CREATES the object when absent --
    #     so a blueprint that "references" a signing key by entry silently
    #     generates one. Only a fail-closed !Find lookup is acceptable.
    for item in entries:
        if item.get("model") == "authentik_crypto.certificatekeypair":
            failures.append(
                "blueprint: an entry for authentik_crypto.certificatekeypair would "
                "CREATE key material when absent (state defaults to present); "
                "reference an existing keypair with !Find instead"
            )
    signing_key = provider.get("signing_key")
    if not isinstance(signing_key, Tagged) or signing_key.tag != "!Find":
        failures.append(
            f"provider signing_key must be a fail-closed !Find lookup, got {signing_key!r}; "
            "anything else can bring a keypair into existence rather than requiring one"
        )

    # 7. The confidential client the gateway authenticates as.
    if provider.get("client_type") != "confidential":
        failures.append(
            f"provider client_type={provider.get('client_type')!r}, must be "
            "'confidential': the gateway exchanges the code server-side with a secret"
        )

    if failures:
        print(f"staging readiness: {len(failures)} failure(s)\n")
        for item in failures:
            print(f"  FAIL  {item}")
        return 1

    print("staging readiness: all checks passed")
    print(f"  blueprint     {BLUEPRINT.relative_to(ROOT.parent)}")
    print(f"  env template  {ENV_TEMPLATE.relative_to(ROOT.parent)}")
    print(f"  issuer        {env.get('OIDC_ISSUER')}")
    print(f"  callback      {callback}")
    print(f"  sub_mode      {sub_mode}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
