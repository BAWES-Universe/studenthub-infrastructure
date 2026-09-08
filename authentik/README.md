# Authentik configuration-as-code — staging

Declarative identity configuration for **Continue with Universe** on staging
(SHU-50, unblocking SHU-30). Secret-free by construction and checked by CI.

| Path | What it is |
|---|---|
| `blueprints/studenthub-staging-oidc.yaml` | The Authentik OIDC provider and application |
| `env/studenthub-gateway.staging.env.example` | The gateway's nine login variables, placeholders only |
| `validate/validate_staging_readiness.py` | The readiness gate — asserts the two agree with each other and with the gateway |
| `validate/test_staging_readiness.py` | Mutation tests proving each rule in the gate can actually fail |

```bash
python3 authentik/validate/validate_staging_readiness.py   # the gate
python3 authentik/validate/test_staging_readiness.py       # proves the gate bites
```

Both run in CI on every push and pull request. No dependencies beyond Python 3
and PyYAML, both present on the runner.

## Why a validator rather than a checklist

The gateway's login configuration is unusually intolerant, and the expensive
failures are the quiet ones. Three examples the gate covers, each a real failure
mode rather than a hypothetical:

- **`sub_mode` must be `user_email`.** The gateway applies
  `UNIVERSE_SUBJECT_POLICY`, which requires an email-shaped `sub`. Authentik's
  default is `hashed_user_id`. Get this wrong and every login fails *after* a
  successful token exchange, at `config.subjectPolicy(claims.sub)` — a generic
  rejection with nothing in it pointing at the cause.
- **The issuer is compared by exact string equality.** A trailing slash present
  in Authentik and absent in `OIDC_ISSUER` breaks every login, and both files
  look correct in isolation.
- **The nine login variables are all-or-nothing.** Set none and login routes stay
  disabled; set some and the gateway throws at startup. There is no partial mode.

A markdown checklist states these. The gate establishes them, and its mutation
tests establish that the gate would notice if they stopped being true.

## Applying

The blueprint is applied by the deployed Authentik **server process**, not from
this repository. That distinction matters for the secret, and is the step most
easily got wrong.

`!Env` is resolved by Authentik at apply time, so
`AUTHENTIK_STUDENTHUB_STAGING_CLIENT_SECRET` must be present **in the environment
of the process running the apply** — the server container. Exporting it in your
own shell and then running `docker exec ... ak apply_blueprint` does *not* carry
it across: the apply will fail to resolve the tag.

Read the value without putting it in shell history:

```bash
read -rs AUTHENTIK_STUDENTHUB_STAGING_CLIENT_SECRET   # prompts, echoes nothing
export AUTHENTIK_STUDENTHUB_STAGING_CLIENT_SECRET
```

Then get it into the server process by whichever path your deployment uses:

- **Compose:** add it to the Authentik server service's `environment:` or
  `env_file:`, recreate the container, then apply.
- **Kubernetes:** add it to the server Deployment's env, sourced from a
  `secretKeyRef`, then apply.
- **One-off exec:** pass it explicitly into the exec environment, e.g.
  `docker exec -e AUTHENTIK_STUDENTHUB_STAGING_CLIENT_SECRET ... ak apply_blueprint ...`,
  which forwards the value from your shell without embedding it in the command.

Either mount the blueprint into the instance's blueprint directory and let it
reconcile, or apply it explicitly once the variable is in place.

Nothing in this repository holds that value, and nothing here reaches a live
instance. `.env*`, `*.key` and `*.pem` are gitignored; the gate independently
fails on a literal secret in either file.

## Recovery

The recovery property is that this blueprint is the whole provider definition:
applying it to an empty Authentik recreates the provider and application with
identical client id, issuer path, redirect URI, scopes and subject mode. Only two
things must be restored out of band, because neither belongs in a repository:

1. **The client secret**, from the password manager.
2. **The signing keypair**, which must exist *before* the blueprint is applied.
   `signing_key` is a `!Find` lookup, so an apply against an instance with no
   keypair of that name **fails** rather than quietly generating one. That is
   deliberate: a blueprint that can bring key material into existence is a
   blueprint that can silently replace your signing identity.

   So the order on a rebuilt instance is: restore the keypair from the Authentik
   backup (or create one under the same name in the UI), *then* apply. If you
   create a new keypair rather than restoring the original, previously issued ID
   tokens stop validating — which is correct, and worth knowing before you do it.

## What this cannot tell you

The gate validates the **contract**: that the blueprint and the gateway env agree,
and that both satisfy what the merged gateway actually enforces. It does not, and
cannot, tell you the blueprint applies cleanly against a particular Authentik
version — field names and the `redirect_uris` shape have changed across releases.
Only a real apply proves that, and that is an operator step in SHU-30.

Rules that would need a live instance are absent rather than faked.
