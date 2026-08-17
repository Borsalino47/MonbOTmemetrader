"""GoPlus Token Security — the contract-level facts about a token.

WHY A THIRD SOURCE

The RPC proves the chain, the indexer says what is trading; neither can say
whether a token can be sold once bought. That is a contract analysis, and GoPlus
publishes one covering Robinhood Chain (chain id 4663).

THE CONTRACT, AND HOW IT WAS ESTABLISHED

`goplus` 0.2.5 was downloaded from PyPI and read. It is generated from GoPlus's
own Swagger definition, so the field list below is not a guess or a doc summary
— it is the API's own schema: `/api/v1/token_security/{chain_id}` with
`contract_addresses`, wrapped in `{code, message, result: {address: {...}}}`.

The host is refused by this environment's egress policy, like every other
external host here. `cryptopulse doctor-robinhood` on a machine with egress is
what turns this into a fact.

THE ONE RULE THAT MATTERS MOST

**Every field is a string, and an absent field means the check was not
performed.** GoPlus omits what it could not determine rather than returning a
negative. So each flag is tri-state — True / False / None — and None is
propagated all the way to the screen. Reading a missing `is_honeypot` as "not a
honeypot" would turn "we do not know" into "it is safe", which on a token minted
four minutes ago is the single most expensive mistake this module could make.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cryptopulse.config.settings import RobinhoodSettings
from cryptopulse.core.clock import SYSTEM_CLOCK, Clock
from cryptopulse.core.errors import DataUnavailable, SourceUnavailable
from cryptopulse.core.logging import get_logger
from cryptopulse.providers.http import HttpClient

log = get_logger("providers.goplus")

__all__ = ["TokenSecurity", "HolderInfo", "GoPlusClient", "parse_token_security"]


def _flag(value: object) -> bool | None:
    """`"1"` -> True, `"0"` -> False, anything else -> None (not checked).

    The None branch is the whole point: GoPlus omits checks it did not run, and
    an omitted honeypot check is not a clean bill of health.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip()
    if text == "1":
        return True
    if text == "0":
        return False
    return None


def _num(value: object) -> float | None:
    """A decimal string like `"0.05"` or `"12.5"`. Missing stays missing."""
    if value is None or isinstance(value, bool):
        return None
    try:
        text = str(value).strip()
        return float(text) if text else None
    except (TypeError, ValueError):
        return None


def _percent(value: object) -> float | None:
    """A share, normalised to percentage units (0-100) exactly once.

    GoPlus returns shares of supply as *fractions*: `"0.92"` means 92 %, not
    0.92 %. Found by running the scorer: comparing those fractions against
    thresholds expressed in percent broke both directions at once — fully
    burned liquidity read as 0.92 and was flagged "non verrouillée", while ten
    holders at 9 % each summed to 0.9 and never tripped a 60 % concentration
    limit. A clean token looked dangerous and a captured one looked clean.

    Values above 1 are passed through: they cannot be fractions, so they are
    already percentages, and doubling them would be a second bug of the same
    family. The conversion lives here so no caller can do it a second time.
    """
    number = _num(value)
    if number is None:
        return None
    return number * 100.0 if number <= 1.0 else number


@dataclass(slots=True)
class HolderInfo:
    address: str | None
    percent: float | None
    is_locked: bool | None
    is_contract: bool | None
    tag: str | None = None

    def to_dict(self) -> dict:
        return {
            "address": self.address,
            "percent": self.percent,
            "is_locked": self.is_locked,
            "is_contract": self.is_contract,
            "tag": self.tag,
        }


@dataclass(slots=True)
class TokenSecurity:
    """One token's contract facts. Every flag tri-state, every number optional."""

    address: str

    # -- can it be traded at all --------------------------------------------- #
    is_honeypot: bool | None = None
    cannot_sell_all: bool | None = None
    cannot_buy: bool | None = None
    buy_tax: float | None = None
    sell_tax: float | None = None
    slippage_modifiable: bool | None = None
    personal_slippage_modifiable: bool | None = None
    transfer_pausable: bool | None = None
    trading_cooldown: bool | None = None

    # -- what the owner can still do ----------------------------------------- #
    is_mintable: bool | None = None
    owner_change_balance: bool | None = None
    can_take_back_ownership: bool | None = None
    hidden_owner: bool | None = None
    selfdestruct: bool | None = None
    is_proxy: bool | None = None
    is_blacklisted: bool | None = None
    is_whitelisted: bool | None = None
    is_anti_whale: bool | None = None
    anti_whale_modifiable: bool | None = None
    external_call: bool | None = None
    is_open_source: bool | None = None

    # -- who it belongs to ---------------------------------------------------- #
    owner_address: str | None = None
    owner_percent: float | None = None
    creator_address: str | None = None
    creator_percent: float | None = None
    holder_count: int | None = None
    holders: list[HolderInfo] = field(default_factory=list)

    # -- the liquidity pool --------------------------------------------------- #
    lp_holder_count: int | None = None
    lp_holders: list[HolderInfo] = field(default_factory=list)
    lp_total_supply: float | None = None

    # -- reputation ------------------------------------------------------------ #
    honeypot_with_same_creator: bool | None = None
    fake_token: bool | None = None
    is_airdrop_scam: bool | None = None
    is_in_dex: bool | None = None
    trust_list: bool | None = None
    other_potential_risks: str | None = None
    note: str | None = None
    token_name: str | None = None
    token_symbol: str | None = None

    # Which of the checks this project cares about actually came back. Not a
    # score: the denominator of "how much of this analysis exists".
    checks_present: int = 0
    checks_total: int = 0

    @property
    def coverage(self) -> float:
        """0.0-1.0. How much of the security picture the source returned."""
        return (self.checks_present / self.checks_total) if self.checks_total else 0.0

    @property
    def top10_percent(self) -> float | None:
        """Share held by the ten largest holders, excluding burn addresses.

        None when the list is absent — a token with no holder data is not a
        token with no concentration.
        """
        if not self.holders:
            return None
        percents = [h.percent for h in self.holders[:10] if h.percent is not None]
        return sum(percents) if percents else None

    @property
    def lp_locked_percent(self) -> float | None:
        """Share of LP that is locked or burned. None when unknown."""
        if not self.lp_holders:
            return None
        known = [h for h in self.lp_holders if h.percent is not None]
        if not known:
            return None
        locked = [h.percent for h in known if h.is_locked or _is_burn(h.address)]
        return sum(locked) if locked else 0.0

    def to_dict(self) -> dict:
        return {
            "address": self.address,
            "token_name": self.token_name,
            "token_symbol": self.token_symbol,
            "is_honeypot": self.is_honeypot,
            "cannot_sell_all": self.cannot_sell_all,
            "cannot_buy": self.cannot_buy,
            "buy_tax": self.buy_tax,
            "sell_tax": self.sell_tax,
            "slippage_modifiable": self.slippage_modifiable,
            "personal_slippage_modifiable": self.personal_slippage_modifiable,
            "transfer_pausable": self.transfer_pausable,
            "trading_cooldown": self.trading_cooldown,
            "is_mintable": self.is_mintable,
            "owner_change_balance": self.owner_change_balance,
            "can_take_back_ownership": self.can_take_back_ownership,
            "hidden_owner": self.hidden_owner,
            "selfdestruct": self.selfdestruct,
            "is_proxy": self.is_proxy,
            "is_blacklisted": self.is_blacklisted,
            "is_whitelisted": self.is_whitelisted,
            "external_call": self.external_call,
            "is_open_source": self.is_open_source,
            "owner_address": self.owner_address,
            "owner_percent": self.owner_percent,
            "creator_address": self.creator_address,
            "creator_percent": self.creator_percent,
            "holder_count": self.holder_count,
            "top10_percent": self.top10_percent,
            "lp_holder_count": self.lp_holder_count,
            "lp_locked_percent": self.lp_locked_percent,
            "honeypot_with_same_creator": self.honeypot_with_same_creator,
            "fake_token": self.fake_token,
            "is_airdrop_scam": self.is_airdrop_scam,
            "is_in_dex": self.is_in_dex,
            "other_potential_risks": self.other_potential_risks,
            "coverage": round(self.coverage, 3),
            "checks_present": self.checks_present,
            "checks_total": self.checks_total,
        }


# Addresses tokens are burned to. LP sent here can never be pulled.
_BURN_ADDRESSES = frozenset({
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
})


def _is_burn(address: str | None) -> bool:
    return bool(address) and address.lower() in _BURN_ADDRESSES


# The flags whose presence defines "how complete is this analysis". Chosen
# because each one can change a decision; cosmetic fields are excluded so
# coverage measures what matters rather than what is verbose.
_COVERAGE_FLAGS = (
    "is_honeypot", "cannot_sell_all", "cannot_buy", "buy_tax", "sell_tax",
    "is_mintable", "owner_change_balance", "can_take_back_ownership",
    "hidden_owner", "selfdestruct", "is_proxy", "is_blacklisted",
    "transfer_pausable", "is_open_source", "slippage_modifiable",
    "owner_percent", "creator_percent", "holder_count", "lp_holder_count",
)


def _holders(raw: object) -> list[HolderInfo]:
    if not isinstance(raw, list):
        return []
    out: list[HolderInfo] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        out.append(HolderInfo(
            address=item.get("address") if isinstance(item.get("address"), str) else None,
            percent=_percent(item.get("percent")),
            is_locked=_flag(item.get("is_locked")),
            is_contract=_flag(item.get("is_contract")),
            tag=item.get("tag") if isinstance(item.get("tag"), str) else None,
        ))
    return out


def parse_token_security(address: str, raw: dict) -> TokenSecurity:
    """One `result[address]` object into tri-state facts."""
    sec = TokenSecurity(
        address=address,
        is_honeypot=_flag(raw.get("is_honeypot")),
        cannot_sell_all=_flag(raw.get("cannot_sell_all")),
        cannot_buy=_flag(raw.get("cannot_buy")),
        buy_tax=_num(raw.get("buy_tax")),
        sell_tax=_num(raw.get("sell_tax")),
        slippage_modifiable=_flag(raw.get("slippage_modifiable")),
        personal_slippage_modifiable=_flag(raw.get("personal_slippage_modifiable")),
        transfer_pausable=_flag(raw.get("transfer_pausable")),
        trading_cooldown=_flag(raw.get("trading_cooldown")),
        is_mintable=_flag(raw.get("is_mintable")),
        owner_change_balance=_flag(raw.get("owner_change_balance")),
        can_take_back_ownership=_flag(raw.get("can_take_back_ownership")),
        hidden_owner=_flag(raw.get("hidden_owner")),
        selfdestruct=_flag(raw.get("selfdestruct")),
        is_proxy=_flag(raw.get("is_proxy")),
        is_blacklisted=_flag(raw.get("is_blacklisted")),
        is_whitelisted=_flag(raw.get("is_whitelisted")),
        is_anti_whale=_flag(raw.get("is_anti_whale")),
        anti_whale_modifiable=_flag(raw.get("anti_whale_modifiable")),
        external_call=_flag(raw.get("external_call")),
        is_open_source=_flag(raw.get("is_open_source")),
        owner_address=raw.get("owner_address") if isinstance(raw.get("owner_address"), str) else None,
        owner_percent=_percent(raw.get("owner_percent")),
        creator_address=raw.get("creator_address") if isinstance(raw.get("creator_address"), str) else None,
        creator_percent=_percent(raw.get("creator_percent")),
        holder_count=int(_num(raw.get("holder_count"))) if _num(raw.get("holder_count")) is not None else None,
        holders=_holders(raw.get("holders")),
        lp_holder_count=(
            int(_num(raw.get("lp_holder_count"))) if _num(raw.get("lp_holder_count")) is not None else None
        ),
        lp_holders=_holders(raw.get("lp_holders")),
        lp_total_supply=_num(raw.get("lp_total_supply")),
        honeypot_with_same_creator=_flag(raw.get("honeypot_with_same_creator")),
        fake_token=_flag(raw.get("fake_token")) if not isinstance(raw.get("fake_token"), dict) else True,
        is_airdrop_scam=_flag(raw.get("is_airdrop_scam")),
        is_in_dex=_flag(raw.get("is_in_dex")),
        trust_list=_flag(raw.get("trust_list")),
        other_potential_risks=(
            raw.get("other_potential_risks") if isinstance(raw.get("other_potential_risks"), str) else None
        ),
        note=raw.get("note") if isinstance(raw.get("note"), str) else None,
        token_name=raw.get("token_name") if isinstance(raw.get("token_name"), str) else None,
        token_symbol=raw.get("token_symbol") if isinstance(raw.get("token_symbol"), str) else None,
    )
    sec.checks_total = len(_COVERAGE_FLAGS)
    sec.checks_present = sum(1 for name in _COVERAGE_FLAGS if getattr(sec, name) is not None)
    return sec


class GoPlusClient:
    """Read-only client for GoPlus token security."""

    name = "GOPLUS"

    def __init__(self, settings: RobinhoodSettings, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.settings = settings
        self.clock = clock
        self.http = HttpClient(
            settings.goplus_base_url,
            name=self.name,
            weight_per_minute=settings.goplus_calls_per_minute,
            max_concurrent=2,
            timeout=settings.request_timeout_seconds,
            max_retries=settings.max_retries,
            retry_base_delay=settings.retry_base_delay_seconds,
            headers={"accept": "application/json"},
        )

    async def close(self) -> None:
        await self.http.close()

    async def token_security(self, addresses: list[str], *, chain_id: int) -> dict[str, TokenSecurity]:
        """Security facts for up to `goplus_batch_size` tokens in one request.

        Batching is what makes this affordable: scoring thirty discoveries one
        at a time would exhaust a 30-per-minute budget on a single cycle.
        """
        if not addresses:
            return {}
        batch = addresses[: self.settings.goplus_batch_size]
        raw = await self.http.get_json(
            f"/api/v1/token_security/{chain_id}",
            params={"contract_addresses": ",".join(batch)},
        )
        if not isinstance(raw, dict):
            raise SourceUnavailable(f"{self.name}: non-object response")
        code = raw.get("code")
        if code != 1:
            raise DataUnavailable(
                f"{self.name}: {str(raw.get('message', 'unknown error'))[:200]}",
                detail={"code": code},
            )
        result = raw.get("result")
        if not isinstance(result, dict):
            raise DataUnavailable(f"{self.name}: no result object")

        # GoPlus keys the result by lower-cased address; callers hold whatever
        # casing the indexer gave them, so the map is keyed both ways would be
        # ambiguous — normalise to lower and let callers do the same.
        out: dict[str, TokenSecurity] = {}
        for key, value in result.items():
            if isinstance(key, str) and isinstance(value, dict):
                out[key.lower()] = parse_token_security(key.lower(), value)
        missing = [a for a in batch if a.lower() not in out]
        if missing:
            # Not an error: GoPlus returns nothing for a token it has not
            # analysed. Absent stays absent rather than becoming a clean row.
            log.info("goplus_no_data", count=len(missing), chain_id=chain_id)
        return out
