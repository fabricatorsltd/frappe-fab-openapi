from __future__ import annotations

from typing import Any, Iterable, Mapping

import requests
from frappe import _
from frappe.exceptions import ValidationError

# the shared client, plus the helpers the SDI backend and its tests still import
# from here
from fab_openapi.clients.base import (
	KNOWN_DEFAULT_OAUTH_TOKEN_URLS,
	OpenAPIClient,
	extract_api_data,
	extract_error_message,
	extract_token_expiry,
	get_default_endpoint_url,
	get_default_oauth_token_url,
	get_document_value,
	normalize_identifier,
	parse_json_response,
)


class SDIClient(OpenAPIClient):
	service_type = "SDI"

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


def ensure_list(value: Any) -> list[Any]:
	if value is None:
		return []
	if isinstance(value, list):
		return value
	return [value]


def normalize_vat_code(value: Any) -> str | None:
	identifier = normalize_identifier(value)
	if not identifier:
		return None
	if len(identifier) > 2 and identifier[:2].isalpha():
		return identifier[2:]
	return identifier


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
