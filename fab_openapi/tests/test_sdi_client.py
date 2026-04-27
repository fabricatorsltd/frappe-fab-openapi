from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from frappe.exceptions import ValidationError

from fab_openapi.clients.sdi import SDIClient, coerce_bool, extract_api_data, matches_openapi_fiscal_id


class TestSDIClient(unittest.TestCase):
	def test_default_urls_follow_environment(self):
		client = SDIClient(
			SimpleNamespace(
				connection_name="SDI Sandbox",
				environment="Sandbox",
				endpoint_url="",
				status_url="",
				oauth_token_url="",
				auth_mode="Bearer Token",
				get_password=lambda fieldname: "token",
				timeout_seconds=30,
				default_apply_signature=1,
				default_apply_legal_storage=0,
			)
		)

		self.assertEqual(client.get_endpoint_url(), "https://test.sdi.openapi.it")
		self.assertEqual(client.get_status_url(), "https://test.sdi.openapi.it")
		self.assertEqual(client.get_oauth_token_url(), "https://test.oauth.openapi.it/token")

	def test_default_oauth_url_follows_environment_even_if_placeholder_is_stale(self):
		client = SDIClient(
			SimpleNamespace(
				connection_name="SDI Sandbox",
				environment="Sandbox",
				endpoint_url="https://test.sdi.openapi.it",
				status_url="https://test.sdi.openapi.it",
				oauth_token_url="https://oauth.openapi.it/token",
				auth_mode="Bearer Token",
				get_password=lambda fieldname: "token",
				timeout_seconds=30,
				default_apply_signature=1,
				default_apply_legal_storage=0,
			)
		)

		self.assertEqual(client.get_oauth_token_url(), "https://test.oauth.openapi.it/token")

	def test_build_scope_value_uses_request_method(self):
		client = SDIClient(
			SimpleNamespace(
				connection_name="SDI Sandbox",
				environment="Sandbox",
				endpoint_url="https://test.sdi.openapi.it",
				status_url="https://test.sdi.openapi.it",
				oauth_token_url="https://test.oauth.openapi.it/token",
				auth_mode="Bearer Token",
				get_password=lambda fieldname: "token",
				timeout_seconds=30,
				default_apply_signature=1,
				default_apply_legal_storage=0,
			)
		)

		self.assertEqual(client.build_scope_value((SDIClient.invoices_path,), method="POST"), "POST:test.sdi.openapi.it/invoices")

	def test_submit_invoice_xml_posts_to_signature_endpoint_by_default(self):
		client = SDIClient(
			SimpleNamespace(
				connection_name="SDI Sandbox",
				environment="Sandbox",
				endpoint_url="",
				status_url="",
				oauth_token_url="",
				auth_mode="Bearer Token",
				get_password=lambda fieldname: "token",
				timeout_seconds=30,
				default_apply_signature=1,
				default_apply_legal_storage=0,
			)
		)

		with patch.object(client, "request_json", return_value={"data": {"uuid": "inv-123"}}) as request_json:
			response = client.submit_invoice_xml("<xml />")

		self.assertEqual(response, {"uuid": "inv-123"})
		request_json.assert_called_once_with(
			method="POST",
			path=SDIClient.invoices_signature_path,
			data="<xml />",
			content_type="application/xml",
		)

	def test_get_invoice_uses_base_invoice_scope(self):
		client = SDIClient(
			SimpleNamespace(
				connection_name="SDI Sandbox",
				environment="Sandbox",
				endpoint_url="https://test.sdi.openapi.it",
				status_url="https://test.sdi.openapi.it",
				oauth_token_url="https://test.oauth.openapi.it/token",
				auth_mode="Bearer Token",
				get_password=lambda fieldname: "token",
				timeout_seconds=30,
				default_apply_signature=1,
				default_apply_legal_storage=0,
			)
		)

		with patch.object(client, "request_json", return_value={"data": [{"uuid": "inv-123"}]}) as request_json:
			response = client.get_invoice("inv-123")

		self.assertEqual(response, {"uuid": "inv-123"})
		request_json.assert_called_once_with(
			"GET",
			"/invoices/inv-123",
			scope_paths=(SDIClient.invoices_path,),
		)

	def test_bearer_mode_uses_access_token(self):
		client = SDIClient(
			SimpleNamespace(
				connection_name="SDI Production",
				environment="Production",
				endpoint_url="https://sdi.openapi.it",
				status_url="https://sdi.openapi.it",
				oauth_token_url="https://oauth.openapi.it/token",
				auth_mode="Bearer Token",
				get_password=lambda fieldname: "secret-token" if fieldname == "access_token" else None,
				timeout_seconds=30,
				default_apply_signature=1,
				default_apply_legal_storage=0,
			)
		)

		self.assertEqual(client.get_authorization_header((SDIClient.invoices_path,), method="GET"), "Bearer secret-token")

	def test_oauth_mode_requires_account_email_and_api_key(self):
		client = SDIClient(
			SimpleNamespace(
				connection_name="SDI Production",
				environment="Production",
				endpoint_url="https://sdi.openapi.it",
				status_url="https://sdi.openapi.it",
				oauth_token_url="https://oauth.openapi.it/token",
				auth_mode="OAuth Client Credentials",
				account_email="",
				get_password=lambda fieldname: None,
				timeout_seconds=30,
				default_apply_signature=1,
				default_apply_legal_storage=0,
			)
		)

		with self.assertRaisesRegex(ValidationError, "requires Account Email and API Key"):
			client.request_access_token("GET:sdi.openapi.it/invoices")

	def test_business_registry_payload_uses_connection_defaults(self):
		client = SDIClient(
			SimpleNamespace(
				connection_name="SDI Production",
				environment="Production",
				endpoint_url="https://sdi.openapi.it",
				status_url="https://sdi.openapi.it",
				oauth_token_url="https://oauth.openapi.it/token",
				auth_mode="Bearer Token",
				get_password=lambda fieldname: "token",
				timeout_seconds=30,
				default_apply_signature=1,
				default_apply_legal_storage=0,
			)
		)

		self.assertEqual(
			client.build_business_registry_configuration_payload(
				fiscal_id="04266880980",
				name="Fabricators SRL",
				email="billing@example.com",
			),
			{
				"fiscal_id": "04266880980",
				"name": "Fabricators SRL",
				"email": "billing@example.com",
				"apply_signature": True,
				"apply_legal_storage": False,
			},
		)

	def test_api_configuration_has_required_callbacks_detects_url_drift(self):
		client = SDIClient(
			SimpleNamespace(
				connection_name="SDI Sandbox",
				environment="Sandbox",
				endpoint_url="https://test.sdi.openapi.it",
				status_url="https://test.sdi.openapi.it",
				oauth_token_url="https://test.oauth.openapi.it/token",
				auth_mode="Bearer Token",
				get_password=lambda fieldname: "token",
				timeout_seconds=30,
				default_apply_signature=1,
				default_apply_legal_storage=0,
			)
		)

		with patch.object(
			client,
			"list_api_configurations_for_fiscal_id",
			return_value=[
				{
					"callback": {
						"event": "supplier-invoice",
						"url": "http://e16.localhost:8000/api/method/fab_italy_edi.api.receive_openapi_callback",
						"field": "data",
					}
				},
				{
					"callback": {
						"event": "customer-notification",
						"url": "http://e16.localhost:8000/api/method/fab_italy_edi.api.receive_openapi_callback",
						"field": "data",
					}
				},
			],
		):
			result = client.api_configuration_has_required_callbacks(
				"04266880980",
				[
					{
						"event": "supplier-invoice",
						"url": "https://e16.fabricators.dev/api/method/fab_italy_edi.api.receive_openapi_callback",
						"field": "data",
					},
					{
						"event": "customer-notification",
						"url": "https://e16.fabricators.dev/api/method/fab_italy_edi.api.receive_openapi_callback",
						"field": "data",
					},
				],
			)

		self.assertFalse(result)

	def test_extract_api_data_prefers_data_key(self):
		self.assertEqual(extract_api_data({"data": [1, 2, 3], "success": True}), [1, 2, 3])

	def test_matches_openapi_fiscal_id_handles_prefixed_vat(self):
		self.assertTrue(matches_openapi_fiscal_id("IT04266880980", "04266880980"))
		self.assertTrue(matches_openapi_fiscal_id("04266880980", "IT04266880980"))
		self.assertFalse(matches_openapi_fiscal_id("ABCDEF12G34H567I", "IT04266880980"))

	def test_coerce_bool_respects_common_values(self):
		self.assertTrue(coerce_bool("1", default=False))
		self.assertFalse(coerce_bool("0", default=True))
		self.assertTrue(coerce_bool(None, default=True))
