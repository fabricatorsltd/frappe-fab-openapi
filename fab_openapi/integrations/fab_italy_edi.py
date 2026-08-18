from __future__ import annotations

import json
from typing import Any, Mapping

import frappe
from frappe import _
from frappe.exceptions import ValidationError
from frappe.utils import cint

from fab_openapi.clients.sdi import (
	KNOWN_DEFAULT_OAUTH_TOKEN_URLS,
	SDIClient,
	coerce_bool,
	get_default_endpoint_url,
	get_default_oauth_token_url,
	matches_openapi_fiscal_id,
	normalize_identifier,
	normalize_vat_code,
)


class OpenAPITransportBackend:
	adapter_key = "openapi"
	documentation_url = "https://console.openapi.com/it/apis/sdi/documentation"
	openapi_spec_url = "https://console.openapi.com/oas/it/sdi.openapi.json"
	default_callback_path = "/api/method/fab_italy_edi.api.receive_openapi_callback"
	terminal_transmission_states = {"accepted", "rejected", "failed", "cancelled"}

	def validate_configuration(self, configuration: Mapping[str, Any]) -> list[str]:
		auth_mode = (configuration.get("auth_mode") or "Basic").strip()
		required_fields = ("password",) if auth_mode == "Bearer Token" else ("username", "api_key")
		missing_fields = [fieldname for fieldname in required_fields if not configuration.get(fieldname)]
		if not configuration.get("use_environment_default_endpoint") and not configuration.get("endpoint_url"):
			missing_fields.append("endpoint_url")
		return missing_fields

	def normalize_provider_document(self, provider) -> None:
		provider.environment = provider.environment or "Production"
		if provider.auth_mode in (None, "", "API Key"):
			provider.auth_mode = "Basic"
		elif provider.auth_mode == "Bearer Token" and not provider.password:
			provider.auth_mode = "Basic"

		if provider.username:
			provider.username = provider.username.strip().lower()

		settings = self.get_additional_settings(provider)
		connection_name = normalize_identifier(settings.get("connection_name")) or self.get_default_connection_name(
			provider.environment
		)
		settings["connection_name"] = connection_name
		settings.setdefault("documentation_url", self.documentation_url)
		settings.setdefault("openapi_spec_url", self.openapi_spec_url)
		oauth_token_url = normalize_identifier(settings.get("oauth_token_url"))
		if not oauth_token_url or oauth_token_url in KNOWN_DEFAULT_OAUTH_TOKEN_URLS:
			settings["oauth_token_url"] = get_default_oauth_token_url(provider.environment)
		settings.setdefault("account_auth_mode", "Basic")
		settings.setdefault("transport_auth_mode", "Bearer Token")
		settings.setdefault("customer_invoice_import_path", SDIClient.customer_invoice_import_path)
		settings.setdefault("invoices_path", SDIClient.invoices_path)
		settings.setdefault("invoices_notifications_path", SDIClient.invoices_notifications_path)
		settings.setdefault(
			"business_registry_configuration_path",
			SDIClient.business_registry_configuration_path,
		)
		settings.setdefault("api_configuration_path", SDIClient.api_configuration_path)

		if provider.use_environment_default_endpoint:
			provider.endpoint_url = self.get_connection_value(
				connection_name,
				"endpoint_url",
			) or get_default_endpoint_url(provider.environment)
			provider.status_url = self.get_connection_value(
				connection_name,
				"status_url",
			) or provider.endpoint_url

		provider.additional_settings = settings

	def ensure_outbound_submission_ready(self, configuration, provider) -> dict[str, bool]:
		client = self.get_client(provider)
		fiscal_id = self.get_openapi_fiscal_id(configuration)
		changes = {
			"created_business_registry_configuration": False,
			"created_api_configuration": False,
		}

		if not client.find_business_registry_configuration(fiscal_id):
			self.create_business_registry_configuration(client, provider, configuration, fiscal_id)
			changes["created_business_registry_configuration"] = True

		callbacks = self.build_api_configuration_callbacks(client, provider)
		if not client.find_api_configuration(fiscal_id) or not client.api_configuration_has_required_callbacks(
			fiscal_id, callbacks
		):
			client.create_api_configuration(fiscal_id=fiscal_id, callbacks=callbacks)
			changes["created_api_configuration"] = True

		return changes

	def submit_outbound_invoice(
		self,
		provider,
		xml_content: str,
		*,
		configuration=None,
		document=None,
	) -> dict[str, Any]:
		return self.get_client(provider).submit_invoice_xml(xml_content)

	def get_outbound_invoice(self, provider, external_submission_id: str) -> dict[str, Any]:
		return self.get_client(provider).get_invoice(external_submission_id)

	def iter_invoice_notifications(self, provider, invoice: Mapping[str, Any]) -> list[dict[str, Any]]:
		notifications = []
		for reference in self.ensure_list(invoice.get("notifications")):
			notification_uuid = self.extract_notification_uuid(reference)
			if notification_uuid:
				notifications.append(self.get_client(provider).get_notification(notification_uuid))
				continue
			if isinstance(reference, Mapping) and self.is_complete_notification(reference):
				notifications.append(dict(reference))
		return notifications

	# safety cap on paged listing, so a misread pagination contract can never loop
	INCOMING_MAX_PAGES = 200

	def list_incoming_invoices(self, configuration, provider) -> list[dict[str, Any]]:
		client = self.get_client(provider)
		company = self.get_document_value(configuration, "company")
		provider_name = self.get_document_value(provider, "name")
		recipient = ",".join(self.get_recipient_values(configuration))
		# Do not filter on OpenAPI's `downloaded` flag: it flips at download time,
		# not at import, so a download that later fails to import would be lost.
		# We decide what to fetch from our own ledger (EDI Document) instead, so a
		# failed import is retried next run rather than silently dropped. Listing is
		# free (GET /invoices); we page through it defensively: dedup by uuid and
		# stop when a page brings nothing new, which is correct whether the API
		# paginates or ignores the page param (then page 2 repeats page 1 -> stop).
		invoices: list[dict[str, Any]] = []
		seen: set[str] = set()
		for page in range(1, self.INCOMING_MAX_PAGES + 1):
			rows = client.list_invoices(params={"type": "1", "recipient": recipient, "page": page})
			fresh = []
			for item in rows:
				uuid = normalize_identifier(item.get("uuid"))
				if not uuid or uuid in seen:
					continue  # uuid-less items cannot be fetched/deduped: skip them
				seen.add(uuid)
				fresh.append((uuid, item))
			# stop when a page brings no new uuid. Correct for both API contracts as
			# long as page ordering is stable/contiguous (real SDI listings are):
			# paginating -> empty tail page; ignoring `page` -> page 2 repeats page 1
			if not fresh:
				break
			already = self.existing_edi_document_uuids(
				company, provider_name, [pair[0] for pair in fresh]
			)
			for uuid, item in fresh:
				if uuid in already:
					continue
				normalized = self.normalize_supplier_invoice(item)
				# the list payload is a JSON view; the purchase invoice parser needs
				# the real FatturaPA XML, fetched per document (a paid call)
				try:
					normalized["payload"] = client.download_invoice_xml(uuid)
				except Exception as exc:
					# keep the failure visible and skip this one; with no EDI
					# Document created it is retried next run, not lost
					frappe.log_error(
						title="OpenAPI incoming invoice download failed",
						message=f"uuid={uuid}: {exc}",
					)
					continue
				invoices.append(normalized)
		return invoices

	def existing_edi_document_uuids(self, company, provider_name, uuids: list[str]) -> set[str]:
		"""Our own 'do we have it?' ledger, batched: the subset of uuids that already
		have an EDI Document for this company/provider (same key as the upsert dedup),
		so those paid downloads are skipped."""
		if not uuids:
			return set()
		return set(
			frappe.get_all(
				"EDI Document",
				filters={
					"external_submission_id": ["in", uuids],
					"document_kind": "supplier_invoice_import",
					"company": company,
					"provider": provider_name,
				},
				pluck="external_submission_id",
			)
		)

	def normalize_outbound_invoice(self, invoice: Mapping[str, Any]) -> dict[str, Any]:
		receipt_state = self.normalize_invoice_marking(invoice.get("marking"), invoice.get("notice"))
		invoice_uuid = normalize_identifier(invoice.get("uuid"))
		return {
			"external_submission_id": invoice_uuid,
			"canonical_identifier": invoice.get("sdi_file_name") or invoice_uuid,
			"transmission_state": receipt_state,
			"receipt_state": receipt_state,
			"processing_notes": self.build_invoice_processing_notes(invoice),
			"receipt_type": "customer_invoice_status",
			"receipt_message_id": self.build_invoice_status_receipt_id(invoice_uuid, receipt_state),
			"payload_prefix": f"openapi-invoice-status-{invoice_uuid or 'unknown'}-{receipt_state}",
			"event_label": "received proxy update",
		}

	def normalize_notification(self, notification: Mapping[str, Any]) -> dict[str, Any]:
		notification_type = normalize_identifier(notification.get("type")) or "notification"
		return {
			"external_message_id": normalize_identifier(notification.get("uuid")),
			"transmission_state": self.normalize_notification_state(
				notification.get("type"), notification.get("message")
			),
			"receipt_state": self.normalize_notification_state(
				notification.get("type"), notification.get("message")
			),
			"processing_notes": self.build_notification_processing_notes(notification),
			"receipt_type": notification_type,
			"payload_prefix": f"openapi-notification-{normalize_identifier(notification.get('uuid')) or 'unknown'}",
			"event_label": f"received SDI notification {notification_type}",
		}

	def normalize_incoming_invoice(self, invoice: Mapping[str, Any]) -> dict[str, Any]:
		normalized_invoice = self.normalize_supplier_invoice(invoice)
		invoice_uuid = normalize_identifier(normalized_invoice.get("uuid"))
		return {
			"external_submission_id": invoice_uuid,
			"canonical_identifier": normalized_invoice.get("sdi_file_name") or invoice_uuid,
			"party_name": self.get_registry_name(normalized_invoice.get("sender")),
			"payload": normalized_invoice.get("payload"),
			"processing_notes": self.build_invoice_processing_notes(normalized_invoice),
			"receipt_type": "supplier_invoice_fetched",
			"receipt_state": "delivered",
			"transmission_state": "ready",
			"recipient_identifiers": self.get_invoice_recipient_identifiers(normalized_invoice),
			"payload_prefix": f"openapi-incoming-{invoice_uuid or 'unknown'}",
		}

	def extract_callback_event(self, payload: Mapping[str, Any]) -> str:
		return normalize_identifier(payload.get("event")) or ""

	def extract_callback_notification(self, payload: Mapping[str, Any]) -> dict[str, Any]:
		data = payload.get("data") if isinstance(payload, Mapping) else None
		data = data if isinstance(data, Mapping) else {}
		return dict(data.get("notification") or {})

	def extract_callback_outbound_invoice(self, payload: Mapping[str, Any]) -> dict[str, Any]:
		data = payload.get("data") if isinstance(payload, Mapping) else None
		data = data if isinstance(data, Mapping) else {}
		return dict(data.get("invoice") or {})

	def extract_callback_supplier_invoice(self, payload: Mapping[str, Any]) -> dict[str, Any]:
		data = payload.get("data") if isinstance(payload, Mapping) else None
		data = data if isinstance(data, Mapping) else {}
		return self.normalize_supplier_invoice(data.get("invoice") or {})

	def get_notification_external_submission_id(self, notification: Mapping[str, Any]) -> str | None:
		return normalize_identifier(notification.get("invoice_uuid"))

	def get_outbound_invoice_external_submission_id(self, invoice: Mapping[str, Any]) -> str | None:
		return normalize_identifier(invoice.get("uuid"))

	def is_xml_payload(self, payload: Any) -> bool:
		return isinstance(payload, str) and payload.lstrip().startswith("<")

	def get_additional_settings(self, provider: Mapping[str, Any] | Any) -> dict[str, Any]:
		value = self.get_document_value(provider, "additional_settings")
		if isinstance(value, dict):
			return dict(value)
		if isinstance(value, str):
			value = value.strip()
			return json.loads(value) if value else {}
		return {}

	def get_client(self, provider) -> SDIClient:
		settings = self.get_additional_settings(provider)
		connection_name = normalize_identifier(settings.get("connection_name")) or self.get_default_connection_name(
			self.get_document_value(provider, "environment")
		)
		connection = self.get_connection_doc(connection_name)
		return SDIClient(
			{
				"connection_name": connection_name,
				"environment": self.get_document_value(provider, "environment")
				or self.get_document_value(connection, "environment")
				or "Production",
				"endpoint_url": self.get_document_value(provider, "endpoint_url")
				or self.get_document_value(connection, "endpoint_url"),
				"status_url": self.get_document_value(provider, "status_url")
				or self.get_document_value(connection, "status_url"),
				"oauth_token_url": normalize_identifier(settings.get("oauth_token_url"))
				or self.get_document_value(connection, "oauth_token_url"),
				"timeout_seconds": cint(settings.get("timeout_seconds"))
				or self.get_document_value(connection, "timeout_seconds")
				or 30,
				"auth_mode": "Bearer Token"
				if self.get_document_value(provider, "auth_mode") == "Bearer Token"
				else "OAuth Client Credentials",
				"account_email": self.get_document_value(provider, "username")
				or self.get_document_value(connection, "account_email"),
				"api_key": self.get_document_secret(provider, "api_key")
				or self.get_document_secret(connection, "api_key"),
				"access_token": self.get_document_secret(provider, "password")
				or self.get_document_secret(connection, "access_token"),
				"default_apply_signature": coerce_bool(
					settings.get("apply_signature"),
					default=coerce_bool(
						self.get_document_value(connection, "default_apply_signature"), default=True
					),
				),
				"default_apply_legal_storage": coerce_bool(
					settings.get("apply_legal_storage"),
					default=coerce_bool(
						self.get_document_value(connection, "default_apply_legal_storage"), default=False
					),
				),
				"callback_field": normalize_identifier(settings.get("callback_field"))
				or self.get_document_value(connection, "callback_field"),
				"webhook_auth_header": normalize_identifier(settings.get("webhook_auth_header"))
				or self.get_document_value(connection, "webhook_auth_header"),
			}
		)

	def create_business_registry_configuration(self, client: SDIClient, provider, configuration, fiscal_id: str):
		last_error = None
		for email in self.get_business_registry_candidate_emails(configuration, provider):
			try:
				return client.create_business_registry_configuration(
					fiscal_id=fiscal_id,
					name=self.get_company_display_name(configuration.company),
					email=email,
				)
			except ValidationError as exc:
				last_error = exc
				if "This email already exists" in str(exc) or "612" in str(exc):
					continue
				raise
		raise ValidationError(
			_(
				"OpenAPI requires a unique email for fiscal ID {0}. Update Sender Email, Sender PEC Address, or Account Email and try again."
			).format(fiscal_id)
		) from last_error

	def build_api_configuration_callbacks(self, client: SDIClient, provider) -> list[dict[str, Any]]:
		callback_url = self.build_callback_url(provider)
		callback_field = normalize_identifier(self.get_additional_settings(provider).get("callback_field")) or (
			self.get_document_value(client.connection, "callback_field") or "data"
		)
		auth_header = normalize_identifier(
			self.get_additional_settings(provider).get("webhook_auth_header")
			or self.get_document_value(client.connection, "webhook_auth_header")
		)
		events = ["supplier-invoice", "customer-notification"]
		if cint(self.get_document_value(provider, "use_webhooks")):
			events = [
				"supplier-invoice",
				"customer-invoice",
				"customer-notification",
				"legal-storage-missing-vat",
				"legal-storage-receipt",
			]

		callbacks = []
		for event in events:
			callback = {"event": event, "url": callback_url, "field": callback_field}
			if auth_header:
				callback["auth_header"] = auth_header
			callbacks.append(callback)
		return callbacks

	def build_callback_url(self, provider) -> str:
		webhook_path = normalize_identifier(self.get_document_value(provider, "webhook_path")) or self.default_callback_path
		if webhook_path.startswith(("http://", "https://")):
			return webhook_path
		return frappe.utils.get_url(webhook_path)

	def get_openapi_fiscal_id(self, configuration) -> str:
		for candidate in (
			normalize_vat_code(self.get_document_value(configuration, "sender_vat_id")),
			normalize_identifier(self.get_document_value(configuration, "sender_fiscal_code")),
			normalize_vat_code(frappe.get_cached_value("Company", configuration.company, "tax_id")),
			normalize_identifier(frappe.get_cached_value("Company", configuration.company, "fiscal_code")),
		):
			if candidate:
				return candidate
		raise ValidationError(
			_(
				"EDI Configuration {0} must define Sender VAT ID or Sender Fiscal Code before OpenAPI setup can run."
			).format(configuration.name)
		)

	def get_recipient_values(self, configuration) -> list[str]:
		recipients: list[str] = []
		for candidate in (
			normalize_vat_code(self.get_document_value(configuration, "sender_vat_id")),
			normalize_identifier(self.get_document_value(configuration, "sender_fiscal_code")),
		):
			if candidate and candidate not in recipients:
				recipients.append(candidate)
		if not recipients:
			raise ValidationError(
				_(
					"EDI Configuration {0} must define Sender VAT ID or Sender Fiscal Code before incoming invoice polling can run."
				).format(configuration.name)
			)
		return recipients

	def get_business_registry_candidate_emails(self, configuration, provider) -> list[str]:
		emails = []
		for value in (
			self.get_document_value(configuration, "sender_email"),
			self.get_document_value(configuration, "sender_pec_address"),
			self.get_document_value(provider, "username"),
		):
			email = normalize_identifier(value)
			if email and email not in emails:
				emails.append(email)
		if not emails:
			raise ValidationError(
				_(
					"EDI Configuration {0} must define Sender Email, Sender PEC Address, or the provider must define Account Email before OpenAPI setup can run."
				).format(configuration.name)
			)
		return emails

	def get_company_display_name(self, company_name: str) -> str:
		company = frappe.get_doc("Company", company_name)
		return (
			normalize_identifier(self.get_document_value(company, "company_name"))
			or normalize_identifier(self.get_document_value(company, "name"))
			or company_name
		)

	def build_invoice_status_receipt_id(self, invoice_uuid: str | None, receipt_state: str | None) -> str | None:
		if not invoice_uuid or not receipt_state:
			return None
		return f"openapi_invoice_status:{invoice_uuid}:{receipt_state}"

	def normalize_invoice_marking(self, marking: Any, notice: Any = None) -> str:
		text = " ".join(
			part for part in [normalize_identifier(marking), normalize_identifier(notice)] if part
		).lower()
		if any(token in text for token in ("scarto", "rejected", "reject")):
			return "rejected"
		if any(token in text for token in ("failed", "errore", "error", "imposs")):
			return "failed"
		if any(token in text for token in ("annull", "cancel")):
			return "cancelled"
		if any(token in text for token in ("accett", "accepted", "decorrenza termini")):
			return "accepted"
		if any(token in text for token in ("consegna", "delivered", "delivery")):
			return "delivered"
		if any(token in text for token in ("queued", "coda", "pending", "attesa")):
			return "queued"
		if text:
			return "sent"
		return "unknown_pending"

	def normalize_notification_state(self, notification_type: Any, message: Any = None) -> str:
		code = (normalize_identifier(notification_type) or "").upper()
		message_text = message if isinstance(message, str) else json.dumps(message or {}, ensure_ascii=False)
		text = f"{code} {message_text}".lower()
		if code == "NE":
			outcome_code = self.extract_notification_outcome_code(message)
			if outcome_code == "EC02":
				return "rejected"
			if outcome_code == "EC01":
				return "accepted"
		if code == "NS" or any(token in text for token in ("scarto", "rejected", "reject")):
			return "rejected"
		if code in {"RC", "MC", "AT"} or any(token in text for token in ("consegna", "delivered")):
			return "delivered"
		if code in {"DT", "EC"} or any(token in text for token in ("accepted", "accett", "termini")):
			return "accepted"
		if any(token in text for token in ("failed", "errore", "error")):
			return "failed"
		if any(token in text for token in ("cancel", "annull")):
			return "cancelled"
		if any(token in text for token in ("queued", "pending", "attesa")):
			return "queued"
		return "unknown_pending"

	def extract_notification_outcome_code(self, message: Any) -> str | None:
		if message is None:
			return None

		if isinstance(message, str):
			text = (normalize_identifier(message) or "").upper()
			for code in ("EC02", "EC01"):
				if code in text:
					return code
			return None

		if isinstance(message, Mapping):
			for key in ("esito_committente", "esitoCommittente", "EsitoCommittente"):
				value = message.get(key)
				if isinstance(value, (Mapping, list, tuple)):
					outcome_code = self.extract_notification_outcome_code(value)
					if outcome_code:
						return outcome_code
					continue
				normalized_value = normalize_identifier(value)
				if normalized_value:
					return normalized_value.upper()
			for value in message.values():
				outcome_code = self.extract_notification_outcome_code(value)
				if outcome_code:
					return outcome_code
			return None

		if isinstance(message, (list, tuple, set)):
			for value in message:
				outcome_code = self.extract_notification_outcome_code(value)
				if outcome_code:
					return outcome_code
			return None

		text = (normalize_identifier(message) or "").upper()
		for code in ("EC02", "EC01"):
			if code in text:
				return code
		return None

	def build_invoice_processing_notes(self, invoice: Mapping[str, Any]) -> str:
		parts = []
		for key in ("marking", "notice", "retry_information", "sdi_file_name"):
			value = invoice.get(key)
			if value:
				parts.append(f"{key}: {value}")
		return "\n".join(parts)

	def build_notification_processing_notes(self, notification: Mapping[str, Any]) -> str:
		parts = []
		if notification.get("type"):
			parts.append(f"type: {notification['type']}")
		message = notification.get("message")
		outcome_code = self.extract_notification_outcome_code(message)
		if outcome_code:
			parts.append(f"esito_committente: {outcome_code}")
		if isinstance(message, Mapping):
			for key in ("identificativo_sdi", "nome_file", "message_id", "note"):
				value = message.get(key)
				if value:
					parts.append(f"{key}: {value}")
			error_block = message.get("lista_errori", {}).get("Errore")
			for error in self.ensure_list(error_block):
				if not isinstance(error, Mapping):
					continue
				code = error.get("Codice")
				description = error.get("Descrizione")
				if code or description:
					parts.append(f"error: {code or ''} {description or ''}".strip())
		elif message:
			parts.append(str(message))
		return "\n".join(parts)

	def normalize_supplier_invoice(self, invoice: Mapping[str, Any]) -> dict[str, Any]:
		payload = invoice.get("payload") if isinstance(invoice, Mapping) else None
		payload = payload if isinstance(payload, dict) else {}
		header = payload.get("fattura_elettronica_header") if isinstance(payload, dict) else None
		header = header if isinstance(header, dict) else {}
		sender = invoice.get("sender") if isinstance(invoice.get("sender"), Mapping) else None
		recipient = invoice.get("recipient") if isinstance(invoice.get("recipient"), Mapping) else None
		return {
			**dict(invoice),
			"uuid": invoice.get("uuid"),
			"sdi_file_name": invoice.get("filename") or invoice.get("sdi_file_name"),
			"sender": dict(sender) if sender else self.build_registry_from_party(header.get("cedente_prestatore")),
			"recipient": dict(recipient)
			if recipient
			else self.build_registry_from_party(header.get("cessionario_committente")),
			"payload": payload or invoice.get("payload"),
		}

	def get_invoice_recipient_identifiers(self, invoice: Mapping[str, Any]) -> list[str]:
		recipient = invoice.get("recipient")
		if not isinstance(recipient, Mapping):
			return []
		values = [
			normalize_vat_code(recipient.get("business_vat_number_code")),
			normalize_identifier(recipient.get("business_fiscal_code")),
		]
		return [value for value in values if value]

	def build_registry_from_party(self, party: object) -> dict[str, object]:
		if not isinstance(party, Mapping):
			return {}
		dati_anagrafici = party.get("dati_anagrafici")
		dati_anagrafici = dati_anagrafici if isinstance(dati_anagrafici, Mapping) else {}
		id_fiscale_iva = dati_anagrafici.get("id_fiscale_iva")
		id_fiscale_iva = id_fiscale_iva if isinstance(id_fiscale_iva, Mapping) else {}
		anagrafica = dati_anagrafici.get("anagrafica")
		anagrafica = anagrafica if isinstance(anagrafica, Mapping) else {}
		return {
			"business_name": anagrafica.get("denominazione"),
			"name": anagrafica.get("nome"),
			"surname": anagrafica.get("cognome"),
			"business_vat_number_code": normalize_vat_code(id_fiscale_iva.get("id_codice")),
			"business_fiscal_code": normalize_identifier(dati_anagrafici.get("codice_fiscale"))
			or normalize_vat_code(id_fiscale_iva.get("id_codice")),
		}

	def get_registry_name(self, registry: Mapping[str, Any] | None) -> str | None:
		if not registry:
			return None
		for fieldname in ("business_name", "denominazione", "name"):
			value = registry.get(fieldname)
			if value:
				return str(value)
		name_parts = [registry.get("name"), registry.get("surname")]
		name_parts = [part for part in name_parts if part]
		return " ".join(name_parts) if name_parts else None

	def extract_notification_uuid(self, value: Any) -> str | None:
		if isinstance(value, Mapping):
			return normalize_identifier(value.get("uuid"))
		return normalize_identifier(value)

	def is_complete_notification(self, notification: Mapping[str, Any]) -> bool:
		if self.extract_notification_uuid(notification):
			return True
		return any(
			notification.get(key)
			for key in (
				"invoice_uuid",
				"message",
				"message_id",
				"identificativo_sdi",
				"nome_file",
				"lista_errori",
			)
		)

	def get_default_connection_name(self, environment: str | None) -> str:
		return "SDI Sandbox" if environment == "Sandbox" else "SDI Production"

	def get_connection_value(self, connection_name: str, fieldname: str) -> Any:
		connection = self.get_connection_doc(connection_name)
		return self.get_document_value(connection, fieldname)

	def get_connection_doc(self, connection_name: str):
		try:
			return frappe.get_doc("OpenAPI Connection", connection_name)
		except frappe.DoesNotExistError:
			return {}

	def get_document_value(self, document: Mapping[str, Any] | Any, fieldname: str) -> Any:
		if isinstance(document, Mapping):
			return document.get(fieldname)
		getter = getattr(document, "get", None)
		if callable(getter):
			return getter(fieldname)
		return getattr(document, fieldname, None)

	def get_document_secret(self, document: Mapping[str, Any] | Any, fieldname: str) -> Any:
		get_password = getattr(document, "get_password", None)
		if callable(get_password):
			return get_password(fieldname, raise_exception=False)
		return self.get_document_value(document, fieldname)

	def ensure_list(self, value: Any) -> list[Any]:
		if value is None:
			return []
		if isinstance(value, list):
			return value
		return [value]
