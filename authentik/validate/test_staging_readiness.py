#!/usr/bin/env python3
"""Mutation tests for the staging readiness gate (SHU-50).

A checker that cannot fail proves nothing. Each case below breaks exactly one
property of the real blueprint or env template in a temporary copy, and requires
the validator to reject it with a message naming that property. The last case
requires the unmutated pair to pass, so the suite cannot go green by making the
validator reject everything.

Run: python3 authentik/validate/test_staging_readiness.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
AUTHENTIK = HERE.parent
BLUEPRINT_REL = Path("blueprints/studenthub-staging-oidc.yaml")
ENV_REL = Path("env/studenthub-gateway.staging.env.example")

# (name, file, find, replace, expected substring in a failure line)
CASES = [
    (
        "sub_mode defaulted to hashed_user_id",
        BLUEPRINT_REL,
        "sub_mode: user_email",
        "sub_mode: hashed_user_id",
        "sub_mode",
    ),
    (
        "redirect matching loosened from strict",
        BLUEPRINT_REL,
        "matching_mode: strict",
        "matching_mode: regex",
        "matching_mode must be strict",
    ),
    (
        "client secret inlined instead of an !Env reference",
        BLUEPRINT_REL,
        "client_secret: !Env AUTHENTIK_STUDENTHUB_STAGING_CLIENT_SECRET",
        "client_secret: hunter2-not-a-real-secret",
        "client_secret must be an !Env reference",
    ),
    (
        "confidential client downgraded to public",
        BLUEPRINT_REL,
        "client_type: confidential",
        "client_type: public",
        "client_type",
    ),
    (
        "issuer drifts between blueprint and env by a trailing slash",
        ENV_REL,
        "OIDC_ISSUER=https://id.staging.bawes.net/application/o/studenthub-staging/",
        "OIDC_ISSUER=https://id.staging.bawes.net/application/o/studenthub-staging",
        "studenthub_issuer",
    ),
    (
        "callback path is not /login/callback",
        ENV_REL,
        "OIDC_CALLBACK_URL=https://studenthub.staging.bawes.net/login/callback",
        "OIDC_CALLBACK_URL=https://studenthub.staging.bawes.net/oauth/callback",
        "/login/callback",
    ),
    (
        "an endpoint downgraded to http",
        ENV_REL,
        "OIDC_TOKEN_URL=https://id.staging.bawes.net/application/o/token/",
        "OIDC_TOKEN_URL=http://id.staging.bawes.net/application/o/token/",
        "must use https",
    ),
    (
        "a required variable removed",
        ENV_REL,
        "OIDC_JWKS_URL=https://id.staging.bawes.net/application/o/studenthub-staging/jwks/",
        "# OIDC_JWKS_URL removed",
        "missing OIDC_JWKS_URL",
    ),
    (
        "a real secret committed into the example",
        ENV_REL,
        "OIDC_CLIENT_SECRET=REPLACE_ME",
        "OIDC_CLIENT_SECRET=an-actual-looking-secret-value",
        "must stay REPLACE_ME",
    ),
    (
        "token endpoint moved to a foreign HTTPS origin",
        ENV_REL,
        "OIDC_TOKEN_URL=https://id.staging.bawes.net/application/o/token/",
        "OIDC_TOKEN_URL=https://attacker.invalid/token",
        "does not match",
    ),
    (
        "JWKS endpoint moved to a foreign HTTPS origin",
        ENV_REL,
        "OIDC_JWKS_URL=https://id.staging.bawes.net/application/o/studenthub-staging/jwks/",
        "OIDC_JWKS_URL=https://attacker.invalid/jwks",
        "does not match",
    ),
    (
        "DATABASE_URL present but empty",
        ENV_REL,
        "DATABASE_URL=REPLACE_ME",
        "DATABASE_URL=",
        "present but empty",
    ),
    (
        "OIDC_CLIENT_SECRET present but empty",
        ENV_REL,
        "OIDC_CLIENT_SECRET=REPLACE_ME",
        "OIDC_CLIENT_SECRET=",
        "present but empty",
    ),
    (
        "signing key downgraded from a !Find lookup to a creating entry",
        BLUEPRINT_REL,
        '      signing_key: !Find [authentik_crypto.certificatekeypair, [name, "authentik Self-signed Certificate"]]',
        "      signing_key: !KeyOf studenthub-signing-key",
        "signing_key must be a fail-closed !Find",
    ),
    (
        "client id drifts between blueprint and env",
        ENV_REL,
        "OIDC_CLIENT_ID=studenthub-staging",
        "OIDC_CLIENT_ID=studenthub-production",
        "drift",
    ),
]


def run(root: Path) -> tuple[int, str]:
    """Run the validator against a tree and return its exit code and output."""
    result = subprocess.run(
        [sys.executable, str(root / "validate" / "validate_staging_readiness.py")],
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout + result.stderr


def main() -> int:
    """Apply each mutation to a fresh copy and require the named rule to reject it."""
    failures: list[str] = []

    for name, rel, find, replace, expected in CASES:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "authentik"
            shutil.copytree(AUTHENTIK, root)
            target = root / rel
            text = target.read_text()

            # The mutation must LAND. A find string that no longer matches would
            # leave the input pristine, and the validator's pass would read as
            # "this rule is unbound" -- a false finding, not a result.
            if text.count(find) != 1:
                failures.append(f"{name}: anchor matched {text.count(find)} times, expected 1")
                continue
            target.write_text(text.replace(find, replace))

            code, output = run(root)
            if code == 0:
                failures.append(f"{name}: validator PASSED a broken configuration")
            elif expected not in output:
                failures.append(
                    f"{name}: rejected, but no message mentioned {expected!r}\n{output}"
                )
            else:
                print(f"  ok    {name}")

    # The control: unmutated input must pass, or every case above is vacuous.
    code, output = run(AUTHENTIK)
    if code != 0:
        failures.append(f"control: the real configuration does not pass\n{output}")
    else:
        print("  ok    control: the real configuration passes")

    if failures:
        print(f"\n{len(failures)} failure(s)")
        for item in failures:
            print(f"  FAIL  {item}")
        return 1
    print(f"\n{len(CASES)} mutations rejected, control passes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
