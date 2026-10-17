"""读取 AI 中转站可用模型：用表单里当前填写的接口地址和密钥，无需先保存。"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

from app.services.ai_client import (
    AIConfigError,
    AIRequestError,
    DEFAULT_USER_AGENT,
    fetch_models,
    resolve_base_url,
)
from app.services.auth_protection import EmailCodeSendLimiter

ADMIN_USERNAME = "admin"

# 同一用户两次读取至少间隔 2 秒，每分钟最多 12 次：这个接口会向用户填写的地址发请求。
_limiter = EmailCodeSendLimiter(cooldown_seconds=2, daily_limit=12, window_seconds=60)


class ModelListRequest(BaseModel):
    base_url: str = Field(default="", max_length=300)
    # 留空表示沿用已保存的密钥，但只在接口地址没有变化时才会这样做。
    api_key: str = Field(default="", max_length=300)
    user_agent: str = Field(default="", max_length=200)
    scope: Literal["account", "system"] = "account"
    cookie_id: str = Field(default="", max_length=100)


def create_ai_models_router(get_current_user: Callable[..., Any], db: Any) -> APIRouter:
    router = APIRouter()

    def saved_key(payload: ModelListRequest, base_url: str, user: Dict[str, Any]) -> str:
        """已保存的密钥。只有接口地址与保存时一致才返回：

        否则一次点击（或一段注入页面的脚本）就能把已保存的密钥发给任意服务器。
        """
        if payload.scope == "system":
            if user.get("username") != ADMIN_USERNAME:
                raise HTTPException(status_code=403, detail="需要管理员权限")
            try:
                saved_url = resolve_base_url(db.get_system_setting("ai_api_url"))
            except AIConfigError:
                return ""
            return (db.get_system_setting("ai_api_key") or "") if saved_url == base_url else ""
        if not payload.cookie_id:
            return ""
        details = db.get_cookie_details(payload.cookie_id)
        if not details or details.get("user_id") != user.get("user_id"):
            raise HTTPException(status_code=404, detail="账号不存在或无权限")
        # 账号实际生效的配置：自己的密钥，或系统默认地址下的系统默认密钥。
        # 生效密钥本来就只会发往生效地址，所以地址一致时复用不会多泄露任何东西。
        effective = db.get_ai_reply_settings(payload.cookie_id)
        return effective["api_key"] if effective["api_key"] and effective["base_url"] == base_url else ""

    @router.post("/ai-models")
    async def list_ai_models(payload: ModelListRequest, current_user: Dict[str, Any] = Depends(get_current_user)):
        """读取中转站当前密钥可用的模型列表。"""
        user_id = current_user.get("user_id")
        retry_after = _limiter.acquire(str(user_id))
        if retry_after:
            raise HTTPException(
                status_code=429,
                detail="获取过于频繁，请稍后再试",
                headers={"Retry-After": str(retry_after)},
            )
        try:
            base_url = resolve_base_url(payload.base_url)
        except AIConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

        api_key = payload.api_key.strip()
        reused_saved_key = False
        if not api_key:
            api_key = saved_key(payload, base_url, current_user)
            reused_saved_key = bool(api_key)
            if not api_key:
                raise HTTPException(status_code=400, detail="请先填写 API Key（更换过接口地址时需要重新填写对应的密钥）")

        host = urlsplit(base_url).hostname
        try:
            resolved_url, models = await asyncio.to_thread(
                fetch_models,
                base_url,
                api_key,
                user_agent=payload.user_agent or DEFAULT_USER_AGENT,
                allow_private=current_user.get("username") == ADMIN_USERNAME,
            )
        except AIConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except AIRequestError as exc:
            # 错误说明来自中转站响应，已经脱敏并截断，不包含请求头和密钥。
            logger.warning(f"source=ai_models user_id={user_id} host={host} result=failed status={exc.status_code} kind={exc.kind}")
            raise HTTPException(status_code=502, detail=f"读取模型列表失败：{exc}") from None
        logger.info(
            f"source=ai_models user_id={user_id} host={host} result=ok count={len(models)} "
            f"saved_key={'yes' if reused_saved_key else 'no'}"
        )
        return {"success": True, "base_url": resolved_url, "models": models, "count": len(models)}

    return router
