"""Configure Authentik SSO + auto-provisioning on the Frappe site. Idempotent.

Provisioning model: a "Social Login Key" with Sign ups = Allow makes Frappe create a User
the first time someone authenticates through Authentik. Frappe creates them as a Website
User with no roles, which cannot open /crm, so Portal Settings' default role is set to a
desk-access role (Frappe then promotes the new user to System User).

Access control is therefore enforced in Authentik (application policy bindings): anyone
who can pass Authentik's authorization for this application gets a CRM account.
"""
import json
import os

import frappe

SITE = os.environ["SITE_NAME"]
PROVIDER = os.environ.get("AUTHENTIK_PROVIDER_NAME", "authentik")
BASE_URL = os.environ["AUTHENTIK_BASE_URL"].rstrip("/")
CLIENT_ID = os.environ["AUTHENTIK_CLIENT_ID"]
CLIENT_SECRET = os.environ["AUTHENTIK_CLIENT_SECRET"]
DEFAULT_ROLE = os.environ.get("SSO_DEFAULT_ROLE", "Sales User")
DISABLE_PASSWORD_LOGIN = os.environ.get("DISABLE_PASSWORD_LOGIN", "0") == "1"


def ensure_role(name):
    if not frappe.db.exists("Role", name):
        frappe.get_doc({"doctype": "Role", "role_name": name, "desk_access": 1}).insert(
            ignore_permissions=True
        )


def upsert_social_login_key():
    existing = frappe.db.get_value("Social Login Key", {"provider_name": PROVIDER})
    doc = (
        frappe.get_doc("Social Login Key", existing)
        if existing
        else frappe.new_doc("Social Login Key")
    )
    doc.update(
        {
            "enable_social_login": 1,
            "social_login_provider": "Custom",
            "provider_name": PROVIDER,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "sign_ups": "Allow",
            "custom_base_url": 1,
            "base_url": BASE_URL,
            "authorize_url": "/application/o/authorize/",
            "access_token_url": "/application/o/token/",
            "redirect_url": f"/api/method/frappe.integrations.oauth2_logins.custom/{PROVIDER}",
            "api_endpoint": "/application/o/userinfo/",
            "auth_url_data": json.dumps(
                {"response_type": "code", "scope": "openid email profile"}
            ),
        }
    )
    doc.save(ignore_permissions=True)


# Frappe logs to ../logs relative to the cwd, so it must run from sites/ like bench does;
# from the bench root it tries /home/frappe/logs and fails.
os.chdir("/home/frappe/frappe-bench/sites")
frappe.init(site=SITE, sites_path=".")
frappe.connect()
try:
    ensure_role(DEFAULT_ROLE)
    frappe.db.set_single_value("Portal Settings", "default_role", DEFAULT_ROLE)
    upsert_social_login_key()
    frappe.db.set_single_value(
        "System Settings", "disable_user_pass_login", 1 if DISABLE_PASSWORD_LOGIN else 0
    )
    frappe.db.commit()
    print(f"SSO configured: provider={PROVIDER} default_role={DEFAULT_ROLE}")
finally:
    frappe.destroy()
