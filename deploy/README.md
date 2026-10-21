# Frappe CRM - production setup

Same shape as `directory-service`: Kustomize in `deploy/`, ArgoCD Application in `argocd/`, OpenBao via External Secrets, Traefik + cert-manager ingress, `nfs-client` storage, image built to GHCR by GitHub Actions.

Served at `https://crm.monashautomation.com`, namespace `frappe-crm`.

## How it fits together

| Wave | What                                                                                                                         |
| ---- | ---------------------------------------------------------------------------------------------------------------------------- |
| 0    | ExternalSecrets, ConfigMap, PVCs, MariaDB, Redis (cache + queue)                                                             |
| 1    | `frappe-crm-bootstrap` hook Job: writes site config, creates site + installs CRM (first run) or `bench migrate`, configures SSO |
| 2    | gunicorn, nginx, socketio, 3 workers, scheduler                                                                              |
| 3    | Ingress                                                                                                                      |

CRM is not in the stock `frappe/erpnext` image, so `image/apps.json` + `.github/workflows/build-image.yml` build a Frappe v16 image with CRM baked in (frappe_docker layered build). Pushing to `main` with changes under `image/`, a weekly cron, or a manual run rebuilds it and commits the new tag to `deploy/overlays/prod/kustomization.yaml`.

## SSO and automatic provisioning

- The bootstrap Job creates a Frappe **Social Login Key** `authentik` (Sign ups = Allow) from the OpenBao secrets, so the config is code, not click-ops, and is re-applied every sync.
- First Authentik login creates the Frappe user automatically. Frappe creates these as _Website Users with no roles_, which cannot open `/crm`, so the Job sets **Portal Settings -> Default Role = Sales User** (desk access, so Frappe promotes them to System User).
- **Access control lives in Authentik.** Anyone who can pass authorization for the application gets a CRM account. Bind the application to the groups that should have CRM (policy bindings), do not leave it open to all users.
- Promotion to **Sales Manager** / System Manager is manual in Frappe (Users -> roles). Frappe cannot map Authentik groups to roles natively; if you want that, it needs a small hook (see "Not done").
- Existing Frappe users match by email, so `Administrator` keeps working with its password.

## 1. GitHub repo

1. Repo is `monashautomation/Frappe-CRM-ArgoCD`. Settings -> Actions -> General -> Workflow permissions -> **Read and write** (push to GHCR, commit tag bump).
2. Push `main`, then run the **Build image and deploy** workflow once (Actions -> Run workflow). The first sync needs an image that exists.
3. After the first build, set the GHCR package `frappe-crm` visibility/permissions as you do for `directory-web`.

## 2. Authentik

At `https://auth.monashautomation.com/if/admin/`:

1. **Applications -> Providers -> Create -> OAuth2/OpenID Provider**
   - Name `frappe-crm`, same authorization flow as directory-service.
   - Client type **Confidential**.
   - Redirect URI, **Strict**, type **Authorization**:
     `https://crm.monashautomation.com/api/method/frappe.integrations.oauth2_logins.custom/authentik`
     (authentik < 2026.5 treats every redirect URI as Authorization; add only this one and no post-logout URI.)
   - Scopes: `openid`, `email`, `profile`. Frappe reads `email` from userinfo, so the email scope mapping must be present.
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

Policy `frappe-crm-read`:

```hcl
path "frappe-crm/data/*" {
  capabilities = ["read"]
}
```

Kubernetes auth role `frappe-crm`: service account `frappe-crm-eso`, namespace `frappe-crm`, policy `frappe-crm-read`, TTL as the other apps.

## 4. Cluster

Create the namespace (or wait for the first sync) and copy the GHCR pull secret, same as directory-service:

```bash
kubectl create namespace frappe-crm --dry-run=client -o yaml | kubectl apply -f -
kubectl get secret ghcr-pull-secret -n ticketing -o json \
  | jq 'del(.metadata.namespace, .metadata.resourceVersion, .metadata.uid, .metadata.creationTimestamp, .metadata.ownerReferences, .metadata.managedFields, .metadata.annotations)' \
  | kubectl apply -n frappe-crm -f -
```

## 5. DNS / tunnel / routing

`crm.monashautomation.com` must reach the same Traefik as the other apps (wildcard record or Cloudflare Tunnel hostname).

This repo ships a standard `Ingress` (Traefik class + cert-manager), as `directory-service` does. If you instead route apps through the central `IngressRoute` file (as for `kan`, `inventory`, etc.), delete `deploy/base/ingress.yaml` (and its line in `kustomization.yaml`) and add this to that file:

```yaml
---
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: frappe-crm-route
  namespace: frappe-crm
spec:
  entryPoints:
    - websecure
  routes:
  - match: Host(`crm.monashautomation.com`)
    kind: Rule
    services:
    - name: frappe-crm-nginx
      port: 80
      scheme: http
```

Do not add the `authentik-global-auth` forward-auth middleware: Frappe does its own OIDC login, and forward auth in front of it would break the OAuth callback and API calls.

## 6. Register with ArgoCD

```bash
kubectl apply -f argocd/application.yaml
```

## 7. First deploy checklist

1. Image tag in `deploy/overlays/prod/kustomization.yaml` is a real `sha-...` (not `latest`).
2. ArgoCD sync: watch `kubectl logs -n frappe-crm job/frappe-crm-bootstrap -f`. First run takes several minutes (site creation + CRM install).
3. Open `https://crm.monashautomation.com/crm`, click the Authentik login, confirm you land in CRM as a Sales User.
4. In Frappe, check **Social Login Key -> authentik** exists and **Portal Settings -> Default Role** is `Sales User`.
5. Promote yourself: log in as `Administrator` (password from OpenBao `ADMIN_PASSWORD`), give your SSO user `Sales Manager` + `System Manager`.
6. Once happy, optionally set `DISABLE_PASSWORD_LOGIN: "1"` in `base/configmap.yaml`.

## Troubleshooting

- **Authentik: "redirect URI mismatch"**: the URI must match exactly, including `https`. The Job sets `host_name` so Frappe sends https; confirm with `bench --site crm.monashautomation.com get-config host_name` (or look at `site_config.json`).
- **Login works but lands on a blank/portal page**: user got no role. Check Portal Settings default role, then add `Sales User` to the user by hand; existing users are not backfilled.
- **Bootstrap Job fails midway on first run**: if `bench new-site` died after creating the DB, the retry fails with "database already exists". Drop it: `kubectl exec -n frappe-crm deploy/frappe-crm-mariadb -- mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" -e 'show databases'`, drop the `_xxxx` DB and `rm -rf sites/<site>` from a Frappe pod, then re-sync.
- **Pods CrashLoop on first sync**: they only start after the hook finishes (wave 2); if the Job is failing they will not exist yet.

## Not done / decisions to revisit

- **Group -> role mapping** (e.g. `infrastructure` -> Sales Manager): Frappe ignores the `groups` claim. Would need a small custom app or a Server Script on User login.
- **Backups**: MariaDB is a single pod on NFS with no backup job. Add a `bench backup` CronJob or mysqldump to your usual target.
- **CRM version**: `image/apps.json` tracks CRM `main`, so a weekly rebuild can bring breaking changes. Pin a tag/branch there once stable.
- Frappe/ERPNext-on-NFS sites volume is shared RWX; fine for this scale.
