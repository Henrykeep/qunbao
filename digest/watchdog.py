"""事件循环看门狗：事件循环卡死（healthz 也答不了）超过 STALL_S 秒就退出进程，
让 docker 的 restart: always 把服务拉起来（healthcheck 只会标 unhealthy，不会重启）。"""
import asyncio, logging, os, threading, time
from envutil import env_int

_log = logging.getLogger('qunbao')

STALL_S = env_int("WATCHDOG_STALL_S", 180)   # 0 = 关闭
_beat = time.monotonic()


def lag_s() -> float:
    """心跳距今多少秒；正常 ≤5，持续变大说明事件循环被卡住。"""
    return round(time.monotonic() - _beat, 1)


def stalled(last: float, now: float, limit: int = STALL_S) -> bool:
    return limit > 0 and now - last > limit


async def heartbeat():
    global _beat
    while True:
        _beat = time.monotonic()
        await asyncio.sleep(5)


def start():
    """在事件循环里调用：起心跳协程 + 守护线程；返回心跳 task。"""
    if STALL_S <= 0:
        return None
    t = asyncio.get_running_loop().create_task(heartbeat())

    def guard():
        while True:
            time.sleep(10)
            if stalled(_beat, time.monotonic()):
                _log.critical("watchdog: 事件循环停滞超过 %ss，退出以便 docker 重启", STALL_S)
                logging.shutdown()
                os._exit(1)
    threading.Thread(target=guard, daemon=True).start()
    return t
