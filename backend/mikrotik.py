"""
MikroTik RouterOS API wrapper.
Connects to MikroTik CCR via RouterOS API to fetch PPPoE sessions,
secrets, profiles, queues, and interface stats.
"""

import logging
import asyncio

log = logging.getLogger("netops.mikrotik")

# Default timeout for API calls
DEFAULT_TIMEOUT = 10


def _run_sync(func, *args, **kwargs):
    """Run a blocking RouterOS API call in a thread executor."""
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(None, lambda: func(*args, **kwargs))


def _connect(host: str, port: int, username: str, password: str, use_ssl: bool = True):
    """Create a RouterOS API connection. Returns the connection object."""
    import routeros_api
    proto = "api-ssl" if use_ssl else "api"
    connection = routeros_api.RouterOsApiPool(
        host=host,
        port=port,
        username=username,
        password=password,
        plaintext_login=True,
        timeout=DEFAULT_TIMEOUT,
        ssl_certificate_verify=False,
    )
    return connection


# ---------- Public async functions ----------

async def test_connection(host: str, port: int, username: str, password: str, use_ssl: bool = True) -> dict:
    """Test connection to MikroTik router. Returns status info."""
    try:
        def _do():
            conn = _connect(host, port, username, password, use_ssl)
            api = conn.get_api()
            identity = api.get_resource("/system/identity").get()[0]
            resource = api.get_resource("/system/resource").get()[0]
            conn.disconnect()
            return {
                "connected": True,
                "router_name": identity.get("name", "Unknown"),
                "version": resource.get("version", "Unknown"),
                "uptime": resource.get("uptime", "Unknown"),
                "cpu_count": resource.get("cpu-count", "Unknown"),
                "total_memory": resource.get("total-memory", "Unknown"),
                "free_memory": resource.get("free-memory", "Unknown"),
            }
        return await _run_sync(_do)
    except Exception as e:
        log.warning(f"MikroTik connection test failed for {host}: {e}")
        return {"connected": False, "error": str(e)[:200]}


async def get_pppoe_sessions(host: str, port: int, username: str, password: str, use_ssl: bool = True) -> list:
    """Get all active PPPoE sessions."""
    try:
        def _do():
            conn = _connect(host, port, username, password, use_ssl)
            api = conn.get_api()
            resource = api.get_resource("/ppp/active")
            sessions = resource.get()
            conn.disconnect()
            return sessions
        raw = await _run_sync(_do)
        return [_parse_pppoe_session(s) for s in raw]
    except Exception as e:
        log.warning(f"Failed to get PPPoE sessions from {host}: {e}")
        return []


async def get_pppoe_secrets(host: str, port: int, username: str, password: str, use_ssl: bool = True) -> list:
    """Get all configured PPPoE secrets (users)."""
    try:
        def _do():
            conn = _connect(host, port, username, password, use_ssl)
            api = conn.get_api()
            resource = api.get_resource("/ppp/secret")
            secrets = resource.get()
            conn.disconnect()
            return secrets
        raw = await _run_sync(_do)
        return [_parse_pppoe_secret(s) for s in raw]
    except Exception as e:
        log.warning(f"Failed to get PPPoE secrets from {host}: {e}")
        return []


async def get_profiles(host: str, port: int, username: str, password: str, use_ssl: bool = True) -> list:
    """Get all PPPoE profiles."""
    try:
        def _do():
            conn = _connect(host, port, username, password, use_ssl)
            api = conn.get_api()
            resource = api.get_resource("/ppp/profile")
            profiles = resource.get()
            conn.disconnect()
            return profiles
        raw = await _run_sync(_do)
        return [_parse_profile(p) for p in raw]
    except Exception as e:
        log.warning(f"Failed to get profiles from {host}: {e}")
        return []


async def get_interfaces(host: str, port: int, username: str, password: str, use_ssl: bool = True) -> list:
    """Get all interfaces with traffic stats."""
    try:
        def _do():
            conn = _connect(host, port, username, password, use_ssl)
            api = conn.get_api()
            resource = api.get_resource("/interface")
            interfaces = resource.get()
            conn.disconnect()
            return interfaces
        raw = await _run_sync(_do)
        return [_parse_interface(i) for i in raw]
    except Exception as e:
        log.warning(f"Failed to get interfaces from {host}: {e}")
        return []


async def get_dhcp_leases(host: str, port: int, username: str, password: str, use_ssl: bool = True) -> list:
    """Get DHCP lease table."""
    try:
        def _do():
            conn = _connect(host, port, username, password, use_ssl)
            api = conn.get_api()
            resource = api.get_resource("/ip/dhcp-server/lease")
            leases = resource.get()
            conn.disconnect()
            return leases
        raw = await _run_sync(_do)
        return [_parse_lease(l) for l in raw]
    except Exception as e:
        log.warning(f"Failed to get DHCP leases from {host}: {e}")
        return []


async def get_simple_queues(host: str, port: int, username: str, password: str, use_ssl: bool = True) -> list:
    """Get simple queues (bandwidth limits)."""
    try:
        def _do():
            conn = _connect(host, port, username, password, use_ssl)
            api = conn.get_api()
            resource = api.get_resource("/queue/simple")
            queues = resource.get()
            conn.disconnect()
            return queues
        raw = await _run_sync(_do)
        return [_parse_queue(q) for q in raw]
    except Exception as e:
        log.warning(f"Failed to get queues from {host}: {e}")
        return []


async def disconnect_client(host: str, port: int, username: str, password: str,
                            session_id: str, use_ssl: bool = True) -> dict:
    """Disconnect an active PPPoE session by .id."""
    try:
        def _do():
            conn = _connect(host, port, username, password, use_ssl)
            api = conn.get_api()
            resource = api.get_resource("/ppp/active")
            resource.remove(session_id)
            conn.disconnect()
            return {"success": True, "message": f"Session {session_id} disconnected"}
        return await _run_sync(_do)
    except Exception as e:
        log.warning(f"Failed to disconnect session {session_id} on {host}: {e}")
        return {"success": False, "error": str(e)[:200]}


async def enable_disable_client(host: str, port: int, username: str, password: str,
                                secret_id: str, disabled: bool, use_ssl: bool = True) -> dict:
    """Enable or disable a PPPoE secret (user)."""
    try:
        def _do():
            conn = _connect(host, port, username, password, use_ssl)
            api = conn.get_api()
            resource = api.get_resource("/ppp/secret")
            resource.set(secret_id, disabled="true" if disabled else "false")
            conn.disconnect()
            action = "disabled" if disabled else "enabled"
            return {"success": True, "message": f"User {secret_id} {action}"}
        return await _run_sync(_do)
    except Exception as e:
        log.warning(f"Failed to toggle user {secret_id} on {host}: {e}")
        return {"success": False, "error": str(e)[:200]}


async def change_client_profile(host: str, port: int, username: str, password: str,
                                secret_id: str, new_profile: str, use_ssl: bool = True) -> dict:
    """Change the PPPoE profile for a secret (user)."""
    try:
        def _do():
            conn = _connect(host, port, username, password, use_ssl)
            api = conn.get_api()
            resource = api.get_resource("/ppp/secret")
            resource.set(secret_id, profile=new_profile)
            conn.disconnect()
            return {"success": True, "message": f"Profile changed to {new_profile} for user {secret_id}"}
        return await _run_sync(_do)
    except Exception as e:
        log.warning(f"Failed to change profile for {secret_id} on {host}: {e}")
        return {"success": False, "error": str(e)[:200]}


async def get_client_usage(host: str, port: int, username: str, password: str,
                           use_ssl: bool = True) -> dict:
    """Get traffic stats per active PPPoE session."""
    try:
        def _do():
            conn = _connect(host, port, username, password, use_ssl)
            api = conn.get_api()
            resource = api.get_resource("/ppp/active")
            sessions = resource.get()
            conn.disconnect()
            result = {}
            for s in sessions:
                name = s.get("name", "unknown")
                result[name] = {
                    "bytes_in": _parse_bytes(s.get("bytes-in", "0")),
                    "bytes_out": _parse_bytes(s.get("bytes-out", "0")),
                    "uptime": s.get("uptime", "0s"),
                    "rate_limit": s.get("rate-limit", ""),
                }
            return result
        return await _run_sync(_do)
    except Exception as e:
        log.warning(f"Failed to get usage from {host}: {e}")
        return {}


# ---------- Parsers ----------

def _parse_pppoe_session(s: dict) -> dict:
    return {
        "id": s.get(".id", ""),
        "name": s.get("name", ""),
        "service": s.get("service", ""),
        "caller_id": s.get("caller-id", ""),
        "address": s.get("address", ""),
        "uptime": s.get("uptime", ""),
        "bytes_in": _parse_bytes(s.get("bytes-in", "0")),
        "bytes_out": _parse_bytes(s.get("bytes-out", "0")),
        "encoding": s.get("encoding", ""),
        "last_called_id": s.get("last-called-id", ""),
    }


def _parse_pppoe_secret(s: dict) -> dict:
    return {
        "id": s.get(".id", ""),
        "name": s.get("name", ""),
        "service": s.get("service", ""),
        "password": "***",  # Never expose password
        "profile": s.get("profile", "default"),
        "local_address": s.get("local-address", ""),
        "remote_address": s.get("remote-address", ""),
        "disabled": s.get("disabled", "false") == "true",
        "comment": s.get("comment", ""),
    }


def _parse_profile(p: dict) -> dict:
    return {
        "id": p.get(".id", ""),
        "name": p.get("name", ""),
        "local_address": p.get("local-address", ""),
        "remote_address": p.get("remote-address", ""),
        "rate_limit": p.get("rate-limit", ""),
        "rate_limit_rx": p.get("rate-limit-rx", ""),
        "rate_limit_tx": p.get("rate-limit-tx", ""),
        "session_timeout": p.get("session-timeout", ""),
        "idle_timeout": p.get("idle-timeout", ""),
        "dns_server": p.get("dns-server", ""),
    }


def _parse_interface(i: dict) -> dict:
    return {
        "id": i.get(".id", ""),
        "name": i.get("name", ""),
        "type": i.get("type", ""),
        "mac_address": i.get("mac-address", ""),
        "running": i.get("running", "false") == "true",
        "disabled": i.get("disabled", "false") == "true",
        "rx_rate": i.get("rx-rate", ""),
        "tx_rate": i.get("tx-rate", ""),
        "rx_bytes": _parse_bytes(i.get("rx-byte", "0")),
        "tx_bytes": _parse_bytes(i.get("tx-byte", "0")),
        "link_downs": i.get("link-downs", "0"),
    }


def _parse_lease(l: dict) -> dict:
    return {
        "id": l.get(".id", ""),
        "address": l.get("address", ""),
        "mac_address": l.get("mac-address", ""),
        "duid": l.get("duid", ""),
        "active_address": l.get("active-address", ""),
        "active_mac_address": l.get("active-mac-address", ""),
        "host_name": l.get("host-name", ""),
        "status": l.get("status", ""),
        "expires": l.get("expires", ""),
    }


def _parse_queue(q: dict) -> dict:
    return {
        "id": q.get(".id", ""),
        "name": q.get("name", ""),
        "target": q.get("target", ""),
        "max_limit": q.get("max-limit", ""),
        "min_limit": q.get("min-limit", ""),
        "burst_limit": q.get("burst-limit", ""),
        "burst_threshold": q.get("burst-threshold", ""),
        "burst_time": q.get("burst-time", ""),
        "bytes_in": _parse_bytes(q.get("byte", "0").split(",")[0] if q.get("byte") else "0"),
        "bytes_out": _parse_bytes(q.get("byte", "0").split(",")[1] if q.get("byte") and "," in q.get("byte", "") else "0"),
        "rate": q.get("rate", ""),
        "packet_rate": q.get("packet-rate", ""),
        "comment": q.get("comment", ""),
    }


def _parse_bytes(val: str) -> int:
    """Parse byte string like '50M', '1G', '1234' to integer."""
    if not val or val == "":
        return 0
    val = val.strip().lower()
    multipliers = {"k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}
    for suffix, mult in multipliers.items():
        if val.endswith(suffix):
            try:
                return int(float(val[:-1]) * mult)
            except ValueError:
                return 0
    try:
        return int(val)
    except ValueError:
        return 0
