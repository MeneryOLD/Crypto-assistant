"""Portfolio: key vault, credential parsing, permission checks, valuation, storage, screens (no network)."""

import ccxt.async_support as ccxt
import pytest

from bot import portfolio_ui as ui
from collectors.accounts import AccountError, AccountReader, parse_credentials
from storage.db import Database
from storage.vault import Vault, VaultError
from tests.test_insights import _app

KEY, SECRET = "k" * 20, "s" * 20


class FakeClient:
    def __init__(self, *, withdraw=False, auth_ok=True):
        self.markets = {}
        self.withdraw, self.auth_ok, self.closed = withdraw, auth_ok, False

    async def sapi_get_account_apirestrictions(self):
        return {"enableWithdrawals": self.withdraw, "enableSpotAndMarginTrading": False}

    async def fetch_balance(self, params=None):
        if not self.auth_ok:
            raise ccxt.AuthenticationError("bad key")
        if params and params.get("type") == "funding":
            return {"total": {"USDT": 50.0, "BTC": 0.0}}
        return {"total": {"BTC": 0.5, "LDETH": 2.0, "USDT": 100.0, "SHIB": 10.0, "WEIRD": 3.0}}

    async def load_markets(self):
        self.markets = {s: {"spot": True} for s in ("BTC/USDT", "ETH/USDT", "SHIB/USDT")}

    async def fetch_tickers(self, symbols):
        prices = {"BTC/USDT": (60000.0, 2.0), "ETH/USDT": (3000.0, -1.0), "SHIB/USDT": (0.00001, 5.0)}
        return {s: {"last": prices[s][0], "percentage": prices[s][1]} for s in symbols}

    async def close(self):
        self.closed = True


def reader(**kw) -> AccountReader:
    return AccountReader(lambda ex, creds: FakeClient(**kw))


def test_vault_roundtrip_and_wrong_key(tmp_path):
    v = Vault.open(None, tmp_path / "vault.key")
    token = v.seal({"apiKey": KEY, "secret": SECRET})
    assert KEY not in token and v.unseal(token)["secret"] == SECRET
    assert Vault.open(None, tmp_path / "vault.key").unseal(token)["apiKey"] == KEY  # same key file reused
    with pytest.raises(VaultError):
        Vault.open(None, tmp_path / "other.key").unseal(token)


def test_parse_credentials():
    assert parse_credentials(f"{KEY}\n{SECRET}", False) == {"apiKey": KEY, "secret": SECRET}
    assert parse_credentials(f"{KEY} {SECRET} pass123", True)["password"] == "pass123"
    with pytest.raises(AccountError):
        parse_credentials(KEY, False)
    with pytest.raises(AccountError):
        parse_credentials(f"{KEY} {SECRET}", True)  # OKX needs a passphrase


async def test_connect_values_balances():
    view, perms = await reader().connect("binance", {"apiKey": KEY, "secret": SECRET})
    assert perms.label == "read-only"
    got = {h.asset: h for h in view.holdings}
    assert got["BTC"].usd == pytest.approx(30000) and got["ETH"].amount == 2.0  # LDETH (Simple Earn) counts as ETH
    assert got["USDT"].amount == 150.0  # spot + funding wallets
    assert view.dust == 1 and view.unpriced == ["WEIRD"]  # SHIB worth $0.0001; WEIRD has no USD market
    assert view.total == pytest.approx(30000 + 6000 + 150)
    assert view.change_usd == pytest.approx(30000 * 2 / 102 - 6000 * 1 / 99)


async def test_withdraw_keys_and_bad_keys_are_refused():
    with pytest.raises(AccountError, match="withdraw"):
        await reader(withdraw=True).connect("binance", {"apiKey": KEY, "secret": SECRET})
    with pytest.raises(AccountError, match="rejected the key"):
        await reader(auth_ok=False).connect("bybit", {"apiKey": KEY, "secret": SECRET})
    view = await reader(auth_ok=False).read("bybit", {"apiKey": KEY, "secret": SECRET})
    assert view.error and "rejected" in view.error  # reading never raises


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.init()
    yield d
    await d.close()


async def test_accounts_storage_and_screens(db, tmp_path):
    app = _app(db)
    app.vault, app.accounts = Vault.open(None, tmp_path / "k"), reader()
    text, kb = await ui.portfolio_screen(app, 1)
    assert "Connect your exchange" in text and "Read" in text  # nothing connected yet

    await db.save_account(1, "binance", app.vault.seal({"apiKey": KEY, "secret": SECRET}), "read-only")
    await db.save_account(1, "binance", app.vault.seal({"apiKey": KEY, "secret": SECRET}), "read-only")  # replaces
    await db.save_account(2, "okx", app.vault.seal({"apiKey": KEY, "secret": SECRET, "password": "p"}))
    assert [a.exchange for a in await db.accounts(1)] == ["binance"]
    assert KEY not in (await db.accounts(1))[0].credentials

    text, kb = await ui.portfolio_screen(app, 1, refresh=True)
    assert "Your portfolio" in text and "$36,150" in text and "BTC" in text and "Binance" in text and "WEIRD" in text
    for screen in (ui.portfolio_screen(app, 1), ui.add_screen(app, 1), ui.disconnect_screen(app, 1, "binance")):
        _, kb = await screen
        assert all(len(b.callback_data.encode()) <= 64 for row in kb.inline_keyboard for b in row)
    assert "OKX" in ui.connect_instructions("okx")[0] and "PASSPHRASE" in ui.connect_instructions("okx")[0]

    assert await db.delete_account(1, "binance") and not await db.accounts(1)
    ui.forget(1)
    assert "Connect your exchange" in (await ui.portfolio_screen(app, 1))[0]
