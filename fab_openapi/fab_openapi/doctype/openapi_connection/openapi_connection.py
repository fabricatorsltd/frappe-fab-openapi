from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from fab_openapi.clients.sdi import get_default_endpoint_url, get_default_oauth_token_url, normalize_url


class OpenAPIConnection(Document):
	def validate(self):
		self.endpoint_url = normalize_url(self.endpoint_url) or get_default_endpoint_url(self.environment)
		self.status_url = normalize_url(self.status_url) or self.endpoint_url
		self.oauth_token_url = normalize_url(self.oauth_token_url) or get_default_oauth_token_url(
			self.environment
		)

		if cint(self.timeout_seconds or 0) <= 0:
			frappe.throw(_("Timeout Seconds must be greater than 0."))
