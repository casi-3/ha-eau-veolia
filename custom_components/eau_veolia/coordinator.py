from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import re

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import VolumeConverter

from .api import Account, DailyReading, PublicTariff, VeoliaAuthError, VeoliaClient, VeoliaError
from .const import (
    CONF_INDEX_ORIGIN,
    CONF_SEWER_FIXED,
    CONF_SEWER_PRICE,
    CONF_WATER_FIXED,
    CONF_WATER_PRICE,
    DOMAIN,
    LOGGER,
    OVERLAP_DAYS,
    RECENT_DAYS,
    UPDATE_INTERVAL,
)


@dataclass(frozen=True)
class Tariff:
    water: float
    water_fixed: float
    sewer: float
    sewer_fixed: float
    water_source: str

    @property
    def enabled(self) -> bool:
        return any(v > 0 for v in (self.water, self.water_fixed, self.sewer, self.sewer_fixed))

    @property
    def fixed_daily(self) -> float:
        return (self.water_fixed + self.sewer_fixed) / 365

    def breakdown(self, liters: float) -> dict[str, float]:
        m3 = liters / 1000
        return {
            "eau_potable": round(m3 * self.water + self.water_fixed / 365, 2),
            "assainissement": round(m3 * self.sewer + self.sewer_fixed / 365, 2),
        }

    def attributes(self) -> dict[str, float | str]:
        return {
            "prix_eau_potable_m3": self.water,
            "abonnement_eau_potable_an": self.water_fixed,
            "prix_assainissement_m3": self.sewer,
            "abonnement_assainissement_an": self.sewer_fixed,
            "source_tarif_eau_potable": self.water_source,
        }

    def day_cost(self, liters: float) -> float:
        return liters / 1000 * (self.water + self.sewer) + self.fixed_daily


@dataclass
class VeoliaData:
    account: Account
    last: DailyReading | None
    readings: list[DailyReading]
    month_liters: float | None
    year_m3: float | None
    average_30d_liters: float | None
    cost_last_day: float | None = None
    cost_last_day_detail: dict[str, float] | None = None
    cost_month: float | None = None
    cost_year: float | None = None
    tariff: Tariff | None = None


def _months(start: date, end: date) -> list[tuple[int, int]]:
    months = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


type VeoliaConfigEntry = ConfigEntry[VeoliaCoordinator]


class VeoliaCoordinator(DataUpdateCoordinator[VeoliaData]):
    config_entry: VeoliaConfigEntry

    def __init__(self, hass: HomeAssistant, entry: VeoliaConfigEntry) -> None:
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = VeoliaClient(
            async_get_clientsession(hass),
            entry.data[CONF_EMAIL],
            entry.data[CONF_PASSWORD],
        )
        self.tariff = self._tariff(None)
        self._public_tariff: PublicTariff | None = None
        self._public_tariff_day: date | None = None
        self._rebuild_cost = entry.entry_id in hass.data.get(DOMAIN, set())

    def _tariff(self, public: PublicTariff | None) -> Tariff:
        options = self.config_entry.options

        def value(key: str, fallback: float | None) -> float | None:
            raw = options.get(key)
            return float(raw) if raw is not None else fallback

        water = value(CONF_WATER_PRICE, public.price_m3 if public else None)
        water_fixed = value(CONF_WATER_FIXED, public.fixed_yearly if public else None)
        manual = CONF_WATER_PRICE in options or CONF_WATER_FIXED in options
        return Tariff(
            water=water or 0.0,
            water_fixed=water_fixed or 0.0,
            sewer=value(CONF_SEWER_PRICE, 0.0) or 0.0,
            sewer_fixed=value(CONF_SEWER_FIXED, 0.0) or 0.0,
            water_source="manuel" if manual else ("veolia" if public else "indisponible"),
        )

    async def _resolve_tariff(self, today: date) -> Tariff:
        options = self.config_entry.options
        needs_public = CONF_WATER_PRICE not in options or CONF_WATER_FIXED not in options
        if needs_public and self._public_tariff_day != today:
            public = await self.client.async_get_public_tariff()
            if public or self._public_tariff is None:
                self._public_tariff = public
            self._public_tariff_day = today
        return self._tariff(self._public_tariff)

    def _statistic_id(self, name: str) -> str:
        suffix = re.sub(r"[^a-z0-9_]", "_", self.config_entry.unique_id.lower())
        return f"{DOMAIN}:{name}_{suffix}"

    @property
    def statistic_id(self) -> str:
        return self._statistic_id("consommation")

    @property
    def cost_statistic_id(self) -> str:
        return self._statistic_id("cout")

    async def _last_imported_day(self, statistic_id: str) -> date | None:
        result = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, False, {"sum"}
        )
        rows = result.get(statistic_id)
        if not rows:
            return None
        return dt_util.as_local(dt_util.utc_from_timestamp(rows[0]["start"])).date()

    async def _sum_before(self, statistic_id: str, day: date, since: date) -> float | None:
        result = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            dt_util.start_of_local_day(since - timedelta(days=1)),
            dt_util.start_of_local_day(day),
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        rows = result.get(statistic_id)
        if not rows:
            return None
        return rows[-1].get("sum")

    async def _async_update_data(self) -> VeoliaData:
        try:
            return await self._fetch()
        except VeoliaAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except VeoliaError as err:
            raise UpdateFailed(str(err)) from err

    async def _fetch(self) -> VeoliaData:
        account = await self.client.async_get_account()
        today = dt_util.now().date()
        self.tariff = await self._resolve_tariff(today)
        last_volume = await self._last_imported_day(self.statistic_id)
        last_cost = (
            await self._last_imported_day(self.cost_statistic_id) if self.tariff.enabled else today
        )
        if self.tariff.enabled and self._rebuild_cost:
            last_cost = None
        if CONF_INDEX_ORIGIN not in self.config_entry.data:
            last_volume = None
        if last_volume is None or last_cost is None:
            start = account.date_debut
        else:
            start = min(
                today - timedelta(days=RECENT_DAYS),
                min(last_volume, last_cost) - timedelta(days=OVERLAP_DAYS),
            )
        start = max(start, account.date_debut)

        by_day: dict[date, DailyReading] = {}
        for year, month in _months(start, today):
            for reading in await self.client.async_get_daily(year, month):
                if account.date_debut <= reading.day < today:
                    by_day[reading.day] = reading
        readings = [by_day[d] for d in sorted(by_day)]

        monthly = await self.client.async_get_monthly(today.year)

        if readings:
            self._import_volume(readings, anchor=start == account.date_debut)
            if self.tariff.enabled:
                base = None
                if not self._rebuild_cost:
                    base = await self._sum_before(
                        self.cost_statistic_id, readings[0].day, account.date_debut
                    )
                self._import_cost(readings, base)
                if self._rebuild_cost:
                    self._rebuild_cost = False
                    self.hass.data.get(DOMAIN, set()).discard(self.config_entry.entry_id)

        recent = [r for r in readings if r.day >= today - timedelta(days=30)]
        month_readings = [
            r for r in readings if (r.day.year, r.day.month) == (today.year, today.month)
        ]
        last = readings[-1] if readings else None
        data = VeoliaData(
            account=account,
            last=last,
            readings=readings,
            month_liters=sum(r.liters for r in month_readings),
            year_m3=round(sum(monthly.values()) / 1000, 3) if monthly else None,
            average_30d_liters=round(sum(r.liters for r in recent) / len(recent))
            if recent
            else None,
        )
        if self.tariff.enabled:
            data.tariff = self.tariff
            data.cost_month = round(sum(self.tariff.day_cost(r.liters) for r in month_readings), 2)
            if last:
                data.cost_last_day = round(self.tariff.day_cost(last.liters), 2)
                data.cost_last_day_detail = self.tariff.breakdown(last.liters)
            if data.year_m3 is not None and last and last.day.year == today.year:
                first_day = max(date(today.year, 1, 1), account.date_debut)
                days = (last.day - first_day).days + 1
                data.cost_year = round(
                    data.year_m3 * (self.tariff.water + self.tariff.sewer)
                    + days * self.tariff.fixed_daily,
                    2,
                )
        return data

    def _index_origin(self, readings: list[DailyReading], anchor: bool) -> float:
        origin = self.config_entry.data.get(CONF_INDEX_ORIGIN)
        if anchor or origin is None:
            origin = readings[0].index_start_m3
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={**self.config_entry.data, CONF_INDEX_ORIGIN: origin},
            )
        return float(origin)

    def _import_volume(self, readings: list[DailyReading], anchor: bool) -> None:
        origin = self._index_origin(readings, anchor)
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name="Eau Veolia consommation",
            source=DOMAIN,
            statistic_id=self.statistic_id,
            unit_class=VolumeConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        )
        stats: list[StatisticData] = []
        if anchor:
            first = readings[0]
            stats.append(
                StatisticData(
                    start=dt_util.start_of_local_day(first.day - timedelta(days=1)),
                    state=0.0,
                    sum=0.0,
                )
            )
        for reading in readings:
            stats.append(
                StatisticData(
                    start=dt_util.start_of_local_day(reading.day),
                    state=round(reading.liters / 1000, 3),
                    sum=round(reading.index_end_m3 - origin, 3),
                )
            )
        LOGGER.debug("Import de %s relevés jusqu'au %s", len(stats), readings[-1].day)
        async_add_external_statistics(self.hass, metadata, stats)

    def _import_cost(self, readings: list[DailyReading], base: float | None) -> None:
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name="Eau Veolia coût",
            source=DOMAIN,
            statistic_id=self.cost_statistic_id,
            unit_class=None,
            unit_of_measurement="EUR",
        )
        stats: list[StatisticData] = []
        if base is None:
            base = 0.0
            stats.append(
                StatisticData(
                    start=dt_util.start_of_local_day(readings[0].day - timedelta(days=1)),
                    state=0.0,
                    sum=0.0,
                )
            )
        total = base
        for reading in readings:
            cost = self.tariff.day_cost(reading.liters)
            total += cost
            stats.append(
                StatisticData(
                    start=dt_util.start_of_local_day(reading.day),
                    state=round(cost, 4),
                    sum=round(total, 4),
                )
            )
        LOGGER.debug("Import de %s coûts jusqu'au %s", len(stats), readings[-1].day)
        async_add_external_statistics(self.hass, metadata, stats)
