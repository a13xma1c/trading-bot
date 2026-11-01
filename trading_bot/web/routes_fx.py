"""Exchange-rate API (latest table, history, converter, currency list)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ..fx import FxError
from .context import WebContext
from .users import User


def build_fx_router(ctx: WebContext) -> APIRouter:
    router = APIRouter(prefix="/api/fx", tags=["fx"])
    require_auth = ctx.require_auth

    def _fx() -> Any:
        if ctx.fx is None:
            raise HTTPException(status_code=503, detail="Exchange rates are not configured (fx.provider)")
        return ctx.fx

    def _code(value: str, name: str) -> str:
        value = value.strip().upper()
        if not (len(value) == 3 and value.isalpha()):
            raise HTTPException(status_code=422, detail=f"{name} must be a three-letter currency code")
        return value

    @router.get("/latest")
    def latest(base: str = Query(default=""), user: User = Depends(require_auth)) -> dict[str, Any]:
        fx = _fx()
        base_code = _code(base, "base") if base else fx.base_currency
        try:
            table = fx.latest(base_code)
        except FxError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        names = fx.currencies()
        data = table.to_dict()
        data["provider"] = fx.provider.name
        data["account_currency"] = ctx.config.payments.currency
        data["rows"] = [{"currency": code, "name": names.get(code, code), "rate": rate, "inverse": (1 / rate) if rate else None}
                        for code, rate in sorted(table.rates.items())]
        return data

    @router.get("/history")
    def history(base: str = Query(default=""), quote: str = Query(min_length=3, max_length=3),
                days: int = Query(default=90, ge=2, le=3660), user: User = Depends(require_auth)) -> dict[str, Any]:
        fx = _fx()
        base_code = _code(base, "base") if base else fx.base_currency
        quote_code = _code(quote, "quote")
        try:
            points = fx.history(base_code, quote_code, days)
        except FxError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        rates = [p["rate"] for p in points]
        change = (rates[-1] / rates[0] - 1) * 100 if len(rates) >= 2 and rates[0] else None
        return {"base": base_code, "quote": quote_code, "days": days, "points": points,
                "min": min(rates) if rates else None, "max": max(rates) if rates else None, "change_pct": change,
                "provider": fx.provider.name}

    @router.get("/convert")
    def convert(amount: float = Query(gt=0), from_currency: str = Query(alias="from", min_length=3, max_length=3),
                to_currency: str = Query(alias="to", min_length=3, max_length=3),
                user: User = Depends(require_auth)) -> dict[str, Any]:
        fx = _fx()
        try:
            return fx.convert(amount, _code(from_currency, "from"), _code(to_currency, "to"))
        except FxError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.get("/currencies")
    def currencies(user: User = Depends(require_auth)) -> dict[str, Any]:
        fx = _fx()
        names = fx.currencies()
        return {"base": fx.base_currency, "account_currency": ctx.config.payments.currency,
                "currencies": [{"code": code, "name": name} for code, name in sorted(names.items())]}

    return router


__all__ = ["build_fx_router"]
