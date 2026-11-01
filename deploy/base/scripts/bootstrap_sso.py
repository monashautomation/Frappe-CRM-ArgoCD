"""Configure Authentik SSO + role mapping on the Frappe site. Idempotent.

Two modes, chosen by whether the oidc_extended app is installed on the site:

* extended (normal): the Social Login Key points its redirect at oidc_extended's callback,
  which creates the user on first login and maps the ID token's `groups` claim to Frappe
  Role Profiles on EVERY login. Role Profiles and the group mapping come from the
  SSO_ROLE_MAPPING JSON in the ConfigMap.
* standard (fallback, e.g. image not rebuilt yet): Frappe's own callback creates the user as
  a Website User; Portal Settings' default role (desk access) promotes them to System User.

Access control (who may log in at all) is enforced in Authentik via application bindings.
"""
import json
import os

import frappe

SITE = os.environ["SITE_NAME"]
PROVIDER = os.environ.get("AUTHENTIK_PROVIDER_NAME", "authentik")
BASE_URL = os.environ["AUTHENTIK_BASE_URL"].rstrip("/")
CLIENT_ID = os.environ["AUTHENTIK_CLIENT_ID"]
CLIENT_SECRET = os.environ["AUTHENTIK_CLIENT_SECRET"]
SCOPES = os.environ.get("AUTHENTIK_SCOPES", "openid email profile groups")
DEFAULT_ROLE = os.environ.get("SSO_DEFAULT_ROLE", "Sales User")
DISABLE_PASSWORD_LOGIN = os.environ.get("DISABLE_PASSWORD_LOGIN", "0") == "1"
MAPPING = json.loads(os.environ.get("SSO_ROLE_MAPPING") or "{}")

STANDARD_CALLBACK = "frappe.integrations.oauth2_logins.custom"
EXTENDED_CALLBACK = "oidc_extended.callback.custom"


def ensure_role(name):
    if not frappe.db.exists("Role", name):
        frappe.get_doc({"doctype": "Role", "role_name": name, "desk_access": 1}).insert(
            ignore_permissions=True
        )


def upsert_role_profile(name, roles):
    for role in roles:
        ensure_role(role)
    if frappe.db.exists("Role Profile", name):
        doc = frappe.get_doc("Role Profile", name)
    else:
        doc = frappe.new_doc("Role Profile")
        doc.role_profile = name
    doc.set("roles", [])
    for role in roles:
        doc.append("roles", {"role": role})
    doc.save(ignore_permissions=True)


def upsert_social_login_key(extended):
    callback = EXTENDED_CALLBACK if extended else STANDARD_CALLBACK
    existing = frappe.db.get_value("Social Login Key", {"provider_name": PROVIDER})
    doc = (
        frappe.get_doc("Social Login Key", existing)
        if existing
        else frappe.new_doc("Social Login Key")
    )
    values = {
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
        "redirect_url": f"/api/method/{callback}/{PROVIDER}",
        "api_endpoint": "/application/o/userinfo/",
        "auth_url_data": json.dumps({"response_type": "code", "scope": SCOPES}),
    }
    if extended:
        # Otherwise the Frappe username becomes Authentik's long hashed `sub`.
        values["user_id_property"] = "preferred_username"
    doc.update(values)
    doc.save(ignore_permissions=True)
    return doc.name


def upsert_oidc_extended_configuration(social_login_key_name):
    for profile, roles in (MAPPING.get("role_profiles") or {}).items():
        upsert_role_profile(profile, roles)

    # autoname is format:{provider}; the callback looks it up by the provider name in the URL.
    if frappe.db.exists("OIDC Extended Configuration", social_login_key_name):
        doc = frappe.get_doc("OIDC Extended Configuration", social_login_key_name)
    else:
        doc = frappe.new_doc("OIDC Extended Configuration")
        doc.provider = social_login_key_name

    doc.groups_claim_name = "groups"
    doc.email_claim_name = "email"
    doc.given_name_claim_name = "given_name"
    doc.family_name_claim_name = "family_name"

    doc.set("group_role_mappings", [])
    for group, profiles in (MAPPING.get("group_role_profiles") or {}).items():
        for profile in profiles:
            doc.append("group_role_mappings", {"group": group, "role_profile": profile})

    doc.set("fallback_role_profiles", [])
    for profile in MAPPING.get("fallback_role_profiles") or []:
        doc.append("fallback_role_profiles", {"role_profile": profile})

    doc.save(ignore_permissions=True)


# Frappe logs to ../logs relative to the cwd, so it must run from sites/ like bench does;
# from the bench root it tries /home/frappe/logs and fails.
os.chdir("/home/frappe/frappe-bench/sites")
frappe.init(site=SITE, sites_path=".")
frappe.connect()
try:
    extended = bool(frappe.db.exists("DocType", "OIDC Extended Configuration"))

    ensure_role(DEFAULT_ROLE)
    frappe.db.set_single_value("Portal Settings", "default_role", DEFAULT_ROLE)

    key_name = upsert_social_login_key(extended)
    if extended:
        upsert_oidc_extended_configuration(key_name)

    frappe.db.set_single_value(
        "System Settings", "disable_user_pass_login", 1 if DISABLE_PASSWORD_LOGIN else 0
    )
    frappe.db.commit()
    # Client ID is not a secret; log a prefix so a stale OpenBao/ExternalSecret value is visible.
    print(
        f"SSO configured: mode={'extended' if extended else 'standard'} provider={PROVIDER} "
        f"client_id={CLIENT_ID[:4]}...{CLIENT_ID[-4:]} (len {len(CLIENT_ID)})"
    )
finally:
    frappe.destroy()
