"""
Router Health Monitor — concurrent checks with async pings.
Periodically checks router status and logs health data.
"""

import asyncio
import logging
import uuid
import re
import socket
import platform
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger("netops.monitor")

MAX_CONCURRENT_CHECKS = 50  # Semaphore limit — don't overwhelm the network


# ---------- Async ping utility ----------

async def async_ping(ip: str, timeout: int = 3) -> bool:
    """Ping an IP address asynchronously. Returns True if reachable."""
    try:
        param = "-n" if platform.system().lower() == "windows" else "-c"
        timeout_param = "-w" if platform.system().lower() == "windows" else "-W"
        proc = await asyncio.create_subprocess_exec(
            "ping", param, "1", timeout_param, str(timeout), ip,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=timeout + 2)
        return proc.returncode == 0
    except Exception:
        return False


async def async_ping_latency(ip: str, timeout: int = 3) -> Optional[float]:
    """Ping an IP address asynchronously and return latency in ms, or None."""
    try:
        param = "-n" if platform.system().lower() == "windows" else "-c"
        timeout_param = "-w" if platform.system().lower() == "windows" else "-W"
        proc = await asyncio.create_subprocess_exec(
            "ping", param, "1", timeout_param, str(timeout), ip,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout + 2)
        if proc.returncode != 0:
            return None
        text = stdout.decode("utf-8", errors="replace")
        for line in text.splitlines():
            if "time=" in line.lower():
                m = re.search(r"time[=<](\d+\.?\d*)", line, re.IGNORECASE)
                if m:
                    return float(m.group(1))
        return None
    except Exception:
        return None


def estimate_signal_from_latency(latency_ms: float) -> int:
    """Estimate WiFi signal percentage from ping latency to router LAN IP."""
    if latency_ms < 3:
        return 95
    elif latency_ms < 5:
        return 90
    elif latency_ms < 10:
        return 82
    elif latency_ms < 20:
        return 72
    elif latency_ms < 50:
        return 58
    elif latency_ms < 100:
        return 40
    else:
        return 20


def tcp_check(ip: str, port: int = 80, timeout: int = 3) -> bool:
    """Check if a TCP port is open (router admin page)."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


# ---------- TP-Link health check ----------

def check_tp_link(router_doc: dict) -> dict:
    """Check a TP-Link router's health via its local API."""
    from tplinkrouterc6u import TplinkRouterProvider

    router_ip = router_doc.get("router_ip", "")
    password = router_doc.get("admin_password", "")
    username = router_doc.get("admin_username", "admin")

    # Decrypt password if it's encrypted (Fernet tokens start with "gAAAAA")
    if password and password.startswith("gAAAAA"):
        try:
            from cryptography.fernet import Fernet
            import os
            key = os.environ.get("ENCRYPTION_KEY", "")
            if key:
                password = Fernet(key.encode()).decrypt(password.encode()).decode()
        except Exception:
            pass

    if not router_ip or not password:
        return {
            "status": "unknown", "wan_status": "unknown", "wan_ip": None,
            "signal_strength": None, "connected_devices": None, "internet_uptime": None,
            "cpu_usage": None, "memory_usage": None, "error_message": "Missing router_ip or admin_password",
        }

    if not tcp_check(router_ip, 80, timeout=3):
        return {
            "status": "offline", "wan_status": "unknown", "wan_ip": None,
            "signal_strength": None, "connected_devices": None, "internet_uptime": None,
            "cpu_usage": None, "memory_usage": None, "error_message": "Router unreachable at " + router_ip,
        }

    try:
        client = TplinkRouterProvider.get_client(f"http://{router_ip}", password, username)
        client.authorize()
        try:
            status = client.get_status()
            ipv4_status = client.get_ipv4_status()

            wan_ip = getattr(ipv4_status, "wan_ipaddr", None) or getattr(ipv4_status, "wan_ipv4_addr", None)
            wan_uptime = getattr(ipv4_status, "wan_ipv4_uptime", None) or getattr(ipv4_status, "internet_uptime", None) or 0

            signal = None
            devices_count = 0
            cpu_usage = None
            memory_usage = None

            if hasattr(status, "devices") and status.devices:
                devices_count = len(status.devices)
            if hasattr(status, "wifi_clients_total"):
                devices_count = getattr(status, "clients_total", devices_count) or devices_count
            if hasattr(status, "cpu_usage"):
                cpu_usage = status.cpu_usage
            if hasattr(status, "mem_usage"):
                memory_usage = status.mem_usage

            if hasattr(status, "wifi_2g_clients") and status.wifi_2g_clients:
                signal = 80
            elif hasattr(status, "wifi_5g_clients") and status.wifi_5g_clients:
                signal = 85

            if wan_ip and wan_ip != "0.0.0.0" and wan_ip != "":
                overall_status = "online"
                wan_status = "connected"
            else:
                overall_status = "degraded"
                wan_status = "disconnected"

            return {
                "status": overall_status, "wan_status": wan_status, "wan_ip": wan_ip,
                "signal_strength": signal, "connected_devices": devices_count,
                "internet_uptime": wan_uptime, "cpu_usage": cpu_usage,
                "memory_usage": memory_usage, "error_message": None,
            }
        finally:
            client.logout()

    except Exception as e:
        log.warning(f"TP-Link check failed for {router_ip}: {e}")
        return {
            "status": "unknown", "wan_status": "unknown", "wan_ip": None,
            "signal_strength": None, "connected_devices": None, "internet_uptime": None,
            "cpu_usage": None, "memory_usage": None, "error_message": str(e)[:200],
        }


# ---------- Generic fallback (async ping) ----------

async def check_generic(router_doc: dict) -> dict:
    """Fallback: async ping the router IP and estimate signal from latency."""
    router_ip = router_doc.get("router_ip", "")
    wan_ip = router_doc.get("wan_ip", "")

    if not router_ip:
        return {
            "status": "unknown", "wan_status": "unknown", "wan_ip": None,
            "signal_strength": None, "connected_devices": None, "internet_uptime": None,
            "cpu_usage": None, "memory_usage": None, "error_message": "No router_ip configured",
        }

    latency, internet_reachable = await asyncio.gather(
        async_ping_latency(router_ip),
        async_ping("8.8.8.8"),
    )
    router_reachable = latency is not None

    if not router_reachable:
        return {
            "status": "offline", "wan_status": "unknown", "wan_ip": wan_ip,
            "signal_strength": None, "connected_devices": None, "internet_uptime": None,
            "cpu_usage": None, "memory_usage": None, "error_message": "Router unreachable",
        }

    signal = estimate_signal_from_latency(latency)

    if not internet_reachable:
        return {
            "status": "degraded", "wan_status": "disconnected", "wan_ip": wan_ip,
            "signal_strength": signal, "connected_devices": None, "internet_uptime": None,
            "cpu_usage": None, "memory_usage": None,
            "error_message": "Internet unreachable - possible wire cut or outage",
        }

    return {
        "status": "online", "wan_status": "connected", "wan_ip": wan_ip,
        "signal_strength": signal, "connected_devices": None, "internet_uptime": None,
        "cpu_usage": None, "memory_usage": None, "error_message": None,
    }


# ---------- Main check dispatcher ----------

def check_router_health_sync(router_doc: dict) -> dict:
    """Route to the correct checker based on brand (sync for TP-Link)."""
    brand = (router_doc.get("brand") or "").lower().strip()
    if brand in ("tplink", "tp-link", "tp_link"):
        return check_tp_link(router_doc)
    return None  # generic needs async


async def check_router_health(router_doc: dict) -> dict:
    """Route to the correct checker based on brand (async-capable)."""
    brand = (router_doc.get("brand") or "").lower().strip()
    if brand in ("tplink", "tp-link", "tp_link"):
        return check_tp_link(router_doc)
    return await check_generic(router_doc)


# ---------- Database operations ----------

async def save_health_log(db, router_id: str, result: dict):
    """Save a health check result only when something meaningful changed."""
    now = datetime.now(timezone.utc)
    last_log = await db.router_health.find_one(
        {"router_id": router_id}, sort=[("timestamp", -1)]
    )

    if last_log:
        last_status = last_log.get("status")
        last_signal = last_log.get("signal_strength")
        last_error = last_log.get("error_message")
        last_ts = datetime.fromisoformat(last_log["timestamp"].replace("Z", "+00:00"))
        hours_since = (now - last_ts).total_seconds() / 3600

        new_status = result.get("status", "unknown")
        new_signal = result.get("signal_strength")
        new_error = result.get("error_message")

        status_changed = new_status != last_status
        signal_changed = (
            (new_signal is not None and last_signal is not None and abs(new_signal - last_signal) > 20)
            or ((new_signal is not None) != (last_signal is not None))
        )
        error_changed = new_error != last_error
        is_online = new_status == "online"
        stale_breadcrumb = not is_online and hours_since >= 2

        if not (status_changed or signal_changed or error_changed or stale_breadcrumb):
            return

    doc = {
        "id": str(uuid.uuid4()),
        "router_id": router_id,
        "timestamp": now.isoformat(),
        "status": result.get("status", "unknown"),
        "wan_status": result.get("wan_status", "unknown"),
        "wan_ip": result.get("wan_ip"),
        "signal_strength": result.get("signal_strength"),
        "connected_devices": result.get("connected_devices"),
        "internet_uptime": result.get("internet_uptime"),
        "cpu_usage": result.get("cpu_usage"),
        "memory_usage": result.get("memory_usage"),
        "error_message": result.get("error_message"),
    }
    await db.router_health.insert_one(doc)


async def update_router_snapshot(db, router_id: str, result: dict):
    """Update the router document with latest health snapshot."""
    now = datetime.now(timezone.utc).isoformat()
    update_fields = {
        "health_status": result.get("status", "unknown"),
        "wan_status": result.get("wan_status", "unknown"),
        "signal": result.get("signal_strength"),
        "connected_devices": result.get("connected_devices"),
        "internet_uptime": result.get("internet_uptime"),
        "last_health_check": now,
    }
    if result.get("wan_ip"):
        update_fields["wan_ip"] = result["wan_ip"]
    if result.get("status") == "online":
        update_fields["last_seen_online"] = now

    await db.routers.update_one({"router_id": router_id}, {"$set": update_fields})


async def cleanup_old_health_logs(db):
    """Delete health logs older than 30 days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    result = await db.router_health.delete_many({"timestamp": {"$lt": cutoff}})
    if result.deleted_count > 0:
        log.info(f"Cleaned up {result.deleted_count} health logs older than 30 days")


# ---------- Concurrent health check engine ----------

async def _check_one_router(db, router: dict, semaphore: asyncio.Semaphore):
    """Check a single router (concurrent-safe via semaphore)."""
    async with semaphore:
        router_id = router["router_id"]
        try:
            brand = (router.get("brand") or "").lower().strip()
            if brand in ("tplink", "tp-link", "tp_link"):
                result = check_tp_link(router)
            else:
                result = await check_generic(router)

            await save_health_log(db, router_id, result)
            await update_router_snapshot(db, router_id, result)

            old_status = router.get("health_status", "unknown")
            new_status = result.get("status", "unknown")
            if old_status != new_status and new_status in ("offline", "degraded"):
                if router.get("dealer_id"):
                    from uuid import uuid4
                    await db.notifications.insert_one({
                        "id": str(uuid4()),
                        "user_id": router["dealer_id"],
                        "ticket_id": None,
                        "type": "router_alert",
                        "title": f"Router {new_status.upper()}",
                        "message": f"Router {router_id} ({router.get('client_name', 'Unknown')}) is {new_status}. {result.get('error_message', '')}",
                        "read": False,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    })
                    from backend.server import send_push_notification
                    await send_push_notification(
                        router["dealer_id"],
                        f"Router {new_status.upper()}",
                        f"Router {router_id} ({router.get('client_name', 'Unknown')}) is {new_status}.",
                        _db=db,
                    )

            return True
        except Exception as e:
            log.error(f"Health check error for {router_id}: {e}")
            return False


async def run_health_checks(db):
    """Check all routers concurrently with a semaphore to limit parallelism."""
    routers = await db.routers.find(
        {"router_ip": {"$ne": None, "$ne": ""}},
        {"_id": 0}
    ).to_list(length=2000)

    if not routers:
        log.info("No routers with credentials to check.")
        return

    log.info(f"Running health checks on {len(routers)} routers (max {MAX_CONCURRENT_CHECKS} concurrent)...")
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)

    results = await asyncio.gather(
        *[_check_one_router(db, r, semaphore) for r in routers],
        return_exceptions=True,
    )

    checked = sum(1 for r in results if r is True)
    errors = sum(1 for r in results if r is not True)
    log.info(f"Health checks complete: {checked} checked, {errors} errors")


async def run_pppoe_health_checks(db):
    """Check PPPoE session status for all assigned routers via MikroTik."""
    pppoe_routers = await db.routers.find(
        {"pppoe_username": {"$ne": None, "$ne": ""}},
        {"_id": 0}
    ).to_list(length=2000)

    if not pppoe_routers:
        log.info("No PPPoE-assigned routers to check.")
        return

    # Get MikroTik config
    try:
        import os
        from dotenv import load_dotenv
        from pathlib import Path
        load_dotenv(Path(__file__).parent / ".env")

        from motor.motor_asyncio import AsyncIOMotorClient
        mongo_client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        mongo_db = mongo_client[os.environ["DB_NAME"]]

        config = await mongo_db.mikrotik_config.find_one({}, {"_id": 0})
        if not config:
            log.info("MikroTik not configured, skipping PPPoE checks.")
            return

        from cryptography.fernet import Fernet
        encryption_key = os.environ.get("ENCRYPTION_KEY", "")
        if not encryption_key:
            log.warning("ENCRYPTION_KEY not set, cannot decrypt MikroTik password.")
            return
        fernet = Fernet(encryption_key.encode())
        password = fernet.decrypt(config["password_encrypted"].encode()).decode()

        host = config["host"]
        port = config["port"]
        username = config["username"]
        use_ssl = config.get("use_ssl", True)
    except Exception as e:
        log.error(f"Failed to load MikroTik config for PPPoE checks: {e}")
        return

    # Fetch all active PPPoE sessions
    try:
        from backend.mikrotik import get_pppoe_sessions, get_client_usage
        sessions = await get_pppoe_sessions(host, port, username, password, use_ssl)
        session_map = {s["name"]: s for s in sessions}
        usage_data = await get_client_usage(host, port, username, password, use_ssl)
    except Exception as e:
        log.error(f"Failed to fetch PPPoE sessions from MikroTik: {e}")
        session_map = {}
        usage_data = {}

    now = datetime.now(timezone.utc)
    checked = 0
    status_changes = 0

    for router in pppoe_routers:
        pppoe_name = router.get("pppoe_username")
        router_id = router.get("router_id")

        if not pppoe_name:
            continue

        is_online = pppoe_name in session_map
        session = session_map.get(pppoe_name, {})
        old_status = router.get("health_status") or router.get("status", "unknown")
        new_status = "online" if is_online else "offline"

        # Update usage data
        if pppoe_name in usage_data:
            usage = usage_data[pppoe_name]
            usage_in = usage.get("bytes_in", 0)
            usage_out = usage.get("bytes_out", 0)
            uptime = usage.get("uptime", "0s")
        else:
            usage_in = session.get("bytes_in", 0) if session else 0
            usage_out = session.get("bytes_out", 0) if session else 0
            uptime = session.get("uptime", "0s") if session else "0s"

        update_fields = {
            "health_status": new_status,
            "status": new_status,
            "pppoe_ip": session.get("address") if session else router.get("pppoe_ip"),
            "pppoe_uptime": uptime,
            "usage_in": usage_in,
            "usage_out": usage_out,
            "last_check": now.isoformat(),
        }
        if is_online:
            update_fields["last_seen_online"] = now.isoformat()

        await mongo_db.routers.update_one({"router_id": router_id}, {"$set": update_fields})

        # Save health log
        health_doc = {
            "id": str(uuid.uuid4()),
            "router_id": router_id,
            "timestamp": now.isoformat(),
            "status": new_status,
            "wan_status": "connected" if is_online else "disconnected",
            "wan_ip": session.get("address") if session else None,
            "signal_strength": None,
            "connected_devices": None,
            "internet_uptime": uptime,
            "usage_in": usage_in,
            "usage_out": usage_out,
            "error_message": None if is_online else "PPPoE session not found",
        }
        await mongo_db.router_health.insert_one(health_doc)

        # Notify on status change
        if old_status != new_status and new_status in ("offline",):
            if router.get("dealer_id"):
                await mongo_db.notifications.insert_one({
                    "id": str(uuid.uuid4()),
                    "user_id": router["dealer_id"],
                    "ticket_id": None,
                    "type": "router_alert",
                    "title": f"Router OFFLINE",
                    "message": f"PPPoE user {pppoe_name} ({router.get('client_name', 'Unknown')}) went offline. Possible wire cut.",
                    "read": False,
                    "created_at": now.isoformat(),
                })
                from backend.server import send_push_notification
                await send_push_notification(
                    router["dealer_id"],
                    "Router OFFLINE",
                    f"PPPoE user {pppoe_name} ({router.get('client_name', 'Unknown')}) went offline. Possible wire cut.",
                    _db=mongo_db,
                )
            if router.get("user_id"):
                await mongo_db.notifications.insert_one({
                    "id": str(uuid.uuid4()),
                    "user_id": router["user_id"],
                    "ticket_id": None,
                    "type": "connection_offline",
                    "title": "Connection Offline",
                    "message": f"Your internet connection ({pppoe_name}) is offline. If this persists, report an issue.",
                    "read": False,
                    "created_at": now.isoformat(),
                })
                from backend.server import send_push_notification as _spn
                await _spn(
                    router["user_id"],
                    "Connection Offline",
                    f"Your internet connection ({pppoe_name}) is offline. If this persists, report an issue.",
                    _db=mongo_db,
                )
            status_changes += 1

        checked += 1

    mongo_client.close()
    log.info(f"PPPoE health checks complete: {checked} checked, {status_changes} status changes")


async def check_single_router(db, router_id: str) -> dict:
    """Check a single router on demand."""
    router = await db.routers.find_one({"router_id": router_id}, {"_id": 0})
    if not router:
        return {"error": "Router not found"}
    if not router.get("router_ip"):
        return {"error": "Router IP not configured. Register router details first."}

    brand = (router.get("brand") or "").lower().strip()
    if brand in ("tplink", "tp-link", "tp_link"):
        result = check_tp_link(router)
    else:
        result = await check_generic(router)

    await save_health_log(db, router_id, result)
    await update_router_snapshot(db, router_id, result)
    return result


# ---------- Scheduler ----------

_health_task = None


async def consolidate_daily_reports(db):
    """Aggregate yesterday's health logs into daily_reports collection."""
    yesterday_start = (datetime.now(timezone.utc) - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_end = yesterday_start + timedelta(days=1)

    pipeline = [
        {"$match": {"timestamp": {"$gte": yesterday_start.isoformat(), "$lt": yesterday_end.isoformat()}}},
        {"$group": {
            "_id": "$router_id",
            "checks": {"$sum": 1},
            "up_count": {"$sum": {"$cond": [{"$eq": ["$status", "online"]}, 1, 0]}},
            "down_count": {"$sum": {"$cond": [{"$eq": ["$status", "offline"]}, 1, 0]}},
            "avg_latency": {"$avg": "$latency_ms"},
            "min_latency": {"$min": "$latency_ms"},
            "max_latency": {"$max": "$latency_ms"},
        }},
    ]

    results = await db.router_health.aggregate(pipeline).to_list(length=500)
    date_str = yesterday_start.strftime("%Y-%m-%d")

    for r in results:
        report = {
            "router_id": r["_id"],
            "date": date_str,
            "total_checks": r["checks"],
            "up_count": r["up_count"],
            "down_count": r["down_count"],
            "uptime_pct": round(r["up_count"] / r["checks"] * 100, 2) if r["checks"] else 0,
            "avg_latency_ms": round(r["avg_latency"], 2) if r["avg_latency"] else None,
            "min_latency_ms": r["min_latency"],
            "max_latency_ms": r["max_latency"],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        await db.daily_reports.update_one(
            {"router_id": r["_id"], "date": date_str},
            {"$set": report},
            upsert=True,
        )

    log.info(f"Daily reports consolidated for {date_str}: {len(results)} routers")


async def health_check_scheduler(db):
    """Background task: run health checks every 10 minutes, cleanup and report daily."""
    global _health_task
    log.info("Health check scheduler started (interval: 600s)")
    cleanup_hour = 0
    report_hour = 1  # generate daily report at 01:00 UTC
    while True:
        await asyncio.sleep(600)
        try:
            await run_health_checks(db)
            await run_pppoe_health_checks(db)
            current_hour = datetime.now(timezone.utc).hour
            if current_hour == cleanup_hour:
                await cleanup_old_health_logs(db)
            if current_hour == report_hour:
                await consolidate_daily_reports(db)
        except Exception as e:
            log.error(f"Health check scheduler error: {e}")


def start_health_scheduler(db):
    """Start the background health check scheduler."""
    global _health_task
    _health_task = asyncio.create_task(_run_startup_and_schedule(db))
    log.info("Health check background task created")


async def _run_startup_and_schedule(db):
    """Run initial cleanup then start the recurring scheduler."""
    try:
        await cleanup_old_health_logs(db)
    except Exception as e:
        log.error(f"Startup cleanup error: {e}")
    await health_check_scheduler(db)


def stop_health_scheduler():
    """Stop the background health check scheduler."""
    global _health_task
    if _health_task:
        _health_task.cancel()
        log.info("Health check scheduler stopped")
