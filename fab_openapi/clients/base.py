"""What every OpenAPI service client shares: the connection row it is built from,
the urls it derives, and the OAuth token minted against the account.

The account email and the API key are account level, so one OpenAPI Connection per
service carries the same credentials; what changes per service is the endpoint and
the scopes the token is minted for. A service client subclasses this with its own
default endpoints and its own `token_scope_requests`.
"""

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
from frappe.utils.password import get_decrypted_password, set_encrypted_password

CONNECTION_DOCTYPE = "OpenAPI Connection"

KNOWN_DEFAULT_OAUTH_TOKEN_URLS = {
	"https://console.openapi.com/apis/oauth/token",
	"https://console.openapi.com/oauth/token",
	"https://oauth.openapi.it/token",
	"https://test.oauth.openapi.it/token",
}

# the endpoint each service answers on, per environment
DEFAULT_ENDPOINT_URLS = {
	"SDI": {
		"Production": "https://sdi.openapi.it",
		"Sandbox": "https://test.sdi.openapi.it",
	},
	"eSignature": {
		"Production": "https://esignature.openapi.com",
		"Sandbox": "https://test.esignature.openapi.com",
	},
}

# reuse a minted OAuth token until it nears expiry instead of minting per request;
# re-mint this many seconds early, and assume this lifetime when the provider omits one
TOKEN_EXPIRY_SKEW_SECONDS = 300
TOKEN_FALLBACK_LIFETIME_SECONDS = 7 * 24 * 3600


class OpenAPIClient:
	"""The connection, its urls and its token. Transport and paths are the
	subclass's business."""

	service_type = "SDI"

	def __init__(self, connection: str | Mapping[str, Any] | Any):
		self.connection = get_connection_document(connection)
		self._shared_token: str | None = None

	@classmethod
	def from_connection_name(cls, connection_name: str):
		return cls(connection_name)

	# --- urls and timeouts

	def get_default_endpoint_url(self) -> str:
		return get_default_endpoint_url(
			get_document_value(self.connection, "environment"), self.service_type
		)

	def get_endpoint_url(self) -> str:
		return normalize_url(get_document_value(self.connection, "endpoint_url")) or self.get_default_endpoint_url()

	def get_status_url(self) -> str:
		return normalize_url(get_document_value(self.connection, "status_url")) or self.get_endpoint_url()

	def get_oauth_token_url(self) -> str:
		configured_url = normalize_url(get_document_value(self.connection, "oauth_token_url"))
		default_url = get_default_oauth_token_url(get_document_value(self.connection, "environment"))
		if not configured_url or configured_url in KNOWN_DEFAULT_OAUTH_TOKEN_URLS:
			return default_url
		return configured_url

	def get_timeout_seconds(self) -> int:
		timeout_seconds = cint(get_document_value(self.connection, "timeout_seconds") or 30)
		return timeout_seconds or 30

	def build_url(self, path: str) -> str:
		base_url = self.get_status_url()
		normalized_path = path if path.startswith("/") else f"/{path}"
		return f"{base_url}{normalized_path}"

	# --- authorization

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
			stored, expiry = self.read_stored_token()
			if stored and expiry and get_datetime(expiry) > now_datetime():
				self._shared_token = stored
				return stored
		self._shared_token = self.mint_access_token()
		return self._shared_token

	def db_connection_name(self) -> str | None:
		"""OpenAPI Connection docname to persist the token against. A client may be
		built from a settings dict (not the doc), so we key off connection_name to
		keep one shared token in the DB across every call instead of minting each time."""
		if isinstance(self.connection, Mapping):
			return normalize_identifier(self.connection.get("connection_name")) or normalize_identifier(
				self.connection.get("name")
			)
		return get_document_value(self.connection, "name") or normalize_identifier(
			get_document_value(self.connection, "connection_name")
		)

	def read_stored_token(self) -> tuple[str | None, Any]:
		"""Token + expiry from the OpenAPI Connection row: the source of truth whether
		the client was built from the doc or from a settings dict."""
		name = self.db_connection_name()
		if name and frappe.db.exists(CONNECTION_DOCTYPE, name):
			token = get_decrypted_password(CONNECTION_DOCTYPE, name, "access_token", raise_exception=False)
			expiry = frappe.db.get_value(CONNECTION_DOCTYPE, name, "access_token_expiry")
			return normalize_identifier(token), expiry
		return get_optional_document_secret(self.connection, "access_token"), get_document_value(
			self.connection, "access_token_expiry"
		)

	def stored_token_expired(self) -> bool:
		_token, expiry = self.read_stored_token()
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

	def token_scope_requests(self) -> tuple[tuple[str, str], ...]:
		"""(method, path) of every operation the service calls, so one token covers
		the lot. Each service client says its own."""
		raise NotImplementedError

	def full_scope_value(self) -> str:
		host = urlparse(self.get_status_url()).netloc
		scopes = {
			f"{method.upper()}:{host}{normalize_scope_path(path)}"
			for method, path in self.token_scope_requests()
		}
		return " ".join(sorted(scopes))

	def build_scope_value(self, scope_paths: Iterable[str], method: str = "GET") -> str:
		host = urlparse(self.get_status_url()).netloc
		normalized_paths = [normalize_scope_path(path) for path in scope_paths]
		http_method = method.upper()
		scopes = sorted({f"{http_method}:{host}{path}" for path in normalized_paths})
		return " ".join(scopes)

	def store_access_token(self, token: str, expiry: Any) -> None:
		name = self.db_connection_name()
		if not name or not frappe.db.exists(CONNECTION_DOCTYPE, name):
			return
		set_encrypted_password(CONNECTION_DOCTYPE, name, token, "access_token")
		frappe.db.set_value(CONNECTION_DOCTYPE, name, "access_token_expiry", expiry, update_modified=False)
		# persist now: the polling job / web request may not commit, and without this
		# the token never lands in the DB so every run mints a fresh one
		frappe.db.commit()

	def invalidate_access_token(self) -> None:
		self._shared_token = None
		name = self.db_connection_name()
		if name and frappe.db.exists(CONNECTION_DOCTYPE, name):
			frappe.db.set_value(
				CONNECTION_DOCTYPE, name, "access_token_expiry", None, update_modified=False
			)
			frappe.db.commit()

	def resolve_token_expiry(self, payload: Mapping[str, Any], token: str) -> Any:
		expiry = extract_token_expiry(payload, token)
		if expiry:
			return add_to_date(expiry, seconds=-TOKEN_EXPIRY_SKEW_SECONDS)
		return add_to_date(now_datetime(), seconds=TOKEN_FALLBACK_LIFETIME_SECONDS)

	def _should_retry_auth(self, status_code: int, attempt: int) -> bool:
		return status_code in (401, 403) and attempt == 0 and self.uses_client_credentials()


def get_connection_document(connection: str | Mapping[str, Any] | Any):
	if isinstance(connection, str):
		return frappe.get_doc(CONNECTION_DOCTYPE, connection)
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


def get_default_endpoint_url(environment: str | None = None, service_type: str | None = None) -> str:
	urls = DEFAULT_ENDPOINT_URLS.get(service_type or "SDI", DEFAULT_ENDPOINT_URLS["SDI"])
	return urls.get(environment or "Production", urls["Production"])


def get_default_oauth_token_url(environment: str | None = None) -> str:
	return {
		"Production": "https://oauth.openapi.it/token",
		"Sandbox": "https://test.oauth.openapi.it/token",
	}.get(environment or "Production", "https://oauth.openapi.it/token")


def extract_api_data(payload: Mapping[str, Any] | Any) -> Any:
	"""The body OpenAPI wraps every answer in: {"data": ..., "success": true,
	"message": "", "error": null}. The documented schemas describe the inner part."""
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
