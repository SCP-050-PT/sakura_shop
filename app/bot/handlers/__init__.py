from aiogram import Router

from app.bot.handlers import admin_catalog, admin_panel, catalog, common, deposit, inline, profile, support


def setup_routers() -> Router:
    root = Router(name="root")
    # common first: /start and the menu button must escape any FSM state;
    # admin next so admin input states win over user handlers.
    root.include_routers(
        common.router,
        admin_panel.router,
        admin_catalog.router,
        catalog.router,
        profile.router,
        deposit.router,
        support.router,
        inline.router,
    )
    return root
