# Staging readiness handoff — SHU-30 first human test

**From:** SHU-50, the secret-free configuration slice.
**For:** the operator running the first human login test (Khalid).

The login implementation is merged and independently verified
(`studenthub-platform` SHU-29, merged as `e9c80c3`). The identity configuration
is now declarative and checked. What remains needs credentials and a live
environment, which this slice deliberately does not touch.

## What is ready

- The Authentik provider and application exist as a blueprint that can be applied
  and re-applied — `authentik/blueprints/studenthub-staging-oidc.yaml`.
- The gateway's nine login variables are documented with exact values, secrets
  left as placeholders — `authentik/env/studenthub-gateway.staging.env.example`.
- A readiness gate asserts the two agree with each other and with what the merged
  gateway enforces, and ten mutation tests prove the gate catches each failure it
  claims to catch. Both run in CI.

## What still needs a human, and why

None of these are things an agent should do unattended, and none were done here.

| Step | Why it is yours |
|---|---|
| 1. Confirm the two hostnames | The blueprint uses `id.staging.bawes.net` and `studenthub.staging.bawes.net`. If staging uses different names, change them in **both** files — the gate fails until they match. |
| 2. Generate the client secret in Authentik | Credential creation. Store it in the password manager, never in a ticket or a chat. |
| 3. Apply the blueprint | Requires the secret in the operator shell and access to the instance. First apply is also the first proof that the field names match your Authentik version. |
| 4. Set the gateway environment | Nine variables including `DATABASE_URL` and `OIDC_CLIENT_SECRET`. Partial configuration makes the service fail to start, by design. |
| 5. Create the test accounts | Khalid, Chahd, Mishari. Each needs an email-shaped identity, because the gateway requires an email-shaped `sub`. |
| 6. Run the test | Log in, see your own profile read-only, log out. Record feedback on SHU-30. |

## The check to run first, before debugging anything

```bash
python3 authentik/validate/validate_staging_readiness.py
```

If it fails, the configuration is wrong in a way that would have produced a
confusing runtime failure. Fix that before touching the environment.

## Failure modes worth recognising on sight

**The blueprint fails to apply.** Most likely a field-shape mismatch with your
Authentik version, or the client-secret variable missing from the *server
process* environment — see the README's Applying section. This is an apply-time
check that no amount of validation here can substitute for.

**The gateway will not start at all.** The nine login variables are
all-or-nothing; the thrown message names the missing ones.

**Login gets all the way through and then refuses, with no useful error.** The
gateway returns the same generic rejection for several distinct causes, all of
them *after* a successful token exchange. Work through them in this order rather
than assuming the first:

1. **`iss` mismatch.** Compare the ID token's `iss` claim against `OIDC_ISSUER`
   character by character, trailing slash included. The gateway compares by exact
   string equality, so a one-character difference fails every login.
2. **`sub_mode`.** It must be `user_email`. Authentik's default `hashed_user_id`
   fails the gateway's subject policy, which requires an email-shaped `sub`.
3. **Audience.** The token's `aud` must be exactly the client id, or a single-entry
   array containing it.
4. **Nonce or state binding**, if the callback was replayed or crossed sessions.

If `sub_mode` is already `user_email` and the issuer matches, stop guessing and
read the gateway logs — the audit events record a typed reason per decision.

## Boundaries observed in this slice

No live deployment, no credential entry, no identity export, no Railway teardown,
no production change, and no apply against any instance. The source-connection
mapping and export contract — the second half of SHU-50 — remains out of scope and
must be split into separately tracked work before it begins.
