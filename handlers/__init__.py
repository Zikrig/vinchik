from aiogram import Router

from handlers import admin, admin_links, browse, fallback, menu, premium, profile, start


def setup_routers() -> Router:
    root = Router()
    root.include_router(start.router)
    root.include_router(profile.router)
    root.include_router(browse.router)
    root.include_router(menu.router)
    root.include_router(premium.router)
    root.include_router(admin.router)
    root.include_router(admin_links.router)
    root.include_router(fallback.router)
    return root
