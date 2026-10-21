#!/bin/bash
# Idempotent: safe to run on every ArgoCD sync.
set -euo pipefail
cd /home/frappe/frappe-bench

ls -1 apps > sites/apps.txt
[[ -f sites/common_site_config.json ]] || echo '{}' > sites/common_site_config.json

# Assets are baked into the image outside the sites volume; the normal entrypoint is
# bypassed here, so link them the same way it does.
rm -rf sites/assets
ln -s /home/frappe/frappe-bench/assets sites/assets

echo "Waiting for MariaDB..."
wait-for-it -t 300 "${DB_HOST}:3306"

bench set-config -gp db_port 3306
bench set-config -g db_host "${DB_HOST}"
bench set-config -g redis_cache "${REDIS_CACHE}"
bench set-config -g redis_queue "${REDIS_QUEUE}"
bench set-config -g redis_socketio "${REDIS_QUEUE}"
bench set-config -gp socketio_port 9000

if [[ -f "sites/${SITE_NAME}/site_config.json" ]]; then
  echo "Site exists, migrating..."
  bench --site "${SITE_NAME}" migrate
else
  echo "Creating site ${SITE_NAME} with CRM..."
  bench new-site "${SITE_NAME}" \
    --no-mariadb-socket \
    --db-type=mariadb \
    --db-host="${DB_HOST}" \
    --db-port=3306 \
    --mariadb-root-username=root \
    --mariadb-root-password="${DB_ROOT_PASSWORD}" \
    --admin-password="${ADMIN_PASSWORD}" \
    --install-app=crm
  rm -f sites/currentsite.txt
fi

# Frappe's nginx forwards X-Forwarded-Proto as plain http, so without host_name the
# OAuth redirect_uri sent to Authentik would be http:// and fail the strict match.
bench --site "${SITE_NAME}" set-config host_name "https://${SITE_NAME}"

echo "Configuring Authentik SSO..."
/home/frappe/frappe-bench/env/bin/python /scripts/bootstrap_sso.py

echo "Bootstrap complete."
