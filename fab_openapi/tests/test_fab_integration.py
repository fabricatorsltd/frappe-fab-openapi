from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fab_openapi.integrations.fab_italy_edi import OpenAPITransportBackend


class TestFabItalyEdiIntegration(unittest.TestCase):
	def test_normalize_provider_document_sets_connection_defaults(self):
		backend = OpenAPITransportBackend()
		provider = SimpleNamespace(
			environment="Production",
			auth_mode="API Key",
			password=None,
			username=" TEST@EXAMPLE.COM ",
			use_environment_default_endpoint=1,
			endpoint_url="",
			status_url="",
			additional_settings={},
		)

		with patch.object(
			backend,
			"get_connection_value",
			side_effect=lambda connection_name, fieldname: {
				("SDI Production", "endpoint_url"): "https://sdi.openapi.it",
				("SDI Production", "status_url"): "https://sdi.openapi.it",
			}.get((connection_name, fieldname)),
		):
			backend.normalize_provider_document(provider)

		self.assertEqual(provider.auth_mode, "Basic")
		self.assertEqual(provider.username, "test@example.com")
		self.assertEqual(provider.endpoint_url, "https://sdi.openapi.it")
		self.assertEqual(provider.status_url, "https://sdi.openapi.it")
		self.assertEqual(provider.additional_settings["connection_name"], "SDI Production")

	def test_extract_callback_supplier_invoice_normalizes_parties(self):
		backend = OpenAPITransportBackend()
		payload = {
			"data": {
				"invoice": {
					"uuid": "sup-uuid",
					"filename": "IT123.xml",
					"payload": {
						"fattura_elettronica_header": {
							"cedente_prestatore": {
								"dati_anagrafici": {
									"id_fiscale_iva": {"id_codice": "IT01234567890"},
									"anagrafica": {"denominazione": "Supplier SRL"},
								}
							},
							"cessionario_committente": {
								"dati_anagrafici": {
									"id_fiscale_iva": {"id_codice": "IT04266880980"},
									"anagrafica": {"denominazione": "Fabricators SRL"},
								}
							},
						}
					},
				}
			}
		}

		invoice = backend.extract_callback_supplier_invoice(payload)

		self.assertEqual(invoice["uuid"], "sup-uuid")
		self.assertEqual(invoice["sdi_file_name"], "IT123.xml")
		self.assertEqual(invoice["sender"]["business_name"], "Supplier SRL")
		self.assertEqual(invoice["recipient"]["business_vat_number_code"], "04266880980")

	def test_normalize_outbound_invoice_maps_state_and_receipt_id(self):
		backend = OpenAPITransportBackend()

		normalized = backend.normalize_outbound_invoice(
			{"uuid": "inv-123", "marking": "Accettata", "sdi_file_name": "IT001.xml"}
		)

		self.assertEqual(normalized["transmission_state"], "accepted")
		self.assertEqual(normalized["receipt_state"], "accepted")
		self.assertEqual(normalized["canonical_identifier"], "IT001.xml")
		self.assertEqual(normalized["receipt_message_id"], "openapi_invoice_status:inv-123:accepted")

	def test_normalize_notification_maps_rejection(self):
		backend = OpenAPITransportBackend()

		normalized = backend.normalize_notification(
			{"uuid": "ntf-123", "type": "NS", "message": {"note": "Scarto"}}
		)

		self.assertEqual(normalized["transmission_state"], "rejected")
		self.assertEqual(normalized["receipt_type"], "NS")
		self.assertEqual(normalized["external_message_id"], "ntf-123")

	def test_normalize_notification_maps_delivery_related_codes(self):
		backend = OpenAPITransportBackend()

		for code in ("MC", "AT"):
			with self.subTest(code=code):
				normalized = backend.normalize_notification(
					{"uuid": f"ntf-{code.lower()}", "type": code, "message": {"note": "recapito"}}
				)

				self.assertEqual(normalized["transmission_state"], "delivered")
				self.assertEqual(normalized["receipt_state"], "delivered")

	def test_normalize_notification_maps_decorrenza_termini_as_accepted(self):
		backend = OpenAPITransportBackend()

		normalized = backend.normalize_notification(
			{"uuid": "ntf-dt", "type": "DT", "message": {"note": "Decorrenza termini"}}
		)

		self.assertEqual(normalized["transmission_state"], "accepted")
		self.assertEqual(normalized["receipt_state"], "accepted")

	def test_normalize_notification_maps_notifica_esito_from_outcome_code(self):
		backend = OpenAPITransportBackend()

		for outcome_code, expected_state in (("EC01", "accepted"), ("EC02", "rejected")):
			with self.subTest(outcome_code=outcome_code):
				normalized = backend.normalize_notification(
					{
						"uuid": f"ntf-{outcome_code.lower()}",
						"type": "NE",
						"message": {
							"esito_committente": outcome_code,
							"message_id": f"msg-{outcome_code.lower()}",
						},
					}
				)

				self.assertEqual(normalized["transmission_state"], expected_state)
				self.assertEqual(normalized["receipt_state"], expected_state)
				self.assertIn(outcome_code, normalized["processing_notes"])

	def test_normalize_notification_maps_nested_notifica_esito_payload(self):
		backend = OpenAPITransportBackend()

		normalized = backend.normalize_notification(
			{
				"uuid": "ntf-ne-nested",
				"type": "NE",
				"message": {
					"esito_committente": {
						"IDENTIFICATIVO_SDI": "111",
						"ESITO": "EC02",
						"DESCRIZIONE": "NOTIFICA DI ESEMPIO",
					},
					"message_id": "msg-ne-nested",
				},
			}
		)

		self.assertEqual(normalized["transmission_state"], "rejected")
		self.assertEqual(normalized["receipt_state"], "rejected")
		self.assertIn("EC02", normalized["processing_notes"])

	def test_normalize_notification_maps_deep_nested_notifica_esito_payload_with_scalar_values(self):
		backend = OpenAPITransportBackend()

		normalized = backend.normalize_notification(
			{
				"uuid": "ntf-ne-deep",
				"type": "NE",
				"message": {
					"esito_committente": {
						"IDENTIFICATIVO_SDI": "111",
						"RIFERIMENTO_FATTURA": {
							"NUMERO_FATTURA": "1111",
							"ANNO_FATTURA": 2013,
							"POSIZIONE_FATTURA": 2,
						},
						"ESITO": "EC02",
					},
					"message_id": "msg-ne-deep",
				},
			}
		)

		self.assertEqual(normalized["transmission_state"], "rejected")
		self.assertEqual(normalized["receipt_state"], "rejected")
		self.assertIn("EC02", normalized["processing_notes"])

	def test_iter_invoice_notifications_skips_incomplete_inline_refs(self):
		backend = OpenAPITransportBackend()
		provider = SimpleNamespace()
		client = SimpleNamespace(
			get_notification=Mock(
				return_value={
					"uuid": "ntf-full",
					"type": "NS",
					"message": {"note": "Scarto"},
				}
			)
		)

		with patch.object(backend, "get_client", return_value=client):
			notifications = backend.iter_invoice_notifications(
				provider,
				{
					"notifications": [
						{"type": "NE", "created_at": "2026-04-26T20:48:25+00:00"},
						{"uuid": "ntf-full"},
					]
				},
			)

		self.assertEqual(notifications, [{"uuid": "ntf-full", "type": "NS", "message": {"note": "Scarto"}}])
		client.get_notification.assert_called_once_with("ntf-full")

	def test_list_incoming_invoices_uses_recipient_filters(self):
		backend = OpenAPITransportBackend()
		configuration = SimpleNamespace(name="fabricators", sender_vat_id="IT04266880980", sender_fiscal_code=None)
		provider = SimpleNamespace()
		client = SimpleNamespace(list_invoices=Mock(return_value=[{"uuid": "sup-uuid"}]))

		with patch.object(backend, "get_client", return_value=client):
			rows = backend.list_incoming_invoices(configuration, provider)

		self.assertEqual(rows, [{"uuid": "sup-uuid", "sdi_file_name": None, "sender": {}, "recipient": {}, "payload": None}])
		client.list_invoices.assert_called_once_with(
			params={"downloaded": "false", "type": "1", "recipient": "04266880980"}
		)

	def test_build_api_configuration_callbacks_adds_webhook_auth_header(self):
		backend = OpenAPITransportBackend()
		provider = SimpleNamespace(use_webhooks=0, webhook_path="/api/method/fab_italy_edi.api.receive_openapi_callback")
		client = SimpleNamespace(connection={"callback_field": "data", "webhook_auth_header": None})

		with patch.object(
			backend,
			"get_additional_settings",
			return_value={"webhook_auth_header": "Bearer abc"},
		), patch.object(backend, "build_callback_url", return_value="https://erp.test/callback"):
			callbacks = backend.build_api_configuration_callbacks(client, provider)

		self.assertEqual(
			callbacks,
			[
				{
					"event": "supplier-invoice",
					"url": "https://erp.test/callback",
					"field": "data",
					"auth_header": "Bearer abc",
				},
				{
					"event": "customer-notification",
					"url": "https://erp.test/callback",
					"field": "data",
					"auth_header": "Bearer abc",
				},
			],
		)

	def test_build_api_configuration_callbacks_expands_events_when_webhooks_enabled(self):
		backend = OpenAPITransportBackend()
		provider = SimpleNamespace(use_webhooks=1, webhook_path="/api/method/fab_italy_edi.api.receive_openapi_callback")
		client = SimpleNamespace(connection={"callback_field": "data", "webhook_auth_header": None})

		with patch.object(backend, "get_additional_settings", return_value={}), patch.object(
			backend, "build_callback_url", return_value="https://erp.test/callback"
		):
			callbacks = backend.build_api_configuration_callbacks(client, provider)

		self.assertEqual(
			[event["event"] for event in callbacks],
			[
				"supplier-invoice",
				"customer-invoice",
				"customer-notification",
				"legal-storage-missing-vat",
				"legal-storage-receipt",
			],
		)
