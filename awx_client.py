#!/usr/bin/env python3
"""Thin REST client for the Airwallex sandbox.

One interface for every sandbox-only simulation call (CLAUDE.md's
architecture rule), so decision logic never talks HTTP directly and the
simulation calls could be swapped out without touching it.
"""

import json
import os
import urllib.error
import urllib.request
import uuid
from decimal import Decimal

BASE = "https://api.sandbox.airwallex.com"


class AwxClient:
    def __init__(self, client_id=None, api_key=None):
        self.client_id = (client_id or os.environ.get("AWX_CLIENT_ID", "")).strip()
        self.api_key = (api_key or os.environ.get("AWX_API_KEY", "")).strip()
        self.token = None

    def _call(self, method, path, body=None, extra_headers=None):
        url = BASE + path
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if extra_headers:
            headers.update(extra_headers)
        # default=str so a Decimal amount in a request body serializes as a
        # plain numeric string - Airwallex accepts string amounts (used for
        # deposit simulation already), and it avoids ever re-introducing a
        # float on the way out.
        data = json.dumps(body, default=str).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw, status = resp.read().decode(), resp.status
        except urllib.error.HTTPError as e:
            raw, status = e.read().decode(), e.code
        try:
            # parse_float=Decimal: every amount coming back from the API
            # becomes a Decimal, never a float - money never touches binary
            # floating point anywhere in this codebase (docs/judge-audit.md B1).
            payload = json.loads(raw, parse_float=Decimal) if raw else {}
        except json.JSONDecodeError:
            payload = raw
        return status, payload

    def login(self):
        status, payload = self._call(
            "POST", "/api/v1/authentication/login",
            extra_headers={"x-client-id": self.client_id, "x-api-key": self.api_key},
        )
        if status not in (200, 201) or not isinstance(payload, dict) or not payload.get("token"):
            raise RuntimeError(f"Airwallex login failed [HTTP {status}]: {payload}")
        self.token = payload["token"]
        return self.token

    def list_global_accounts(self):
        status, payload = self._call("GET", "/api/v1/global_accounts")
        if status != 200:
            raise RuntimeError(f"list_global_accounts failed [HTTP {status}]: {payload}")
        return _as_list(payload)

    def create_global_account(self, country_code, currency, transfer_method="LOCAL"):
        body = {
            "request_id": str(uuid.uuid4()),
            "country_code": country_code,
            "required_features": [{"currency": currency, "transfer_method": transfer_method}],
        }
        status, payload = self._call("POST", "/api/v1/global_accounts/create", body=body)
        if status not in (200, 201) or not payload.get("id"):
            raise RuntimeError(f"create_global_account failed [HTTP {status}]: {payload}")
        return payload

    def simulate_deposit(self, global_account_id, amount, payer_name, reference=None):
        """The one sandbox-only interface for simulated incoming money."""
        body = {
            "request_id": str(uuid.uuid4()),
            "global_account_id": global_account_id,
            "amount": amount,
            "payer_name": payer_name,
        }
        if reference:
            body["reference"] = reference
        status, payload = self._call("POST", "/api/v1/simulation/deposit/create", body=body)
        if status not in (200, 201):
            raise RuntimeError(f"simulate_deposit failed [HTTP {status}]: {payload}")
        return payload

    def list_deposits(self):
        """Plain GET, no x-api-version, no date params - verified working."""
        status, payload = self._call("GET", "/api/v1/deposits")
        if status != 200:
            raise RuntimeError(f"list_deposits failed [HTTP {status}]: {payload}")
        return _as_list(payload)

    def get_fx_rate(self, sell_currency, buy_currency="USD"):
        """A rate check, not a quote - no quote_id, no client_rate. Read the
        top-level `rate` field (confirmed live: it matches the CLIENT level
        entry in rate_details). No x-api-version header - confirmed none is
        sent by default here, and a stale one returns incorrect_version per
        the builder guide."""
        status, payload = self._call(
            "GET", f"/api/v1/fx/rates/current?sell_currency={sell_currency}&buy_currency={buy_currency}"
        )
        if status != 200:
            raise RuntimeError(f"get_fx_rate failed [HTTP {status}]: {payload}")
        return payload

    def beneficiary_schema(self, country_code, currency, transfer_method, entity_type="COMPANY"):
        body = {"country_code": country_code, "currency": currency,
                "transfer_method": transfer_method, "entity_type": entity_type}
        status, payload = self._call("POST", "/api/v1/beneficiaries/schema", body=body)
        if status != 200:
            raise RuntimeError(f"beneficiary_schema failed [HTTP {status}]: {payload}")
        return payload

    def create_beneficiary(self, beneficiary, transfer_methods):
        """transfer_methods, not payment_methods - confirmed empirically:
        payment_methods is rejected with validation code 001."""
        body = {
            "beneficiary": beneficiary,
            "transfer_methods": transfer_methods,
            "request_id": str(uuid.uuid4()),
        }
        status, payload = self._call("POST", "/api/v1/beneficiaries/create", body=body)
        if status not in (200, 201):
            raise RuntimeError(f"create_beneficiary failed [HTTP {status}]: {payload}")
        return payload

    def create_transfer(self, beneficiary_id, currency, amount, transfer_method,
                         reason, reference, request_id):
        """Returns (status, payload) - unlike the other calls, this one does
        NOT raise on failure. A 400 duplicate_request_id is an expected,
        meaningful response here (idempotency working as intended), not an
        error the caller wants turned into an exception."""
        body = {
            "beneficiary_id": beneficiary_id,
            "transfer_currency": currency,
            "transfer_amount": amount,
            "transfer_method": transfer_method,
            "source_currency": currency,
            "reason": reason,
            "reference": reference,
            "request_id": request_id,
        }
        return self._call("POST", "/api/v1/transfers/create", body=body)

    def transition_transfer(self, transfer_id, next_status):
        return self._call(
            "POST", f"/api/v1/simulation/transfers/{transfer_id}/transition",
            body={"next_status": next_status},
        )

    def get_transfer(self, transfer_id):
        status, payload = self._call("GET", f"/api/v1/transfers/{transfer_id}")
        if status != 200:
            raise RuntimeError(f"get_transfer failed [HTTP {status}]: {payload}")
        return payload


def _as_list(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "data", "deposits", "global_accounts", "results"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []
