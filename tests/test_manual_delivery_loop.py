"""手动发货必须在账号循环执行，不能跨线程使用库存锁和消息连接。"""
import asyncio
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from app import reply_server as server


class ManualDeliveryLoopTests(unittest.TestCase):
    def test_full_delivery_runs_once_on_account_thread(self):
        loop = asyncio.new_event_loop()
        started = threading.Event()
        def run():
            asyncio.set_event_loop(loop)
            loop.call_soon(started.set)
            loop.run_forever()
        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(started.wait(5))
        calls = []
        async def operation(*args):
            calls.append((asyncio.get_running_loop(), threading.get_ident(), args))
            return {'success_count': 1}
        try:
            with patch.object(server.cookie_manager, 'manager', SimpleNamespace(loop=loop)), \
                 patch.object(server, '_manual_ship_orders_impl', side_effect=operation):
                result = asyncio.run(server.manual_ship_orders(['fixture-order'], 'full_delivery', None, {'user_id': 1}))
            self.assertEqual(result, {'success_count': 1})
            self.assertEqual(len(calls), 1)
            self.assertIs(calls[0][0], loop)
            self.assertEqual(calls[0][1], thread.ident)
            self.assertEqual(calls[0][2], (['fixture-order'], 'full_delivery', None, {'user_id': 1}))
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(5)
            loop.close()

    def test_same_loop_does_not_dispatch_or_duplicate(self):
        async def exercise():
            operation = AsyncMock(return_value={'success_count': 1})
            with patch.object(server.cookie_manager, 'manager', SimpleNamespace(loop=asyncio.get_running_loop())), \
                 patch.object(server, '_manual_ship_orders_impl', operation):
                await server.manual_ship_orders(['fixture-order'], 'full_delivery', None, {'user_id': 1})
            operation.assert_awaited_once()
        asyncio.run(exercise())

    def test_offline_account_service_never_starts_delivery(self):
        operation = AsyncMock()
        with patch.object(server.cookie_manager, 'manager', None), \
             patch.object(server, '_manual_ship_orders_impl', operation):
            with self.assertRaises(HTTPException) as error:
                asyncio.run(server.manual_ship_orders(['fixture-order'], 'full_delivery', None, {'user_id': 1}))
        self.assertEqual(error.exception.status_code, 503)
        operation.assert_not_awaited()

    def test_status_only_keeps_independent_session_path(self):
        operation = AsyncMock(return_value={'success_count': 1})
        with patch.object(server.cookie_manager, 'manager', None), \
             patch.object(server, '_manual_ship_orders_impl', operation):
            asyncio.run(server.manual_ship_orders(['fixture-order'], 'status_only', None, {'user_id': 1}))
        operation.assert_awaited_once()
