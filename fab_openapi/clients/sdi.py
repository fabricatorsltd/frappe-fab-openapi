from __future__ import annotations

import json
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

import frappe
import requests
from frappe import _
from frappe.exceptions import ValidationError
from frappe.utils import cint

KNOWN_DEFAULT_OAUTH_TOKEN_URLS = {
	"https://console.openapi.com/apis/oauth/token",
	"https://console.openapi.com/oauth/token",
	"https://oauth.openapi.it/token",
	"https://test.oauth.openapi.it/token",
}


class SDIClient:
	customer_invoice_import_path = "/customer_invoice_imports"
	invoices_path = "/invoices"
	invoices_signature_path = "/invoices_signature"
	invoices_signature_legal_storage_path = "/invoices_signature_legal_storage"
	invoice_detail_path = "/invoices/{uuid}"
	invoices_notifications_path = "/invoices_notifications"
	invoice_notification_detail_path = "/invoices_notifications/{uuid}"
	business_registry_configuration_path = "/business_registry_configurations"
	api_configuration_path = "/api_configurations"

	def __init__(self, connection: str | Mapping[str, Any] | Any):
		self.connection = get_connection_document(connection)
		self._token_cache: dict[str, str] = {}

	@classmethod
	def from_connection_name(cls, connection_name: str) -> "SDIClient":
		return cls(connection_name)

	def get_endpoint_url(self) -> str:
		configured_url = normalize_url(get_document_value(self.connection, "endpoint_url"))
		return configured_url or get_default_endpoint_url(get_document_value(self.connection, "environment"))

	def get_status_url(self) -> str:
		configured_url = normalize_url(get_document_value(self.connection, "status_url"))
		return configured_url or self.get_endpoint_url()

	def get_oauth_token_url(self) -> str:
		configured_url = normalize_url(get_document_value(self.connection, "oauth_token_url"))
		default_url = get_default_oauth_token_url(get_document_value(self.connection, "environment"))
		if not configured_url or configured_url in KNOWN_DEFAULT_OAUTH_TOKEN_URLS:
			return default_url
		return configured_url

	def get_timeout_seconds(self) -> int:
		timeout_seconds = cint(get_document_value(self.connection, "timeout_seconds") or 30)
		return timeout_seconds or 30

	def get_submit_invoice_path(self) -> str:
		apply_signature = coerce_bool(get_document_value(self.connection, "default_apply_signature"), default=True)
		apply_legal_storage = coerce_bool(
			get_document_value(self.connection, "default_apply_legal_storage"), default=False
		)
		if apply_signature and apply_legal_storage:
			return self.invoices_signature_legal_storage_path
		if apply_signature:
			return self.invoices_signature_path
		return self.invoices_path

	def submit_invoice_xml(self, xml_content: str) -> dict[str, Any]:
		submit_path = self.get_submit_invoice_path()
		payload = self.request_json(
			method="POST",
			path=submit_path,
			data=xml_content,
			content_type="application/xml",
		)
		data = extract_api_data(payload)
		if not isinstance(data, Mapping) or not data.get("uuid"):
			raise ValidationError(_("OpenAPI did not return an outbound invoice UUID."))
		return dict(data)

	def list_invoices(self, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
		return [
			dict(item)
			for item in ensure_list(extract_api_data(self.request_json("GET", self.invoices_path, params=params)))
			if isinstance(item, Mapping)
		]

	def get_invoice(self, invoice_uuid: str) -> dict[str, Any]:
		payload = self.request_json(
			"GET",
			self.invoice_detail_path.format(uuid=invoice_uuid),
			scope_paths=(self.invoices_path,),
		)
		items = ensure_list(extract_api_data(payload))
		if not items:
			raise ValidationError(_("OpenAPI invoice {0} was not returned by the provider.").format(invoice_uuid))
		return dict(items[0])

	def get_notification(self, notification_uuid: str) -> dict[str, Any]:
		payload = self.request_json(
			"GET",
			self.invoice_notification_detail_path.format(uuid=notification_uuid),
			scope_paths=(self.invoices_notifications_path,),
		)
		items = ensure_list(extract_api_data(payload))
		if not items:
			raise ValidationError(
				_("OpenAPI notification {0} was not returned by the provider.").format(notification_uuid)
			)
		return dict(items[0])

	def list_business_registry_configurations(self) -> list[dict[str, Any]]:
		return [
			dict(item)
			for item in ensure_list(
				extract_api_data(self.request_json("GET", self.business_registry_configuration_path))
			)
			if isinstance(item, Mapping)
		]

	def find_business_registry_configuration(self, fiscal_id: str) -> dict[str, Any] | None:
		for row in self.list_business_registry_configurations():
			if openapi_fiscal_ids_match_exactly(row.get("fiscal_id"), fiscal_id):
				return row
		return None

	def create_business_registry_configuration(
		self,
		*,
		fiscal_id: str,
		name: str,
		email: str,
		apply_signature: bool | None = None,
		apply_legal_storage: bool | None = None,
	) -> dict[str, Any]:
		payload = self.build_business_registry_configuration_payload(
			fiscal_id=fiscal_id,
			name=name,
			email=email,
			apply_signature=apply_signature,
			apply_legal_storage=apply_legal_storage,
		)
		response = self.request_json("POST", self.business_registry_configuration_path, json_data=payload)
		data = extract_api_data(response)
		return dict(data) if isinstance(data, Mapping) else {}

	def build_business_registry_configuration_payload(
		self,
		*,
		fiscal_id: str,
		name: str,
		email: str,
		apply_signature: bool | None = None,
		apply_legal_storage: bool | None = None,
	) -> dict[str, Any]:
		return {
			"fiscal_id": fiscal_id,
			"name": name,
			"email": email,
			"apply_signature": coerce_bool(
				apply_signature,
				default=coerce_bool(get_document_value(self.connection, "default_apply_signature"), default=True),
			),
			"apply_legal_storage": coerce_bool(
				apply_legal_storage,
				default=coerce_bool(
					get_document_value(self.connection, "default_apply_legal_storage"), default=False
				),
			),
		}

	def list_api_configurations(self) -> list[dict[str, Any]]:
		return [
			dict(item)
			for item in ensure_list(extract_api_data(self.request_json("GET", self.api_configuration_path)))
			if isinstance(item, Mapping)
		]

	def list_api_configurations_for_fiscal_id(self, fiscal_id: str) -> list[dict[str, Any]]:
		return [
			row
			for row in self.list_api_configurations()
			if openapi_fiscal_ids_match_exactly(row.get("fiscal_id") or row.get("id"), fiscal_id)
		]

	def find_api_configuration(self, fiscal_id: str) -> dict[str, Any] | None:
		rows = self.list_api_configurations_for_fiscal_id(fiscal_id)
		return rows[0] if rows else None

	def create_api_configuration(
		self,
		*,
		fiscal_id: str,
		callbacks: list[dict[str, Any]],
	) -> dict[str, Any]:
		payload = {"fiscal_id": fiscal_id, "callbacks": callbacks}
		response = self.request_json("POST", self.api_configuration_path, json_data=payload)
		data = extract_api_data(response)
		return dict(data) if isinstance(data, Mapping) else {}

	def api_configuration_has_required_callbacks(
		self, fiscal_id: str, callbacks: list[dict[str, Any]]
	) -> bool:
		required_callbacks = {
			self._normalize_callback_signature(callback) for callback in callbacks if isinstance(callback, Mapping)
		}
		configured_callbacks = {
			self._normalize_callback_signature(row.get("callback") or {})
			for row in self.list_api_configurations_for_fiscal_id(fiscal_id)
			if isinstance(row.get("callback"), Mapping)
		}
		return required_callbacks.issubset(configured_callbacks)

	def _normalize_callback_signature(
		self, callback: Mapping[str, Any]
	) -> tuple[str | None, str | None, str | None, str | None]:
		return (
			normalize_identifier(callback.get("event")),
			normalize_identifier(callback.get("url")),
			normalize_identifier(callback.get("field")),
			normalize_identifier(callback.get("auth_header")),
		)

	def request_json(
		self,
		method: str,
		path: str,
		scope_paths: Iterable[str] | None = None,
		params: Mapping[str, Any] | None = None,
		data: Any = None,
		json_data: Mapping[str, Any] | None = None,
		content_type: str | None = None,
	) -> dict[str, Any]:
		headers = {
			"Accept": "application/json",
			"Authorization": self.get_authorization_header(scope_paths or (path,), method=method),
		}
		if content_type:
			headers["Content-Type"] = content_type

		try:
			response = requests.request(
				method=method,
				url=self.build_url(path),
				params=params,
				data=data,
				json=json_data,
				headers=headers,
				timeout=self.get_timeout_seconds(),
			)
		except requests.RequestException as exc:
			raise ValidationError(_("OpenAPI request failed: {0}").format(exc)) from exc

		payload = parse_json_response(response)
		if response.status_code >= 400:
			raise ValidationError(
				_("OpenAPI request to {0} failed with status {1}: {2}").format(
					self.build_url(path),
					response.status_code,
					extract_error_message(payload, response.text),
				)
			)

		if not isinstance(payload, dict):
			raise ValidationError(
				_("OpenAPI returned a non-object JSON response for {0}.").format(self.build_url(path))
			)

		return payload

	def get_authorization_header(self, scope_paths: Iterable[str], method: str = "GET") -> str:
		auth_mode = (get_document_value(self.connection, "auth_mode") or "OAuth Client Credentials").strip()
		if auth_mode == "Bearer Token":
			access_token = get_document_secret(self.connection, "access_token")
			if not access_token:
				raise ValidationError(
					_("OpenAPI Access Token is missing on connection {0}.").format(
						get_document_value(self.connection, "connection_name") or "unknown"
					)
				)
			return f"Bearer {access_token}"

		scope_value = self.build_scope_value(scope_paths, method=method)
		if scope_value not in self._token_cache:
			self._token_cache[scope_value] = self.request_access_token(scope_value)
		return f"Bearer {self._token_cache[scope_value]}"

	def request_access_token(self, scope_value: str) -> str:
		account_email = normalize_identifier(get_document_value(self.connection, "account_email"))
		api_key = get_document_secret(self.connection, "api_key")
		if not account_email or not api_key:
			raise ValidationError(
				_("OpenAPI connection {0} requires Account Email and API Key before OAuth can run.").format(
					get_document_value(self.connection, "connection_name") or "unknown"
				)
			)

		try:
			response = requests.post(
				self.get_oauth_token_url(),
				auth=(account_email, api_key),
				json={"scopes": scope_value.split()},
				headers={"Accept": "application/json"},
				timeout=self.get_timeout_seconds(),
			)
		except requests.RequestException as exc:
			raise ValidationError(_("OpenAPI token request failed: {0}").format(exc)) from exc

		payload = parse_json_response(response)
		if response.status_code >= 400:
			raise ValidationError(
				_("OpenAPI token request failed with status {0}: {1}").format(
					response.status_code,
					extract_error_message(payload, response.text),
				)
			)

		access_token = payload.get("token") if isinstance(payload, dict) else None
		if not access_token:
			raise ValidationError(_("OpenAPI token response did not include a token."))
		return access_token

	def build_scope_value(self, scope_paths: Iterable[str], method: str = "GET") -> str:
		host = urlparse(self.get_status_url()).netloc
		normalized_paths = [normalize_scope_path(path) for path in scope_paths] or [self.invoices_path]
		http_method = method.upper()
		scopes = sorted({f"{http_method}:{host}{path}" for path in normalized_paths})
		return " ".join(scopes)

	def build_url(self, path: str) -> str:
		base_url = self.get_status_url()
		normalized_path = path if path.startswith("/") else f"/{path}"
		return f"{base_url}{normalized_path}"


def get_connection_document(connection: str | Mapping[str, Any] | Any):
	if isinstance(connection, str):
		return frappe.get_doc("OpenAPI Connection", connection)
	return connection


def get_document_value(document: Mapping[str, Any] | Any, fieldname: str) -> Any:
	if isinstance(document, Mapping):
		return document.get(fieldname)
	return getattr(document, fieldname, None)


def get_document_secret(document: Mapping[str, Any] | Any, fieldname: str) -> str | None:
	if isinstance(document, Mapping):
		return normalize_identifier(document.get(fieldname))
	get_password = getattr(document, "get_password", None)
	if callable(get_password):
		return normalize_identifier(get_password(fieldname))
	return normalize_identifier(getattr(document, fieldname, None))


def get_default_endpoint_url(environment: str | None = None) -> str:
	return {
		"Production": "https://sdi.openapi.it",
		"Sandbox": "https://test.sdi.openapi.it",
	}.get(environment or "Production", "https://sdi.openapi.it")


def get_default_oauth_token_url(environment: str | None = None) -> str:
	return {
		"Production": "https://oauth.openapi.it/token",
		"Sandbox": "https://test.oauth.openapi.it/token",
	}.get(environment or "Production", "https://oauth.openapi.it/token")


def ensure_list(value: Any) -> list[Any]:
	if value is None:
		return []
	if isinstance(value, list):
		return value
	return [value]


def extract_api_data(payload: Mapping[str, Any] | Any) -> Any:
	if isinstance(payload, Mapping) and "data" in payload:
		return payload["data"]
	return payload


def extract_error_message(payload: Any, fallback: str | None = None) -> str:
	if isinstance(payload, Mapping):
		for key in ("message", "detail", "error"):
			value = payload.get(key)
			if value:
				if isinstance(value, (dict, list)):
					return json.dumps(value, ensure_ascii=False)[:500]
				return str(value)[:500]
	return (fallback or "Unknown provider error")[:500]


def parse_json_response(response) -> Any:
	try:
		return response.json()
	except ValueError as exc:
		raise ValidationError(
			_("OpenAPI returned an invalid JSON response with status {0}.").format(response.status_code)
		) from exc


def normalize_identifier(value: Any) -> str | None:
	if value is None:
		return None
	text = str(value).strip()
	return text or None


def normalize_url(value: Any) -> str | None:
	identifier = normalize_identifier(value)
	if not identifier:
		return None
	return identifier.rstrip("/")


def normalize_vat_code(value: Any) -> str | None:
	identifier = normalize_identifier(value)
	if not identifier:
		return None
	if len(identifier) > 2 and identifier[:2].isalpha():
		return identifier[2:]
	return identifier


def normalize_scope_path(path: str) -> str:
	base_path = path.split("/{", 1)[0]
	return base_path.rstrip("/") or "/"


def openapi_fiscal_ids_match_exactly(candidate: Any, expected: str | None) -> bool:
	candidate_value = normalize_identifier(candidate)
	expected_value = normalize_identifier(expected)
	return bool(candidate_value and expected_value and candidate_value == expected_value)


def matches_openapi_fiscal_id(candidate: Any, expected: str | None) -> bool:
	candidate_value = normalize_identifier(candidate)
	expected_value = normalize_identifier(expected)
	if not candidate_value or not expected_value:
		return False
	if candidate_value == expected_value:
		return True
	if expected_value.isdigit() and normalize_vat_code(candidate_value) == expected_value:
		return True
	if candidate_value.isdigit() and normalize_vat_code(expected_value) == candidate_value:
		return True
	return False


def coerce_bool(value: Any, default: bool = False) -> bool:
	if value is None:
		return default
	if isinstance(value, bool):
		return value
	if isinstance(value, (int, float)):
		return bool(value)
	text = str(value).strip().lower()
	if text in {"1", "true", "yes", "y", "on"}:
		return True
	if text in {"0", "false", "no", "n", "off"}:
		return False
	return default
