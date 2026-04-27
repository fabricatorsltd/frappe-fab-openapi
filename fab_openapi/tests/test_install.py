from __future__ import annotations

import unittest

from fab_openapi import install


class TestInstall(unittest.TestCase):
	def test_default_connections_cover_sdi_production_and_sandbox(self):
		connections = {item["connection_name"]: item for item in install.get_default_connections()}

		self.assertEqual(set(connections), {"SDI Production", "SDI Sandbox"})
		self.assertEqual(connections["SDI Production"]["service_type"], "SDI")
		self.assertEqual(connections["SDI Production"]["environment"], "Production")
		self.assertEqual(connections["SDI Production"]["endpoint_url"], "https://sdi.openapi.it")
		self.assertEqual(connections["SDI Production"]["oauth_token_url"], "https://oauth.openapi.it/token")
		self.assertEqual(connections["SDI Sandbox"]["environment"], "Sandbox")
		self.assertEqual(connections["SDI Sandbox"]["endpoint_url"], "https://test.sdi.openapi.it")
		self.assertEqual(
			connections["SDI Sandbox"]["oauth_token_url"],
			"https://test.oauth.openapi.it/token",
		)
