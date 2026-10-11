from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime
import json
import time
from typing import Any

import aiohttp

from .const import (
    BACKEND_URL,
    COGNITO_CLIENT_ID,
    COGNITO_URL,
    COMMUNES_URL,
    TYPE_FRONT,
    USER_AGENT,
)

AUTH_ERRORS = {
    "NotAuthorizedException",
    "UserNotFoundException",
    "PasswordResetRequiredException",
    "UserNotConfirmedException",
}
TIMEOUT = aiohttp.ClientTimeout(total=30)
ATTEMPTS = 3


class VeoliaError(Exception):
    pass


class VeoliaAuthError(VeoliaError):
    pass


@dataclass(frozen=True)
class Account:
    id_abonnement: str
    numero_pds: str
    numero_compteur: str
    date_debut: date
    code_insee: str | None
    code_contrat: str | None


@dataclass(frozen=True)
class PublicTariff:
    price_m3: float
    fixed_yearly: float


@dataclass(frozen=True)
class DailyReading:
    day: date
    liters: float
    index_start_m3: float
    reliability: str | None

    @property
    def index_end_m3(self) -> float:
        return round(self.index_start_m3 + self.liters / 1000, 3)


class VeoliaClient:
    def __init__(self, session: aiohttp.ClientSession, email: str, password: str) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._token: str | None = None
        self._token_expiry = 0.0
        self.account: Account | None = None

    async def _login(self) -> None:
        payload = {
            "ClientId": COGNITO_CLIENT_ID,
            "AuthFlow": "USER_PASSWORD_AUTH",
            "AuthParameters": {"USERNAME": self._email, "PASSWORD": self._password},
        }
        headers = {
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
            "User-Agent": USER_AGENT,
        }
        try:
            async with self._session.post(
                COGNITO_URL, data=json.dumps(payload), headers=headers, timeout=TIMEOUT
            ) as resp:
                body = await resp.text()
                status = resp.status
        except (aiohttp.ClientError, TimeoutError) as err:
            raise VeoliaError(f"Connexion Veolia impossible : {err}") from err
        try:
            data = json.loads(body) if body else {}
        except ValueError as err:
            raise VeoliaError(f"Réponse de connexion illisible (HTTP {status})") from err
        if status != 200:
            kind = str(data.get("__type", "")).split("#")[-1]
            message = data.get("message", body[:200])
            if kind in AUTH_ERRORS:
                raise VeoliaAuthError(message)
            raise VeoliaError(f"Connexion refusée (HTTP {status}, {kind}) : {message}")
        result = data.get("AuthenticationResult") or {}
        token = result.get("AccessToken")
        if not token:
            challenge = data.get("ChallengeName")
            raise VeoliaAuthError(f"Connexion incomplète, défi demandé : {challenge}")
        self._token = token
        self._token_expiry = time.monotonic() + int(result.get("ExpiresIn", 3600)) - 120

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        last_error: Exception | None = None
        for attempt in range(ATTEMPTS):
            if not self._token or time.monotonic() >= self._token_expiry:
                await self._login()
            headers = {
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            }
            try:
                async with self._session.get(
                    f"{BACKEND_URL}{path}", params=params, headers=headers, timeout=TIMEOUT
                ) as resp:
                    if resp.status == 204:
                        return None
                    if resp.status == 200:
                        return await resp.json(content_type=None)
                    if resp.status in (401, 403):
                        self._token = None
                        last_error = VeoliaError(f"{path} : accès refusé (HTTP {resp.status})")
                    elif resp.status == 429 or resp.status >= 500:
                        last_error = VeoliaError(f"{path} : HTTP {resp.status}")
                    else:
                        raise VeoliaError(f"{path} : HTTP {resp.status}")
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                last_error = VeoliaError(f"{path} : {err}")
            if attempt < ATTEMPTS - 1:
                await asyncio.sleep(2 ** (attempt + 1))
        raise last_error or VeoliaError(path)

    async def async_get_account(self) -> Account:
        if self.account:
            return self.account
        data = await self._get("/espace-client", {"type-front": TYPE_FRONT})
        try:
            abonnement = data["contacts"][0]["tiers"][0]["abonnements"][0]
            id_abonnement = str(abonnement["id_abonnement"])
        except (KeyError, IndexError, TypeError) as err:
            raise VeoliaError("Aucun abonnement trouvé sur ce compte") from err
        facturation = await self._get(f"/abonnements/{id_abonnement}/facturation")
        try:
            account = Account(
                id_abonnement=id_abonnement,
                numero_pds=str(facturation["numero_pds"]),
                numero_compteur=str(
                    abonnement.get("numero_compteur") or facturation.get("numero_compteur") or ""
                ),
                date_debut=date.fromisoformat(facturation["date_debut_abonnement"]),
                code_insee=facturation.get("code_insee_facturation"),
                code_contrat=abonnement.get("code_contrat"),
            )
        except (KeyError, TypeError, ValueError) as err:
            raise VeoliaError("Données d'abonnement incomplètes") from err
        self.account = account
        return account

    def _params(self, year: int, month: int | None = None) -> dict[str, Any]:
        assert self.account
        params: dict[str, Any] = {
            "annee": year,
            "numero-pds": self.account.numero_pds,
            "date-debut-abonnement": self.account.date_debut.isoformat(),
        }
        if month is not None:
            params["mois"] = month
        return params

    async def async_get_daily(self, year: int, month: int) -> list[DailyReading]:
        account = await self.async_get_account()
        rows = await self._get(
            f"/consommations/{account.id_abonnement}/journalieres", self._params(year, month)
        )
        readings = []
        for row in rows or []:
            try:
                readings.append(
                    DailyReading(
                        day=datetime.strptime(row["date_releve"][:10], "%Y-%m-%d").date(),
                        liters=float(row["consommation"]["litre"]),
                        index_start_m3=float(row["index"]["m3"]),
                        reliability=row.get("fiabilite_index"),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return readings

    async def async_get_monthly(self, year: int) -> dict[int, float]:
        account = await self.async_get_account()
        rows = await self._get(
            f"/consommations/{account.id_abonnement}/mensuelles", self._params(year)
        )
        totals: dict[int, float] = {}
        for row in rows or []:
            try:
                if int(row["annee"]) == year:
                    totals[int(row["mois"])] = float(row["consommation"]["litre"])
            except (KeyError, TypeError, ValueError):
                continue
        return totals

    async def async_get_public_tariff(self) -> PublicTariff | None:
        account = await self.async_get_account()
        if not account.code_insee:
            return None
        try:
            async with self._session.get(
                f"{COMMUNES_URL}/communes/{account.code_insee}",
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                timeout=TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return None
        contrats = [c for c in data.get("contrats") or [] if c.get("activite") == "EAU"]
        for contrat in contrats:
            if account.code_contrat and contrat.get("code") != account.code_contrat:
                continue
            tarifs = contrat.get("tarifs") or {}
            try:
                return PublicTariff(
                    price_m3=float(tarifs["un_m3"]),
                    fixed_yearly=float(tarifs.get("abonnement_semestriel") or 0) * 2,
                )
            except (KeyError, TypeError, ValueError):
                continue
        return None
