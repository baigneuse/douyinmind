"""
API 路由聚合模块

将各子路由（auth / favorites / knowledge / chat）注册到统一前缀 /api 下。
"""
from fastapi import APIRouter

from app.api.routes import auth, chat, favorites, knowledge, knowledge_bases, local_media, network_media

api_router = APIRouter(prefix="/api")

api_router.include_router(auth.router)
api_router.include_router(favorites.router)
api_router.include_router(knowledge.router)
api_router.include_router(knowledge_bases.router)
api_router.include_router(chat.router)
api_router.include_router(local_media.router)
api_router.include_router(network_media.router)
