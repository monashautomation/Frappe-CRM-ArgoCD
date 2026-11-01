# Frappe CRM - production setup

Same shape as `directory-service`: Kustomize in `deploy/`, ArgoCD Application in `argocd/`, OpenBao via External Secrets, Traefik IngressRoute, `nfs-client` storage, image built to GHCR by GitHub Actions.

Served at `https://crm.monashautomation.com` (CRM, lands on `/crm`) and `https://desk.monashautomation.com` (Frappe desk / central control area, lands on `/desk`), namespace `frappe-crm`. Both hostnames are one Frappe site (`SITE_NAME` stays `crm.monashautomation.com`).

## How it fits together

| Wave | What                                                                                                                         |
| ---- | ---------------------------------------------------------------------------------------------------------------------------- |
| 0    | ExternalSecrets, ConfigMap, PVCs, MariaDB, Redis (cache + queue)                                                             |
| 1    | `frappe-crm-bootstrap` hook Job: writes site config, creates site + installs CRM (first run) or `bench migrate`, configures SSO |
| 2    | gunicorn, nginx, socketio, 3 workers, scheduler                                                                              |
| 3    | Traefik IngressRoute (`crm.monashautomation.com`)                                                                                |

CRM is not in the stock `frappe/erpnext` image, so `image/apps.json` + `.github/workflows/build-image.yml` build a Frappe v16 image with CRM baked in (frappe_docker layered build). Pushing to `main` with changes under `image/`, a weekly cron, or a manual run rebuilds it and commits the new tag to `deploy/overlays/prod/kustomization.yaml`.

## SSO, provisioning and role mapping

Login uses Frappe's **Social Login Key** `authentik` plus the third-party app [frappe-oidc-extended](https://github.com/monashautomation/frappe-oidc-extended) (org fork of the upstream project, with a one-line `setup.py` fix), baked into the image (`image/apps.json`). Frappe on its own ignores the `groups` claim; this app adds it.

- The bootstrap Job writes the Social Login Key from OpenBao and creates the **OIDC Extended Configuration** and **Role Profiles** from the `SSO_ROLE_MAPPING` JSON in `deploy/base/configmap.yaml`. Config is code and is re-applied on every Sync.
- On **every login** the app creates the user if needed (as a System User, username = Authentik `preferred_username`) and sets their **Role Profiles** from their Authentik groups: groups listed in `group_role_profiles` map to profiles; users in none of them get `fallback_role_profiles`. Changes in Authentik take effect at the user's next login.
- Default mapping: everyone gets `CRM Sales User` (role `Sales User`); members of the Authentik group `crm-managers` get `CRM Sales Manager`. **Edit the group names in the ConfigMap to match your groups**, merge, and Sync.
- **Who can log in at all is still controlled in Authentik** (application policy bindings). Anyone Authentik lets through gets at least the fallback profile.
- The app refuses OIDC login for a user named `Administrator`. Use the local login for that account.
- `System Manager` is deliberately not mapped; grant it by hand in Frappe if you want it.
- If the running image doesn't contain `oidc_extended` yet, the Job logs a warning and uses Frappe's standard callback (default role `Sales User`) so login still works.

### Rolling out the app (order matters)

1. Merge the PR. Pushing `image/apps.json` triggers the image build, which commits a new `sha-...` tag.
2. In Authentik, **add** the new redirect URI (keep the old one until the cutover is done), and add the `groups` scope: see step 2.
3. ArgoCD syncs the new image; the Job installs the app and switches the Social Login Key to the new callback. Check the Job log ends with `SSO configured: mode=extended ...`.
4. Remove the old redirect URI from Authentik.

Risks: the app is small, unreleased and tracks its `main` branch (last upstream activity checked: March 2026). Fork it into the org and point `apps.json` at the fork to pin it. It does not verify the ID token signature (it relies on the TLS-protected token endpoint) and its role sync assumes Frappe's `role_profiles` model (v16).

## 1. GitHub repo

1. Repo is `monashautomation/Frappe-CRM-ArgoCD`. Settings -> Actions -> General -> Workflow permissions -> **Read and write** (push to GHCR, commit tag bump).
2. Push `main`, then run the **Build image and deploy** workflow once (Actions -> Run workflow). The first sync needs an image that exists.
3. Keep the GHCR package `frappe-crm` **private** (default). Pods pull it with `ghcr-pull-secret`, which an ExternalSecret builds from OpenBao (step 3), so nothing is copied into the cluster by hand.

## 2. Authentik

At `https://auth.monashautomation.com/if/admin/`:

1. **Applications -> Providers -> Create -> OAuth2/OpenID Provider**
   - Name `frappe-crm`, same authorization flow as directory-service.
   - Client type **Confidential**.
   - Redirect URI, **Strict**, type **Authorization**:
     `https://crm.monashautomation.com/api/method/oidc_extended.callback.custom/authentik`
     (During migration from the standard callback, also keep
     `https://crm.monashautomation.com/api/method/frappe.integrations.oauth2_logins.custom/authentik` until the new one works, then delete it.
     authentik < 2026.5 treats every redirect URI as Authorization; add no post-logout URI.)
   - Scopes: `openid`, `email`, `profile`, and the **groups** scope mapping (the same custom `groups` mapping directory-service uses). The ID token must carry `groups`, `email`, `preferred_username`, `given_name` and `family_name`; check the provider includes claims in the ID token (default).
   - Copy Client ID and Client Secret.
2. **Applications -> Create**: name `Frappe CRM`, slug `frappe-crm`, the provider above, launch URL `https://crm.monashautomation.com/crm`.
3. **Policy bindings on the application**: bind the group(s) allowed to use CRM (see warning above).

## 3. OpenBao

In the OpenBao UI (`https://openbao.monashautomation.com/`), enable a KV v2 engine at path `frappe-crm`, then create:

- `frappe-crm/web`

  | Key                       | Value                       |
  | ------------------------- | --------------------------- |
  | `ADMIN_PASSWORD`          | _random_ (Frappe Administrator) |
  | `AUTHENTIK_CLIENT_ID`     | from step 2                 |
  | `AUTHENTIK_CLIENT_SECRET` | from step 2                 |

- `frappe-crm/mariadb`

  | Key                     | Value    |
  | ----------------------- | -------- |
  | `MARIADB_ROOT_PASSWORD` | _random_ |

- `frappe-crm/ghcr`

  | Key        | Value                                                              |
  | ---------- | ------------------------------------------------------------------ |
  | `username` | GitHub username (or bot account) the token belongs to              |
  | `token`    | GitHub personal access token (classic) with only `read:packages`   |

  Use the same token the other apps' `ghcr-pull-secret` was made from, or a new one from a bot/service account. If the org enforces SAML SSO, authorize the token for the org.

Policy `frappe-crm-read`:

```hcl
path "frappe-crm/data/*" {
  capabilities = ["read"]
}
```

Kubernetes auth role `frappe-crm`: service account `frappe-crm-eso`, namespace `frappe-crm`, policy `frappe-crm-read`, TTL as the other apps.

## 4. Cluster

Nothing to do by hand. ArgoCD creates the `frappe-crm` namespace (`CreateNamespace=true`) and everything in it.

## 5. DNS / tunnel / routing

`crm.monashautomation.com` and `desk.monashautomation.com` must both reach the same Traefik as the other apps (wildcard record or Cloudflare Tunnel hostname). TLS ends at the tunnel, as for the other apps, so there is no cert-manager certificate.

Routing **is** in this repo: `deploy/base/ingressroute.yaml` is a Traefik `IngressRoute` for `crm.monashautomation.com` -> `frappe-crm-nginx:80`, deployed by ArgoCD with the app (wave 3). Traefik picks up IngressRoutes from any namespace, so nothing needs adding to a central routes file.

Do not add the `authentik-global-auth` forward-auth middleware: Frappe does its own OIDC login, and forward auth in front of it would break the OAuth callback and API calls.

## 6. Register with ArgoCD (UI)

In the ArgoCD UI (`https://argocd.monashautomation.com`): **+ New App** -> **Edit as YAML**, paste the contents of `argocd/application.yaml`, then **Save** and **Create**. (Or fill the form: name `frappe-crm`, project `default`, sync policy Automatic with Prune + Self Heal + Auto-Create Namespace, repo `https://github.com/monashautomation/Frappe-CRM-ArgoCD.git`, revision `main`, path `deploy/overlays/prod`, cluster `https://kubernetes.default.svc`, namespace `frappe-crm`.)

If the repo is private, add it first under Settings -> Repositories (same credentials as `directory-service`).

## 7. First deploy checklist

1. Image tag in `deploy/overlays/prod/kustomization.yaml` is a real `sha-...` (not `latest`).
2. In the ArgoCD UI open the `frappe-crm` app. Wave 0 (secrets, DB, Redis) goes green, then the `frappe-crm-bootstrap` Job runs; click it -> **Logs** to watch. First run takes several minutes (site creation + CRM install). The Frappe pods appear only after it succeeds.
3. Open `https://crm.monashautomation.com/crm`, click the Authentik login, confirm you land in CRM as a Sales User.
4. In Frappe, check **Social Login Key -> authentik** exists and **Portal Settings -> Default Role** is `Sales User`.
5. Promote yourself: log in as `Administrator` (password from OpenBao `ADMIN_PASSWORD`), give your SSO user `Sales Manager` + `System Manager`.
6. Once happy, optionally set `DISABLE_PASSWORD_LOGIN: "1"` in `base/configmap.yaml`.

## Troubleshooting

- **Authentik: "redirect URI mismatch"**: the URI must match exactly, including `https`. The Job sets `host_name` so Frappe sends https; check the bootstrap Job logs for the `set-config host_name` step, or in Frappe open the Social Login Key and confirm the redirect URL.
- **Login works but lands on a blank/portal page**: user got no role. Check Portal Settings default role, then add `Sales User` to the user by hand; existing users are not backfilled.
- **Bootstrap Job fails midway on first run**: if `bench new-site` died after creating the DB, the retry fails with "database already exists". Fix: in the ArgoCD UI delete the `frappe-crm-mariadb-data` PVC and the `frappe-crm-sites` PVC (right-click -> Delete; prune/self-heal recreates them empty, MariaDB restarts fresh), then re-sync. Only safe before any real data exists.
- **Pods CrashLoop on first sync**: they only start after the hook finishes (wave 2); if the Job is failing they will not exist yet.

## Not done / decisions to revisit

- **Upstream drift for `oidc_extended`**: the org fork tracks `main`; periodically merge upstream changes, or pin `image/apps.json` to a tag on the fork.
- **Backups**: MariaDB is a single pod on NFS with no backup job. Add a `bench backup` CronJob or mysqldump to your usual target.
- **CRM version**: `image/apps.json` tracks CRM `main`, so a weekly rebuild can bring breaking changes. Pin a tag/branch there once stable.
- Frappe/ERPNext-on-NFS sites volume is shared RWX; fine for this scale.
