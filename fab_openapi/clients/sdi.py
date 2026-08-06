from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

import frappe
import requests
from frappe import _
from frappe.exceptions import ValidationError
from frappe.utils import add_to_date, cint, get_datetime, now_datetime
from frappe.utils.password import set_encrypted_password

KNOWN_DEFAULT_OAUTH_TOKEN_URLS = {
	"https://console.openapi.com/apis/oauth/token",
	"https://console.openapi.com/oauth/token",
	"https://oauth.openapi.it/token",
	"https://test.oauth.openapi.it/token",
}

# reuse a minted OAuth token until it nears expiry instead of minting per request;
# re-mint this many seconds early, and assume this lifetime when the provider omits one
TOKEN_EXPIRY_SKEW_SECONDS = 300
TOKEN_FALLBACK_LIFETIME_SECONDS = 7 * 24 * 3600


class SDIClient:
	customer_invoice_import_path = "/customer_invoice_imports"
	invoices_path = "/invoices"
	invoice_download_path = "/invoices_download/{uuid}"
	invoices_signature_path = "/invoices_signature"
	invoices_signature_legal_storage_path = "/invoices_signature_legal_storage"
	invoice_detail_path = "/invoices/{uuid}"
	invoices_notifications_path = "/invoices_notifications"
	invoice_notification_detail_path = "/invoices_notifications/{uuid}"
	business_registry_configuration_path = "/business_registry_configurations"
	api_configuration_path = "/api_configurations"

	def __init__(self, connection: str | Mapping[str, Any] | Any):
		self.connection = get_connection_document(connection)
		self._shared_token: str | None = None

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

	def download_invoice_xml(self, invoice_uuid: str) -> str:
		"""Fetch the raw FatturaPA XML for an invoice.

		The list endpoint returns a JSON representation; the downstream
		purchase invoice parser needs the actual XML, which this endpoint
		serves only when the Accept header asks for it.
		"""
		url = self.build_url(self.invoice_download_path.format(uuid=invoice_uuid))
		for attempt in range(2):
			headers = {
				"Accept": "application/xml",
				"Authorization": self.get_authorization_header(
					(self.invoice_download_path,), method="GET", force_refresh=attempt > 0
				),
			}
			try:
				response = requests.get(url, headers=headers, timeout=self.get_timeout_seconds())
			except requests.RequestException as exc:
				raise ValidationError(_("OpenAPI invoice download failed: {0}").format(exc)) from exc
			if self._should_retry_auth(response.status_code, attempt):
				self.invalidate_access_token()
				continue
			break
		if response.status_code >= 400:
			raise ValidationError(
				_("OpenAPI download {0} failed with status {1}: {2}").format(
					url, response.status_code, response.text[:200]
				)
			)
		return response.text

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
		for attempt in range(2):
			headers = {
				"Accept": "application/json",
				"Authorization": self.get_authorization_header(
					scope_paths or (path,), method=method, force_refresh=attempt > 0
				),
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

			if self._should_retry_auth(response.status_code, attempt):
				self.invalidate_access_token()
				continue
			break

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

	def get_authorization_header(
		self, scope_paths: Iterable[str], method: str = "GET", force_refresh: bool = False
	) -> str:
		auth_mode = (get_document_value(self.connection, "auth_mode") or "OAuth Client Credentials").strip()
		if auth_mode == "Bearer Token":
			access_token = get_optional_document_secret(self.connection, "access_token")
			if not access_token:
				raise ValidationError(
					_("OpenAPI Access Token is missing on connection {0}.").format(
						get_document_value(self.connection, "connection_name") or "unknown"
					)
				)
			return f"Bearer {access_token}"

		# OAuth client-credentials: one token minted for the union of every operation
		# scope, reused until it nears expiry, so we do not mint a fresh token per call
		return f"Bearer {self.get_client_credentials_token(force_refresh=force_refresh)}"

	def uses_client_credentials(self) -> bool:
		auth_mode = (get_document_value(self.connection, "auth_mode") or "OAuth Client Credentials").strip()
		return auth_mode != "Bearer Token"

	def get_client_credentials_token(self, force_refresh: bool = False) -> str:
		if not force_refresh:
			if self._shared_token:
				return self._shared_token
			stored = get_optional_document_secret(self.connection, "access_token")
			if stored and not self.stored_token_expired():
				self._shared_token = stored
				return stored
		self._shared_token = self.mint_access_token()
		return self._shared_token

	def stored_token_expired(self) -> bool:
		expiry = get_document_value(self.connection, "access_token_expiry")
		if not expiry:
			return True
		return get_datetime(expiry) <= now_datetime()

	def mint_access_token(self) -> str:
		token, payload = self.fetch_access_token(self.full_scope_value())
		self.store_access_token(token, self.resolve_token_expiry(payload, token))
		return token

	def request_access_token(self, scope_value: str) -> str:
		token, _payload = self.fetch_access_token(scope_value)
		return token

	def fetch_access_token(self, scope_value: str) -> tuple[str, dict[str, Any]]:
		account_email = normalize_identifier(get_document_value(self.connection, "account_email"))
		api_key = get_optional_document_secret(self.connection, "api_key")
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
		return access_token, payload if isinstance(payload, dict) else {}

	def full_scope_value(self) -> str:
		host = urlparse(self.get_status_url()).netloc
		scopes = {
			f"{method.upper()}:{host}{normalize_scope_path(path)}"
			for method, path in self.token_scope_requests()
		}
		return " ".join(sorted(scopes))

	def token_scope_requests(self) -> tuple[tuple[str, str], ...]:
		return (
			("POST", self.invoices_signature_path),
			("POST", self.invoices_signature_legal_storage_path),
			("POST", self.invoices_path),
			("GET", self.invoices_path),
			("GET", self.invoices_notifications_path),
			("GET", self.invoice_download_path),
			("GET", self.business_registry_configuration_path),
			("POST", self.business_registry_configuration_path),
			("GET", self.api_configuration_path),
			("POST", self.api_configuration_path),
			("POST", self.customer_invoice_import_path),
		)

	def store_access_token(self, token: str, expiry: Any) -> None:
		name = get_document_value(self.connection, "name")
		if not name or isinstance(self.connection, Mapping):
			return
		set_encrypted_password("OpenAPI Connection", name, token, "access_token")
		frappe.db.set_value(
			"OpenAPI Connection", name, "access_token_expiry", expiry, update_modified=False
		)
		if hasattr(self.connection, "access_token_expiry"):
			self.connection.access_token_expiry = expiry

	def invalidate_access_token(self) -> None:
		self._shared_token = None
		name = get_document_value(self.connection, "name")
		if name and not isinstance(self.connection, Mapping):
			frappe.db.set_value(
				"OpenAPI Connection", name, "access_token_expiry", None, update_modified=False
			)

	def resolve_token_expiry(self, payload: Mapping[str, Any], token: str) -> Any:
		expiry = extract_token_expiry(payload, token)
		if expiry:
			return add_to_date(expiry, seconds=-TOKEN_EXPIRY_SKEW_SECONDS)
		return add_to_date(now_datetime(), seconds=TOKEN_FALLBACK_LIFETIME_SECONDS)

	def _should_retry_auth(self, status_code: int, attempt: int) -> bool:
		return status_code in (401, 403) and attempt == 0 and self.uses_client_credentials()

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


def get_optional_document_secret(document: Mapping[str, Any] | Any, fieldname: str) -> str | None:
	"""Like get_document_secret but returns None instead of raising when the
	encrypted field has never been set (the normal state before the first mint)."""
	if isinstance(document, Mapping):
		return normalize_identifier(document.get(fieldname))
	get_password = getattr(document, "get_password", None)
	if callable(get_password):
		try:
			return normalize_identifier(get_password(fieldname, raise_exception=False))
		except TypeError:
			# test doubles whose get_password takes only the fieldname
			try:
				return normalize_identifier(get_password(fieldname))
			except Exception:
				return None
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


def extract_token_expiry(payload: Mapping[str, Any], token: str) -> Any:
	"""Best-effort expiry for a minted token: the provider payload first, then the
	token's own JWT `exp`. None when neither is available (caller falls back)."""
	if isinstance(payload, Mapping):
		for key in ("expire", "expiration", "expires_at", "expires", "exp"):
			expiry = coerce_epoch_or_datetime(payload.get(key))
			if expiry:
				return expiry
	return jwt_expiry(token)


def coerce_epoch_or_datetime(value: Any) -> Any:
	if not value:
		return None
	if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
		try:
			return get_datetime(datetime.fromtimestamp(int(value)))
		except (ValueError, OverflowError, OSError):
			return None
	try:
		return get_datetime(value)
	except (ValueError, TypeError):
		return None


def jwt_expiry(token: str) -> Any:
	parts = token.split(".") if isinstance(token, str) else []
	if len(parts) != 3:
		return None
	try:
		segment = parts[1] + "=" * (-len(parts[1]) % 4)
		claims = json.loads(base64.urlsafe_b64decode(segment.encode("ascii")))
	except (ValueError, TypeError):
		return None
	return coerce_epoch_or_datetime(claims.get("exp")) if isinstance(claims, dict) else None


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
