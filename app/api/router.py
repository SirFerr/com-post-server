from fastapi import APIRouter

from .auth import router as auth_router
from .composters import router as composters_router
from .moderation import router as moderation_router

router = APIRouter()
router.include_router(auth_router)
router.include_router(composters_router)
router.include_router(moderation_router)
