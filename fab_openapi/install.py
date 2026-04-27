from __future__ import annotations

import frappe


def after_install():
	if frappe.db.exists("DocType", "OpenAPI Connection"):
		ensure_seed_records()


def after_migrate():
	if frappe.db.exists("DocType", "OpenAPI Connection"):
		ensure_seed_records()


def ensure_seed_records():
	ensure_seed_documents("OpenAPI Connection", "connection_name", get_default_connections())


def ensure_seed_documents(doctype: str, lookup_field: str, documents: list[dict[str, object]]):
	if not frappe.db.exists("DocType", doctype):
		return

	for payload in documents:
		docname = frappe.db.get_value(doctype, {lookup_field: payload[lookup_field]})
		if not docname:
			frappe.get_doc({"doctype": doctype, **payload}).insert(ignore_permissions=True)
			continue

		doc = frappe.get_doc(doctype, docname)
		changed = False
		for fieldname, value in payload.items():
			if fieldname in {"doctype", "name"}:
				continue
			if doc.get(fieldname) in (None, ""):
				doc.set(fieldname, value)
				changed = True

		if changed:
			doc.save(ignore_permissions=True)


def get_default_connections() -> list[dict[str, object]]:
	return [
		{
			"connection_name": "SDI Production",
			"service_type": "SDI",
			"environment": "Production",
			"enabled": 1,
			"endpoint_url": "https://sdi.openapi.it",
			"status_url": "https://sdi.openapi.it",
			"oauth_token_url": "https://oauth.openapi.it/token",
			"auth_mode": "OAuth Client Credentials",
			"timeout_seconds": 30,
			"default_apply_signature": 1,
			"default_apply_legal_storage": 0,
			"callback_field": "data",
		},
		{
			"connection_name": "SDI Sandbox",
			"service_type": "SDI",
			"environment": "Sandbox",
			"enabled": 1,
			"endpoint_url": "https://test.sdi.openapi.it",
			"status_url": "https://test.sdi.openapi.it",
			"oauth_token_url": "https://test.oauth.openapi.it/token",
			"auth_mode": "OAuth Client Credentials",
			"timeout_seconds": 30,
			"default_apply_signature": 1,
			"default_apply_legal_storage": 0,
			"callback_field": "data",
		},
	]
