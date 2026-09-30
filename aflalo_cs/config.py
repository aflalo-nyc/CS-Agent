"""Wiring. Which implementation you get is a config decision, not a code change."""

from __future__ import annotations

import os
from pathlib import Path

from .gmail_client import GmailMailbox, Mailbox, MockMailbox
from .shopify import FixtureShopify, Shopify, ShopifyAdminClient

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path | None = None) -> list[str]:
    """Read .env into the environment. Returns the keys it set.

    The real environment always wins, so `SHOPIFY_ADMIN_TOKEN=… python -m …` still overrides
    the file. Deliberately dependency-free and forgiving — this file is hand-edited, and a
    loader that raises on a stray line is worse than one that skips it.
    """
    try:
        text = (path or ROOT / ".env").read_text()
    except OSError:
        return []
    loaded = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        # Blank values mean "not configured yet" — don't set them, or they mask a real
        # environment variable that is set.
        if key and value and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


load_dotenv()

DB_PATH = os.environ.get("AFLALO_DB", str(ROOT / "data" / "cs.db"))
MOCK_INBOX = ROOT / "fixtures" / "mock_inbox.json"
ORDER_FIXTURES = ROOT / "fixtures" / "shopify" / "orders.json"


def get_shopify() -> tuple[Shopify, str]:
    """Live client when credentials exist; fixtures otherwise. Returns (client, mode).

    Two ways to authenticate and the pipeline doesn't care which:

    - `SHOPIFY_ADMIN_TOKEN` — a long-lived `shpat_` token from an admin-created custom app.
    - `SHOPIFY_API_KEY` + `SHOPIFY_API_SECRET` — the Dev Dashboard app's client credentials,
      exchanged for a 24-hour token on demand. Requires the app and the store to be in the
      same Shopify organization.

    Either way `SHOPIFY_SHOP_DOMAIN` is required — it's the host every call is made against.
    """
    domain = os.environ.get("SHOPIFY_SHOP_DOMAIN")
    token = os.environ.get("SHOPIFY_ADMIN_TOKEN")
    client_id = os.environ.get("SHOPIFY_API_KEY")
    client_secret = os.environ.get("SHOPIFY_API_SECRET")
    if domain and token:
        return ShopifyAdminClient(shop_domain=domain, access_token=token), "live (admin token)"
    if domain and client_id and client_secret:
        return (
            ShopifyAdminClient(
                shop_domain=domain, client_id=client_id, client_secret=client_secret
            ),
            "live (client credentials)",
        )
    return FixtureShopify(path=ORDER_FIXTURES), "fixtures"


def shopify_hint() -> str:
    """What is actually missing, rather than a generic 'set these two'."""
    have_domain = bool(os.environ.get("SHOPIFY_SHOP_DOMAIN"))
    have_pair = bool(os.environ.get("SHOPIFY_API_KEY") and os.environ.get("SHOPIFY_API_SECRET"))
    if have_pair and not have_domain:
        return "client id + secret found — set SHOPIFY_SHOP_DOMAIN (the .myshopify.com host) to go live"
    if have_domain and not have_pair:
        return "domain found — set SHOPIFY_ADMIN_TOKEN, or SHOPIFY_API_KEY + SHOPIFY_API_SECRET"
    return "set SHOPIFY_SHOP_DOMAIN + either SHOPIFY_ADMIN_TOKEN or SHOPIFY_API_KEY/_SECRET"


def get_mailbox(live: bool) -> tuple[Mailbox, str]:
    if live:
        return (
            GmailMailbox(
                credentials_path=os.environ.get("GMAIL_CREDENTIALS", "credentials.json"),
                token_path=os.environ.get("GMAIL_TOKEN", "token.json"),
            ),
            "gmail",
        )
    return MockMailbox(path=MOCK_INBOX), "mock"


def anthropic_credential() -> str | None:
    """An unset ANTHROPIC_API_KEY does not mean there are no credentials.

    The SDK resolves in this order: ANTHROPIC_API_KEY -> ANTHROPIC_AUTH_TOKEN -> the profile
    written by `ant auth login`. A bare Anthropic() client picks up the profile with no env
    var set, so check for it before telling anyone to go make a key.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "ANTHROPIC_API_KEY"
    if os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "ANTHROPIC_AUTH_TOKEN"
    cfg = Path(
        os.environ.get("ANTHROPIC_CONFIG_DIR", Path.home() / ".config" / "anthropic")
    )
    profile = os.environ.get("ANTHROPIC_PROFILE", "default")
    if (cfg / "credentials" / f"{profile}.json").exists():
        return f"ant profile '{profile}'"
    return None


def have_anthropic_key() -> bool:
    return anthropic_credential() is not None
