"""[5] Shopify lookup — one interface, two implementations.

`ShopifyAdminClient` is written against the real Admin GraphQL API and goes live the moment
a token exists. `FixtureShopify` reads the same shape from fixtures/shopify/orders.json so
the rest of the pipeline is testable today. Which one you get is decided by config, not by
a code change.

Read-only by design: this module has no mutation, so the pipeline structurally cannot modify
an order, issue a refund, or create a label.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .models import OrderFacts

ORDER_RE = re.compile(r"#?\b(\d{4,6})\b")

ORDER_QUERY = """
query LookupOrder($q: String!) {
  orders(first: 2, query: $q) {
    edges {
      node {
        id
        name
        createdAt
        displayFulfillmentStatus
        displayFinancialStatus
        customer { firstName numberOfOrders }
        lineItems(first: 20) { edges { node { title quantity variantTitle } } }
        fulfillments(first: 5) {
          createdAt
          estimatedDeliveryAt
          trackingInfo { number url company }
        }
      }
    }
  }
}
"""


class Shopify(Protocol):
    def lookup(self, order_identifier: str | None, fields: list[str]) -> OrderFacts: ...
    def price_lookup(self, product_query: str, country: str) -> OrderFacts: ...
    def product_lookup(self, product_query: str) -> OrderFacts: ...
    def ships_to_countries(self) -> list[str]: ...


def normalize_order_number(text: str | None) -> str | None:
    if not text:
        return None
    m = ORDER_RE.search(text)
    return m.group(1) if m else None


# Shopify's status enums are internal vocabulary. Dropping them verbatim into customer text
# produces "It's currently fulfilled", which is not how a person writes.
STATUS_PROSE = {
    "fulfilled": "on its way",
    "in_transit": "in transit",
    "out_for_delivery": "out for delivery",
    "delivered": "delivered",
    "unfulfilled": "being prepared",
    "on_hold": "on hold",
    "scheduled": "scheduled to ship",
    "partially_fulfilled": "partially on its way",
}


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _project(node: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    """Pull only what the matched category actually needs — nothing else reaches the drafter."""
    ful = (node.get("fulfillments") or [{}])[0]
    tracking = (ful.get("trackingInfo") or [{}])[0] if ful else {}
    items = [e["node"]["title"] for e in node.get("lineItems", {}).get("edges", [])]
    available: dict[str, Any] = {
        "order_number": node.get("name"),
        "fulfillment_status": (node.get("displayFulfillmentStatus") or "").lower() or None,
        "financial_status": (node.get("displayFinancialStatus") or "").lower() or None,
        "tracking_number": tracking.get("number"),
        "tracking_url": tracking.get("url"),
        "carrier": tracking.get("company"),
        "carrier_status": node.get("carrier_status"),
        "carrier_location": node.get("carrier_location"),
        "estimated_delivery": ful.get("estimatedDeliveryAt"),
        "estimated_ship_date": node.get("estimated_ship_date"),
        "line_items": items or None,
        "item": items[0] if items else None,
        # numberOfOrders is UnsignedInt64, which GraphQL serialises as a string ("4").
        # Left as-is it reaches the drafter as text and breaks any numeric comparison.
        "orders_count": _as_int((node.get("customer") or {}).get("numberOfOrders")),
        "first_name": (node.get("customer") or {}).get("firstName"),
    }
    raw_status = available["fulfillment_status"]
    available["fulfillment_status"] = STATUS_PROSE.get(raw_status, raw_status)
    # Keep the machine value for verify.py's status/claim cross-check.
    available["_raw_fulfillment_status"] = raw_status

    keep = set(fields) | {"order_number", "first_name"}
    if "fulfillment_status" in keep:
        keep.add("_raw_fulfillment_status")
    return {k: v for k, v in available.items() if k in keep and v is not None}


# ---------------------------------------------------------------------------- price lookup
#
# Diamond jewelry PDPs can't show price, so those pages were duplicated with price set to
# $0 and international customers email in. The price the pipeline may quote comes from
# here — live, per country — never from memory.

# Customers write their country in prose ("I'm in Italy", "shipping to the UK"). Shopify's
# contextualPricing wants an ISO code. Unknown country -> no lookup -> the draft asks.
COUNTRY_CODES = {
    "italy": "IT", "italia": "IT", "uk": "GB", "united kingdom": "GB", "england": "GB",
    "great britain": "GB", "scotland": "GB", "wales": "GB", "france": "FR",
    "germany": "DE", "deutschland": "DE", "switzerland": "CH", "sweden": "SE",
    "greece": "GR", "belgium": "BE", "poland": "PL", "australia": "AU",
    "new zealand": "NZ", "canada": "CA", "usa": "US", "us": "US", "united states": "US",
    "america": "US", "spain": "ES", "netherlands": "NL", "holland": "NL",
    "portugal": "PT", "austria": "AT", "ireland": "IE", "denmark": "DK", "norway": "NO",
    "finland": "FI", "japan": "JP", "uae": "AE", "dubai": "AE", "israel": "IL",
    "brazil": "BR", "mexico": "MX", "singapore": "SG", "hong kong": "HK",
    "south korea": "KR", "korea": "KR",
}

PRICE_DISCLAIMER = (
    "Duties, taxes, shipping, and timing can vary by destination and time of purchase. "
    "Checkout shows the final total for your location."
)

# Shopify appends a numeric suffix when a page is duplicated ("...-1"). The suffix alone is
# not proof (legitimately renamed products have them too) — non-zero price is the primary
# signal, the suffix only breaks ties.
DUPLICATE_HANDLE_RE = re.compile(r"-\d+$")

PRICE_QUERY = """
query PriceLookup($q: String!, $country: CountryCode!) {
  products(first: 10, query: $q) {
    edges {
      node {
        title
        handle
        status
        variants(first: 50) {
          edges {
            node {
              title
              contextualPricing(context: {country: $country}) {
                price { amount currencyCode }
              }
            }
          }
        }
      }
    }
  }
}
"""


def country_code(country: str | None) -> str | None:
    if not country:
        return None
    text = country.strip().lower()
    if len(text) == 2 and text.upper() in COUNTRY_CODES.values():
        return text.upper()
    return COUNTRY_CODES.get(text)


def _pick_priced_product(
    candidates: list[dict[str, Any]], query: str
) -> tuple[dict[str, Any] | None, list[tuple[float, str]]]:
    """Choose the product to quote from and its non-zero prices.

    The rule from the working note: parse every price, DISCARD the zeros — a 0 is the
    price-suppressed duplicate page, never a price and never "free". Prefer the candidate
    whose title best matches the ask; among ties, the non-suffixed handle (the original,
    not the duplicate). A candidate with no non-zero price at all is unquotable.
    """
    want = set(re.findall(r"[a-z']+", query.lower()))

    best: dict[str, Any] | None = None
    best_key: tuple = ()
    best_prices: list[tuple[float, str]] = []
    for c in candidates:
        prices = [
            (amt, cur)
            for amt, cur in (
                (float(str(p[0]).replace(",", "") or 0), p[1]) for p in c.get("prices", [])
            )
            if amt > 0
        ]
        if not prices:
            continue
        overlap = len(want & set(re.findall(r"[a-z']+", str(c.get("title", "")).lower())))
        not_duplicate = 0 if DUPLICATE_HANDLE_RE.search(str(c.get("handle", ""))) else 1
        key = (overlap, not_duplicate)
        if best is None or key > best_key:
            best, best_key, best_prices = c, key, prices
    return best, best_prices


PRODUCT_QUERY = """
query ProductLookup($q: String!) {
  products(first: 10, query: $q) {
    edges {
      node {
        title
        handle
        status
        description
        variants(first: 50) {
          edges {
            node {
              title price availableForSale selectedOptions { name value }
              inventoryItem {
                inventoryLevels(first: 5) {
                  edges { node { quantities(names: ["available", "incoming"]) { name quantity } } }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""

SHOP_QUERY = "{ shop { currencyCode shipsToCountries } }"


def _size_of(variant: dict[str, Any]) -> str:
    for opt in variant.get("selectedOptions") or []:
        if str(opt.get("name", "")).strip().lower() in ("size", "taglia", "length"):
            return str(opt["value"])
    return str(variant.get("title", ""))


def _product_facts(candidates: list[dict[str, Any]], query: str, currency: str) -> OrderFacts:
    """Catalog answer for a named piece: sizes, per-size stock, price, description.

    This is most of what a customer actually asks. The $0 sentinel rule applies to the
    price fields; description and availability are reported even when every price is
    suppressed, because "what sizes does it come in" deserves an answer regardless.
    """
    if not candidates:
        return OrderFacts(found=False, error=f"no product matching {query!r}")

    want = set(re.findall(r"[a-z']+", query.lower()))

    def score(c):
        overlap = len(want & set(re.findall(r"[a-z']+", str(c.get("title", "")).lower())))
        active = 1 if str(c.get("status", "")).upper() == "ACTIVE" else 0
        priced = 1 if any(v["price"] > 0 for v in c["variants"]) else 0
        original = 0 if DUPLICATE_HANDLE_RE.search(str(c.get("handle", ""))) else 1
        return (overlap, active, priced, original)

    best = max(candidates, key=score)
    in_stock = sorted({_size_of(v) for v in best["variants"] if v["available"]},
                      key=lambda x: (len(x), x))
    out_stock = sorted({_size_of(v) for v in best["variants"] if not v["available"]}
                       - set(in_stock), key=lambda x: (len(x), x))
    # read_inventory: incoming units mean a replenishment is on its way — the factual core
    # of every "is it restocking?" email. The DATE needs read_inventory_shipments (not
    # granted yet), so drafts can say a restock is coming but must blank the when.
    restocking = sorted({_size_of(v) for v in best["variants"]
                         if not v["available"] and v.get("incoming", 0) > 0},
                        key=lambda x: (len(x), x))
    nonzero = sorted({v["price"] for v in best["variants"] if v["price"] > 0})

    fields: dict[str, Any] = {
        "product": best.get("title"),
        "sizes_in_stock": in_stock or None,
        "sizes_sold_out": out_stock or None,
        "sizes_restock_incoming": restocking or None,
    }
    desc = " ".join(str(best.get("description") or "").split())
    if desc:
        fields["product_description"] = desc[:700]
    if nonzero:
        # Whole dollars here too — both retrieval paths must agree or the same piece
        # quotes "4250" one day and "4250.00" the next.
        fields["price_min"] = str(round(nonzero[0]))
        fields["price_max"] = str(round(nonzero[-1]))
        fields["price_currency"] = currency
        fields["price_disclaimer"] = PRICE_DISCLAIMER
    return OrderFacts(found=True, fields={k: v for k, v in fields.items() if v is not None})


def _price_facts(product: dict[str, Any], prices: list[tuple[float, str]], country: str) -> OrderFacts:
    amounts = sorted(a for a, _ in prices)
    currency = prices[0][1]
    # Whole dollars, per review: "$6,640" not "$6,639.95". No thousands separators in
    # the stored value: verify's haystack check is substring-based after comma stripping,
    # so "6640" grounds "€6,640" and "6640 EUR" alike.
    fields = {
        "product": product.get("title"),
        "price_country": country,
        "price_currency": currency,
        "price_min": str(round(amounts[0])),
        "price_max": str(round(amounts[-1])),
        "price_disclaimer": PRICE_DISCLAIMER,
    }
    return OrderFacts(found=True, fields=fields)


@dataclass
class FixtureShopify:
    """Fixture-backed stand-in with the same contract as the live client."""

    path: Path

    def __post_init__(self) -> None:
        self._orders = json.loads(Path(self.path).read_text())
        products_path = Path(self.path).parent / "products.json"
        self._products = (
            json.loads(products_path.read_text()) if products_path.exists() else []
        )
        shop_path = Path(self.path).parent / "shop.json"
        self._shop = (
            json.loads(shop_path.read_text())
            if shop_path.exists()
            else {"currencyCode": "USD", "shipsToCountries": []}
        )

    def ships_to_countries(self) -> list[str]:
        return list(self._shop.get("shipsToCountries", []))

    def product_lookup(self, product_query: str) -> OrderFacts:
        want = set(__import__("re").findall(r"[a-z']+", product_query.lower()))
        candidates = []
        for p in self._products:
            title_tokens = set(__import__("re").findall(r"[a-z']+", p["title"].lower()))
            if not (want & title_tokens):
                continue
            variants = []
            for v in p.get("variants", []):
                amount, _cur = (v.get("contextual", {}).get("US") or ["0", "USD"])
                variants.append({
                    "price": float(str(amount).replace(",", "") or 0),
                    "available": bool(v.get("available", True)),
                    "incoming": int(v.get("incoming", 0)),
                    "selectedOptions": [{"name": k, "value": val} for k, val in (v.get("options") or {}).items()],
                    "title": v.get("title", ""),
                })
            candidates.append({"title": p["title"], "handle": p.get("handle", ""),
                               "status": p.get("status", "ACTIVE"),
                               "description": p.get("description", ""), "variants": variants})
        return _product_facts(candidates, product_query, self._shop.get("currencyCode", "USD"))

    def lookup(self, order_identifier: str | None, fields: list[str]) -> OrderFacts:
        num = normalize_order_number(order_identifier)
        if not num:
            return OrderFacts(found=False, error="no order number in message")
        matches = [o for o in self._orders if str(o.get("name", "")).lstrip("#") == num]
        if not matches:
            return OrderFacts(found=False, error=f"order {num} not found")
        if len(matches) > 1:
            return OrderFacts(found=False, error=f"ambiguous match for order {num}")
        node = matches[0]
        return OrderFacts(found=True, order_id=node.get("name"), fields=_project(node, fields))

    def price_lookup(self, product_query: str, country: str) -> OrderFacts:
        """Same contract as the live client, against fixtures/shopify/products.json."""
        want = set(re.findall(r"[a-z']+", product_query.lower()))
        candidates = []
        for p in self._products:
            title_tokens = set(re.findall(r"[a-z']+", p["title"].lower()))
            if not (want & title_tokens):
                continue
            prices = []
            for v in p.get("variants", []):
                ctx = v.get("contextual", {})
                amount, cur = ctx.get(country) or ctx.get("US") or ("0", "USD")
                prices.append((amount, cur))
            candidates.append({"title": p["title"], "handle": p.get("handle", ""), "prices": prices})
        if not candidates:
            return OrderFacts(found=False, error=f"no product matching {product_query!r}")
        product, prices = _pick_priced_product(candidates, product_query)
        if not product:
            return OrderFacts(
                found=False,
                error=f"every match for {product_query!r} is a $0 price-suppressed page — "
                "a human must confirm the real price",
            )
        return _price_facts(product, prices, country)


# A client-credentials token lasts 24h (expires_in is always 86399). Refresh a minute
# early — a token that expires mid-flight is an indistinguishable 401.
TOKEN_REFRESH_BUFFER_S = 60

# Shopify answers a failed token exchange with an HTML error page, and the useful part is
# one word in its <title>. Dumping the raw page into a routing reason makes the audit trail
# unreadable and tells whoever is looking at it nothing about what to fix.
OAUTH_ERROR_RE = re.compile(r"Oauth error (\w+)|\"error\":\s*\"([^\"]+)\"")

OAUTH_ERROR_HELP = {
    "app_not_installed": (
        "the app is recognised but is not installed on this store — "
        "Dev Dashboard -> your app -> Home -> Install app -> pick the store"
    ),
    "application_cannot_be_found": (
        "SHOPIFY_API_KEY matches no app in this store's organization — "
        "check the client id, and that app and store are in the same org"
    ),
    "invalid_request": "SHOPIFY_API_SECRET was rejected — check the client secret",
    "invalid_client": "SHOPIFY_API_SECRET was rejected — check the client secret",
}


# read_orders and read_customers are protected customer data scopes: granting them in the
# Dev Dashboard is not enough on its own, they also need the protected customer data request
# completed and the app reinstalled. That distinction is the difference between "I ticked the
# box" and "it works", so name it in the error.
REQUIRED_SCOPES = ("read_orders", "read_customers", "read_inventory")
PROTECTED_SCOPES = ("read_orders", "read_customers")

PROBE_QUERY = (
    "{ currentAppInstallation { app { title } accessScopes { handle } } "
    "shop { name currencyCode } }"
)


def _explain_graphql_errors(errors: list[dict[str, Any]]) -> str:
    """An access denial is a missing scope, not a malformed query. Say so."""
    messages = [str(e.get("message", "")).strip() for e in errors]
    denied = [m for m in messages if "access denied" in m.lower()]
    if denied:
        return (
            f"Shopify access denied ({denied[0]}) — the token has no read scope. Grant "
            f"{', '.join(REQUIRED_SCOPES)} in the Dev Dashboard and reinstall; "
            f"{' and '.join(PROTECTED_SCOPES)} also need protected customer data approval."
        )
    return f"Shopify GraphQL error: {'; '.join(messages)[:200]}"


def _explain_oauth_error(body: str) -> str:
    """Turn Shopify's HTML error page into the one line worth acting on."""
    m = OAUTH_ERROR_RE.search(body)
    if not m:
        return body[:200].strip()
    code = m.group(1) or m.group(2)
    help_text = OAUTH_ERROR_HELP.get(code)
    return f"{code} — {help_text}" if help_text else code


def mint_access_token(
    shop_domain: str, client_id: str, client_secret: str, timeout: float = 10.0
) -> tuple[str, float]:
    """Exchange the app's client id + secret for an Admin API access token.

    The client credentials grant, which is the whole point: an app that only ever touches
    stores in its own Shopify organization does not need a merchant to click through OAuth.
    It works only when the app and the store are in the same organization.

    Returns (token, expires_at_epoch_seconds).
    """
    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ).encode()
    req = urllib.request.Request(
        f"https://{shop_domain}/admin/oauth/access_token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read())
    except urllib.error.HTTPError as exc:
        detail = _explain_oauth_error(exc.read().decode("utf-8", "replace"))
        raise RuntimeError(f"Shopify token exchange HTTP {exc.code}: {detail}") from exc
    except OSError as exc:
        raise RuntimeError(f"Shopify token endpoint unreachable: {exc}") from exc

    token = payload.get("access_token")
    if not token:
        raise RuntimeError(f"Shopify token exchange returned no access_token: {payload}")
    return token, time.time() + float(payload.get("expires_in", 86399))


@dataclass
class ShopifyAdminClient:
    """Live Admin GraphQL client. Read-only scopes: read_orders, read_customers, read_inventory.

    Authenticates one of two ways, and it does not care which:

    - `access_token` — a long-lived `shpat_` token from an admin-created custom app.
    - `client_id` + `client_secret` — the Dev Dashboard app's credentials, exchanged for a
      24-hour token on first use and re-minted automatically when it ages out.
    """

    shop_domain: str
    access_token: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    api_version: str = "2026-01"
    timeout: float = 10.0
    _token: str | None = field(default=None, init=False, repr=False)
    _expires_at: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self) -> None:
        if not (self.access_token or (self.client_id and self.client_secret)):
            raise ValueError(
                "ShopifyAdminClient needs either access_token, or client_id + client_secret"
            )

    @property
    def mints_its_own_token(self) -> bool:
        return not self.access_token

    def _bearer(self, force_refresh: bool = False) -> str:
        """The value for X-Shopify-Access-Token, minted and cached as needed."""
        if self.access_token:
            return self.access_token
        if force_refresh or not self._token or time.time() >= self._expires_at - TOKEN_REFRESH_BUFFER_S:
            self._token, self._expires_at = mint_access_token(
                self.shop_domain, self.client_id or "", self.client_secret or "", self.timeout
            )
        return self._token

    @property
    def endpoint(self) -> str:
        return f"https://{self.shop_domain}/admin/api/{self.api_version}/graphql.json"

    def _graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """POST a query with a minted token. Raises; callers translate to OrderFacts."""
        payload = json.dumps({"query": query, "variables": variables or {}}).encode()

        def call(token: str) -> dict[str, Any]:
            req = urllib.request.Request(
                self.endpoint,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Shopify-Access-Token": token,
                },
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())

        try:
            return call(self._bearer())
        except urllib.error.HTTPError as exc:
            # A revoked or rotated token reads as a 401. Worth exactly one re-mint —
            # retrying further would just hammer the token endpoint with bad credentials.
            if exc.code != 401 or not self.mints_its_own_token:
                raise
            return call(self._bearer(force_refresh=True))

    def probe(self) -> str:
        """Mint a token and report what it can actually reach.

        Credentials resolving is not the same as the API working: an app can be installed,
        hold a valid token, and still be granted no scopes at all — which fails later as an
        access denial on a customer's email rather than at startup.
        """
        try:
            body = self._graphql(PROBE_QUERY)
        except urllib.error.HTTPError as exc:
            return f"HTTP {exc.code}"
        except (OSError, RuntimeError) as exc:
            return str(exc)

        if body.get("errors"):
            return _explain_graphql_errors(body["errors"])
        data = body.get("data") or {}
        shop = (data.get("shop") or {}).get("name", "?")
        install = data.get("currentAppInstallation") or {}
        # Name the app. Two similarly-named apps on one store is a real failure mode, and
        # without this the symptom is "I granted the scopes and nothing happened".
        app = (install.get("app") or {}).get("title", "?")
        granted = {s["handle"] for s in install.get("accessScopes", [])}
        missing = [s for s in REQUIRED_SCOPES if s not in granted]
        where = f"app '{app}' on '{shop}'"
        if not granted:
            return (
                f"{where} is granted NO scopes — add {', '.join(REQUIRED_SCOPES)} to "
                f"THIS app (check the name) in the Dev Dashboard, release, and reinstall"
            )
        if missing:
            return f"{where} — missing scopes: {', '.join(missing)}"
        return f"{where} — {len(granted)} scopes, all required ones present"

    def lookup(self, order_identifier: str | None, fields: list[str]) -> OrderFacts:
        num = normalize_order_number(order_identifier)
        if not num:
            return OrderFacts(found=False, error="no order number in message")

        try:
            body = self._graphql(ORDER_QUERY, {"q": f"name:{num}"})
        except urllib.error.HTTPError as exc:
            return OrderFacts(found=False, error=f"Shopify HTTP {exc.code}")
        except OSError as exc:
            return OrderFacts(found=False, error=f"Shopify unreachable: {exc}")
        except RuntimeError as exc:
            return OrderFacts(found=False, error=str(exc))

        if body.get("errors"):
            return OrderFacts(found=False, error=_explain_graphql_errors(body["errors"]))

        edges = body.get("data", {}).get("orders", {}).get("edges", [])
        if not edges:
            return OrderFacts(found=False, error=f"order {num} not found")
        if len(edges) > 1:
            return OrderFacts(found=False, error=f"ambiguous match for order {num}")

        node = edges[0]["node"]
        return OrderFacts(found=True, order_id=node.get("name"), fields=_project(node, fields))

    def ships_to_countries(self) -> list[str]:
        """Where the store ships — shop-level, stable, cached for the client's lifetime."""
        if getattr(self, "_ships_to", None) is None:
            body = self._graphql(SHOP_QUERY)
            shop = (body.get("data") or {}).get("shop") or {}
            self._ships_to = list(shop.get("shipsToCountries") or [])
            self._shop_currency = shop.get("currencyCode") or "USD"
        return self._ships_to

    def product_lookup(self, product_query: str) -> OrderFacts:
        """Catalog facts for a named piece: sizes, per-size stock, price, description."""
        try:
            body = self._graphql(PRODUCT_QUERY, {"q": product_query})
        except urllib.error.HTTPError as exc:
            return OrderFacts(found=False, error=f"Shopify HTTP {exc.code}")
        except OSError as exc:
            return OrderFacts(found=False, error=f"Shopify unreachable: {exc}")
        except RuntimeError as exc:
            return OrderFacts(found=False, error=str(exc))
        if body.get("errors"):
            return OrderFacts(found=False, error=_explain_graphql_errors(body["errors"]))

        candidates = []
        for edge in body.get("data", {}).get("products", {}).get("edges", []):
            node = edge["node"]
            if str(node.get("status", "")).upper() == "ARCHIVED":
                continue
            variants = []
            for v in node.get("variants", {}).get("edges", []):
                vn = v["node"]
                incoming = sum(
                    q["quantity"]
                    for lv in (vn.get("inventoryItem") or {}).get("inventoryLevels", {}).get("edges", [])
                    for q in lv["node"].get("quantities", [])
                    if q.get("name") == "incoming"
                )
                variants.append({
                    "price": float(str(vn.get("price") or "0").replace(",", "") or 0),
                    "available": bool(vn.get("availableForSale")),
                    "incoming": incoming,
                    "selectedOptions": vn.get("selectedOptions") or [],
                    "title": vn.get("title", ""),
                })
            candidates.append(
                {"title": node.get("title"), "handle": node.get("handle"),
                 "status": node.get("status"), "description": node.get("description"),
                 "variants": variants}
            )
        currency = getattr(self, "_shop_currency", None) or "USD"
        return _product_facts(candidates, product_query, currency)

    def price_lookup(self, product_query: str, country: str) -> OrderFacts:
        """Per-country price for a named piece, via contextualPricing (read_products).

        Zero prices are the $0 price-suppressed duplicate pages and are discarded before
        anything is quoted; if every match is zero, that is a human's problem, not a $0
        quote. Returns min/max of the non-zero variant prices — a range when they differ.
        """
        try:
            body = self._graphql(PRICE_QUERY, {"q": product_query, "country": country})
        except urllib.error.HTTPError as exc:
            return OrderFacts(found=False, error=f"Shopify HTTP {exc.code}")
        except OSError as exc:
            return OrderFacts(found=False, error=f"Shopify unreachable: {exc}")
        except RuntimeError as exc:
            return OrderFacts(found=False, error=str(exc))

        if body.get("errors"):
            return OrderFacts(found=False, error=_explain_graphql_errors(body["errors"]))

        candidates = []
        for edge in body.get("data", {}).get("products", {}).get("edges", []):
            node = edge["node"]
            if str(node.get("status", "")).upper() == "ARCHIVED":
                continue
            prices = []
            for v in node.get("variants", {}).get("edges", []):
                cp = (v["node"].get("contextualPricing") or {}).get("price") or {}
                prices.append((cp.get("amount") or "0", cp.get("currencyCode") or "USD"))
            candidates.append(
                {"title": node.get("title"), "handle": node.get("handle"), "prices": prices}
            )
        if not candidates:
            return OrderFacts(found=False, error=f"no product matching {product_query!r}")
        product, prices = _pick_priced_product(candidates, product_query)
        if not product:
            return OrderFacts(
                found=False,
                error=f"every match for {product_query!r} is a $0 price-suppressed page — "
                "a human must confirm the real price",
            )
        return _price_facts(product, prices, country)
