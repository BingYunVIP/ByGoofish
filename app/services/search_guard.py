"""已登录用户的搜索配额与并发限制。"""
import asyncio
from fastapi import HTTPException
from app.services.auth_protection import EmailCodeSendLimiter

_limiter=EmailCodeSendLimiter(cooldown_seconds=15,daily_limit=120,window_seconds=3600)
_locks={}


async def run_search(user_id, operation):
    lock=_locks.setdefault(user_id,asyncio.Lock())
    if lock.locked():raise HTTPException(409,'该账号的搜索仍在运行，请等待完成')
    retry=_limiter.acquire(str(user_id))
    if retry:raise HTTPException(429,'搜索过于频繁，请稍后再试',headers={'Retry-After':str(retry)})
    async with lock:return await operation()
