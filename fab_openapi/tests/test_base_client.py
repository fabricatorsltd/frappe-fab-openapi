from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from frappe.exceptions import ValidationError

from fab_openapi.clients import base
from fab_openapi.clients.base import OpenAPIClient, get_default_endpoint_url


class ESignatureProbe(OpenAPIClient):
	"""The smallest client on top of the shared one: its own service and scopes."""

	service_type = "eSignature"

	def token_scope_requests(self):
		return (("POST", "/EU-SES"), ("GET", "/signatures/{id}/{actionType}"))


def connection(**overrides):
	values = {
		"connection_name": "eSignature Sandbox",
		"environment": "Sandbox",
		"endpoint_url": "",
		"status_url": "",
		"oauth_token_url": "",
		"auth_mode": "OAuth Client Credentials",
		"account_email": "account@example.com",
		"timeout_seconds": 30,
		"get_password": lambda fieldname, raise_exception=True: "api-key",
	}
	values.update(overrides)
	return SimpleNamespace(**values)


class TestDefaultEndpoints(unittest.TestCase):
	def test_each_service_has_its_own_endpoint(self):
		self.assertEqual(get_default_endpoint_url("Production", "SDI"), "https://sdi.openapi.it")
		self.assertEqual(
			get_default_endpoint_url("Sandbox", "eSignature"), "https://test.esignature.openapi.com"
		)
		self.assertEqual(
			get_default_endpoint_url("Production", "eSignature"), "https://esignature.openapi.com"
		)

	def test_an_unknown_service_falls_back_to_sdi(self):
		self.assertEqual(get_default_endpoint_url("Sandbox", None), "https://test.sdi.openapi.it")


class TestSharedClient(unittest.TestCase):
	def test_a_service_client_inherits_the_urls_of_its_environment(self):
		client = ESignatureProbe(connection())
		self.assertEqual(client.get_endpoint_url(), "https://test.esignature.openapi.com")
		self.assertEqual(client.get_oauth_token_url(), "https://test.oauth.openapi.it/token")
		self.assertEqual(client.build_url("/EU-SES"), "https://test.esignature.openapi.com/EU-SES")

	def test_one_token_covers_every_scope_the_service_calls(self):
		scope = ESignatureProbe(connection()).full_scope_value()
		self.assertEqual(
			scope,
			"GET:test.esignature.openapi.com/signatures POST:test.esignature.openapi.com/EU-SES",
		)

	def test_the_token_is_minted_with_the_account_credentials(self):
		calls = []

		# a fake token endpoint: what matters is the basic auth and the scopes
		def post(url, **kwargs):
			calls.append((url, kwargs))
			return SimpleNamespace(
				status_code=200, text="", json=lambda: {"token": "tok-1", "expire": "2030-01-01 00:00:00"}
			)

		with patch.object(base.requests, "post", post):
			token = ESignatureProbe(connection()).request_access_token("GET:host/x")
		self.assertEqual(token, "tok-1")
		self.assertEqual(calls[0][0], "https://test.oauth.openapi.it/token")
		self.assertEqual(calls[0][1]["auth"], ("account@example.com", "api-key"))
		self.assertEqual(calls[0][1]["json"], {"scopes": ["GET:host/x"]})

	def test_a_connection_without_credentials_refuses_to_mint(self):
		with self.assertRaises(ValidationError):
			ESignatureProbe(connection(account_email="")).request_access_token("GET:host/x")


if __name__ == "__main__":
	unittest.main()
