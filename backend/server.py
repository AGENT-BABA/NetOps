from dotenv import load_dotenv
from pathlib import Path

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

import os
import logging
import random
import uuid
import math
import time
import bcrypt
import jwt
from datetime import datetime, timezone, timedelta
from typing import Optional, Literal
from fastapi import FastAPI, APIRouter, Depends, HTTPException, Request, Query
from fastapi.responses import StreamingResponse
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field, EmailStr
import asyncio
import io
from backend.router_monitor import (
    check_router_health, save_health_log, update_router_snapshot,
    run_health_checks, check_single_router, start_health_scheduler,
)
from backend.mikrotik import (
    test_connection, get_pppoe_sessions, get_pppoe_secrets,
    get_profiles, get_interfaces, get_simple_queues,
    disconnect_client, enable_disable_client, change_client_profile,
    get_client_usage,
)

# ---------------- Config ----------------
JWT_SECRET = os.environ.get("JWT_SECRET")
if not JWT_SECRET or JWT_SECRET == "dev-secret":
    raise RuntimeError("JWT_SECRET must be set to a secure value in .env")
JWT_ALG = "HS256"
JWT_EXPIRE_MIN = 60 * 24

client = AsyncIOMotorClient(os.environ["MONGO_URL"])
db = client[os.environ["DB_NAME"]]

# ---------------- Encryption ----------------
ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY", "")
_fernet = None

def _get_fernet():
    global _fernet
    if _fernet is None:
        if not ENCRYPTION_KEY:
            raise RuntimeError("ENCRYPTION_KEY must be set in .env")
        from cryptography.fernet import Fernet
        _fernet = Fernet(ENCRYPTION_KEY.encode())
    return _fernet

def encrypt_secret(plain: str) -> str:
    return _get_fernet().encrypt(plain.encode()).decode()

def decrypt_secret(cipher: str) -> str:
    return _get_fernet().decrypt(cipher.encode()).decode()

# ---------------- Audit Log ----------------
async def log_action(user: dict, action: str, detail: str = "", request: Request = None, success: bool = True):
    doc = {
        "id": str(uuid.uuid4()),
        "user_id": user.get("id", ""),
        "user_name": user.get("name", user.get("email", "unknown")),
        "user_role": user.get("role", ""),
        "action": action,
        "detail": detail,
        "ip_address": request.client.host if request else "",
        "success": success,
        "timestamp": now_iso(),
    }
    await db.audit_logs.insert_one(doc)

app = FastAPI(title="NetOps Dealer Portal v2")
api = APIRouter(prefix="/api")
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("netops")

# ---------------- Rate Limiter ----------------
_rate_store: dict[str, list[float]] = {}
RATE_WINDOW = 60  # seconds
RATE_MAX_LOGIN = 5
RATE_MAX_REGISTER = 3


def _check_rate(key: str, limit: int):
    now = time.time()
    timestamps = _rate_store.get(key, [])
    timestamps = [t for t in timestamps if now - t < RATE_WINDOW]
    if len(timestamps) >= limit:
        raise HTTPException(429, "Too many requests. Please try again later.")
    timestamps.append(now)
    _rate_store[key] = timestamps


# ---------------- Helpers ----------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_pw(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def verify_pw(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except Exception:
        return False


def make_token(user_id: str, role: str) -> str:
    return jwt.encode(
        {
            "sub": user_id,
            "role": role,
            "exp": datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MIN),
            "iat": datetime.now(timezone.utc),
        },
        JWT_SECRET,
        algorithm=JWT_ALG,
    )


def scrub(doc: dict) -> dict:
    if not doc:
        return doc
    d = dict(doc)
    d.pop("_id", None)
    d.pop("password_hash", None)
    return d


def haversine_km(lat1, lng1, lat2, lng2) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


# ---------------- Models ----------------
class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6)
    name: str
    phone: str
    address: Optional[str] = ""
    role: Literal["user", "worker"] = "user"
    dealer_code: Optional[str] = None  # required for workers, not for clients
    lat: Optional[float] = None
    lng: Optional[float] = None


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class CreateWorkerIn(BaseModel):
    name: str
    email: EmailStr
    password: str = Field(min_length=6)
    phone: str
    area: str
    address: Optional[str] = ""
    lat: float
    lng: float


class CreateClientIn(BaseModel):
    name: str
    email: EmailStr
    password: str = Field(min_length=6)
    phone: str
    address: str
    lat: float
    lng: float


class CreateDealerIn(BaseModel):
    name: str
    email: EmailStr
    password: str = Field(min_length=6)
    phone: str
    city: str
    address: Optional[str] = ""
    lat: float
    lng: float
    dealer_code: str


class ReportIssueIn(BaseModel):
    router_id: str
    issue_type: Literal["wire_cut", "no_signal", "slow_speed", "outage", "other"] = "other"
    description: str = ""


class AssignTicketIn(BaseModel):
    worker_id: str


class TicketStatusIn(BaseModel):
    status: Literal["open", "assigned", "in_progress", "resolved"]
    note: Optional[str] = None


class FeedbackIn(BaseModel):
    working: bool
    reason: Optional[str] = ""


class TicketDeleteIn(BaseModel):
    reason: str = Field(min_length=1)


class AssignClientIn(BaseModel):
    dealer_id: str


class AssignRouterIn(BaseModel):
    pppoe_username: str


class RegisterRouterIn(BaseModel):
    router_id: str  # existing router_id like "RTR-XXXXXXXX"
    brand: str = "tplink"  # tp-link | netgear | dlink | asus | xiaomi | mikrotik | other
    model: Optional[str] = ""
    router_ip: str  # LAN admin IP e.g. "192.168.0.1"
    wan_ip: Optional[str] = ""  # public IP (optional, can be auto-detected)
    mac_address: Optional[str] = ""
    admin_username: str = "admin"
    admin_password: str  # router admin password
    serial_number: Optional[str] = ""


class CreateRouterIn(BaseModel):
    router_id: Optional[str] = None  # auto-generated if empty
    brand: str = "tplink"
    model: Optional[str] = ""
    router_ip: str  # LAN admin IP e.g. "192.168.0.1"
    wan_ip: Optional[str] = ""
    mac_address: Optional[str] = ""
    admin_username: str = "admin"
    admin_password: str
    serial_number: Optional[str] = ""
    location: str = ""
    lat: Optional[float] = None
    lng: Optional[float] = None


class MikroTikConfigIn(BaseModel):
    host: str  # Router IP e.g. "192.168.88.1"
    port: int = 8729  # API-SSL port
    username: str  # Dedicated API user (not admin)
    password: str
    use_ssl: bool = True


# ---------------- Auth ----------------
async def current_user(request: Request) -> dict:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Not authenticated")
    try:
        payload = jwt.decode(auth[7:], JWT_SECRET, algorithms=[JWT_ALG])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")
    user = await db.users.find_one({"id": payload["sub"]})
    if not user:
        raise HTTPException(401, "User not found")
    return user


def require_role(*roles):
    async def _dep(user: dict = Depends(current_user)):
        if user.get("role") not in roles:
            raise HTTPException(403, f"Requires role: {', '.join(roles)}")
        return user
    return _dep


# ---------------- Auth Endpoints ----------------
@api.post("/auth/login")
async def login(body: LoginIn, request: Request):
    _check_rate(f"login:{request.client.host}", RATE_MAX_LOGIN)
    email = body.email.lower().strip()
    user = await db.users.find_one({"email": email})
    if not user or not verify_pw(body.password, user["password_hash"]):
        raise HTTPException(401, "Invalid credentials")
    if not user.get("active", True):
        raise HTTPException(403, "Account deactivated")
    return {"token": make_token(user["id"], user["role"]), "user": scrub(user)}


@api.post("/auth/register")
async def register(body: RegisterIn, request: Request):
    """Public — CLIENTS and WORKERS. Workers require a valid dealer_code."""
    _check_rate(f"register:{request.client.host}", RATE_MAX_REGISTER)
    email = body.email.lower().strip()
    if await db.users.find_one({"email": email}):
        raise HTTPException(400, "Email already registered")

    dealer = None
    if body.dealer_code:
        dealer = await db.users.find_one({"role": "dealer", "dealer_code": body.dealer_code.upper()})
        if not dealer:
            raise HTTPException(400, "Invalid dealer code")

    role = body.role
    if role == "worker" and not dealer:
        raise HTTPException(400, "Dealer code is required for workers")

    uid = str(uuid.uuid4())
    doc = {
        "id": uid, "email": email, "password_hash": hash_pw(body.password),
        "name": body.name, "role": role, "phone": body.phone, "address": body.address,
        "dealer_id": dealer["id"] if dealer else None,
        "lat": body.lat, "lng": body.lng,
        "active": True, "created_at": now_iso(),
    }
    if role == "worker":
        doc["area"] = body.address or ""

    await db.users.insert_one(doc)

    return {"token": make_token(uid, role), "user": scrub(doc)}


@api.get("/auth/me")
async def me(user: dict = Depends(current_user)):
    return scrub(user)


# ---------------- Admin (System-wide) ----------------
@api.get("/admin/audit-logs")
async def admin_audit_logs(limit: int = Query(50, ge=1, le=200), _: dict = Depends(require_role("admin"))):
    """Get recent audit logs."""
    logs = await db.audit_logs.find({}, {"_id": 0}).sort("timestamp", -1).limit(limit).to_list(length=limit)
    return logs


@api.get("/admin/daily-reports")
async def admin_daily_reports(
    router_id: Optional[str] = Query(None),
    days: int = Query(7, ge=1, le=30),
    _: dict = Depends(require_role("admin")),
):
    """Get daily health reports. Optional filter by router_id."""
    q = {}
    if router_id:
        q["router_id"] = router_id
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    q["date"] = {"$gte": cutoff}
    reports = await db.daily_reports.find(q, {"_id": 0}).sort("date", -1).limit(200).to_list(length=200)
    return reports


@api.get("/admin/daily-reports/{date}")
async def admin_daily_report_by_date(date: str, _: dict = Depends(require_role("admin"))):
    """Get all router reports for a specific date (YYYY-MM-DD)."""
    reports = await db.daily_reports.find({"date": date}, {"_id": 0}).to_list(length=200)
    return reports


# ---------------- Monthly Reports ----------------
def _month_range(month: str):
    """Return (start, end) date strings for a YYYY-MM month."""
    year, mon = map(int, month.split("-"))
    start = f"{year:04d}-{mon:02d}-01"
    if mon == 12:
        end = f"{year + 1:04d}-01-01"
    else:
        end = f"{year:04d}-{mon + 1:02d}-01"
    return start, end


@api.get("/admin/reports/monthly")
async def admin_monthly_summary(month: str = Query(..., description="YYYY-MM"), _: dict = Depends(require_role("admin"))):
    """Aggregated monthly health summary per router."""
    start, end = _month_range(month)
    q = {"date": {"$gte": start, "$lt": end}}
    reports = await db.daily_reports.find(q, {"_id": 0}).to_list(length=1000)
    if not reports:
        return {"month": month, "routers": [], "totals": {}}

    by_router = {}
    for r in reports:
        rid = r["router_id"]
        if rid not in by_router:
            by_router[rid] = {"router_id": rid, "days": 0, "total_checks": 0, "up_count": 0, "down_count": 0, "latencies": []}
        br = by_router[rid]
        br["days"] += 1
        br["total_checks"] += r.get("total_checks", 0)
        br["up_count"] += r.get("up_count", 0)
        br["down_count"] += r.get("down_count", 0)
        if r.get("avg_latency_ms") is not None:
            br["latencies"].append(r["avg_latency_ms"])

    routers = []
    total_checks = total_up = total_down = 0
    for br in by_router.values():
        uptime = round(br["up_count"] / br["total_checks"] * 100, 2) if br["total_checks"] else 0
        avg_lat = round(sum(br["latencies"]) / len(br["latencies"]), 2) if br["latencies"] else None
        routers.append({
            "router_id": br["router_id"],
            "days": br["days"],
            "total_checks": br["total_checks"],
            "up_count": br["up_count"],
            "down_count": br["down_count"],
            "uptime_pct": uptime,
            "avg_latency_ms": avg_lat,
        })
        total_checks += br["total_checks"]
        total_up += br["up_count"]
        total_down += br["down_count"]

    return {
        "month": month,
        "routers": routers,
        "totals": {
            "total_checks": total_checks,
            "up_count": total_up,
            "down_count": total_down,
            "uptime_pct": round(total_up / total_checks * 100, 2) if total_checks else 0,
        },
    }


@api.get("/admin/reports/monthly/pdf")
async def admin_monthly_pdf(month: str = Query(..., description="YYYY-MM"), _: dict = Depends(require_role("admin"))):
    """Generate and download a monthly health report PDF."""
    from io import BytesIO
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

    start, end = _month_range(month)
    q = {"date": {"$gte": start, "$lt": end}}
    reports = await db.daily_reports.find(q, {"_id": 0}).sort("date", 1).to_list(length=1000)

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=20 * mm, bottomMargin=20 * mm)
    styles = getSampleStyleSheet()
    elements = []

    title_style = ParagraphStyle("Title2", parent=styles["Title"], fontSize=18, spaceAfter=6)
    subtitle_style = ParagraphStyle("Sub", parent=styles["Normal"], fontSize=10, textColor=colors.grey, spaceAfter=12)
    elements.append(Paragraph(f"NetOps — Monthly Health Report", title_style))
    elements.append(Paragraph(f"Period: {month} &nbsp;|&nbsp; Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", subtitle_style))

    if not reports:
        elements.append(Paragraph("No data available for this month.", styles["Normal"]))
    else:
        # Build per-router summary
        by_router = {}
        for r in reports:
            rid = r["router_id"]
            if rid not in by_router:
                by_router[rid] = {"days": 0, "checks": 0, "up": 0, "down": 0, "lats": []}
            br = by_router[rid]
            br["days"] += 1
            br["checks"] += r.get("total_checks", 0)
            br["up"] += r.get("up_count", 0)
            br["down"] += r.get("down_count", 0)
            if r.get("avg_latency_ms") is not None:
                br["lats"].append(r["avg_latency_ms"])

        # Summary table
        elements.append(Paragraph("Router Summary", styles["Heading2"]))
        sum_data = [["Router ID", "Days", "Checks", "Up", "Down", "Uptime %", "Avg Latency"]]
        for rid, br in sorted(by_router.items()):
            uptime = f"{br['up'] / br['checks'] * 100:.1f}%" if br["checks"] else "—"
            avg_lat = f"{sum(br['lats']) / len(br['lats']):.1f}ms" if br["lats"] else "—"
            sum_data.append([rid, str(br["days"]), str(br["checks"]), str(br["up"]), str(br["down"]), uptime, avg_lat])

        t = Table(sum_data, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a1a2e")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5f5")]),
            ("ALIGN", (1, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        elements.append(t)
        elements.append(Spacer(1, 16))

        # Daily breakdown per router
        for rid in sorted(by_router.keys()):
            elements.append(Paragraph(f"Daily Breakdown — {rid}", styles["Heading2"]))
            daily = [r for r in reports if r["router_id"] == rid]
            tbl_data = [["Date", "Checks", "Up", "Down", "Uptime %", "Avg Lat", "Min Lat", "Max Lat"]]
            for r in daily:
                tbl_data.append([
                    r["date"],
                    str(r.get("total_checks", 0)),
                    str(r.get("up_count", 0)),
                    str(r.get("down_count", 0)),
                    f"{r.get('uptime_pct', 0)}%",
                    f"{r.get('avg_latency_ms', '—')}" + ("ms" if r.get("avg_latency_ms") else ""),
                    f"{r.get('min_latency_ms', '—')}" + ("ms" if r.get("min_latency_ms") else ""),
                    f"{r.get('max_latency_ms', '—')}" + ("ms" if r.get("max_latency_ms") else ""),
                ])
            dt = Table(tbl_data, repeatRows=1)
            dt.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a1a2e")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTSIZE", (0, 0), (-1, -1), 7),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5f5")]),
                ("ALIGN", (1, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]))
            elements.append(dt)
            elements.append(Spacer(1, 12))

    doc.build(elements)
    buf.seek(0)
    filename = f"netops-health-report-{month}.pdf"
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@api.delete("/admin/reports/monthly")
async def admin_delete_monthly_reports(month: str = Query(..., description="YYYY-MM"), request: Request = None, user: dict = Depends(require_role("admin"))):
    """Delete daily reports and health logs for a given month."""
    start, end = _month_range(month)
    dr = await db.daily_reports.delete_many({"date": {"$gte": start, "$lt": end}})
    hl = await db.router_health.delete_many({"timestamp": {"$gte": f"{start}T00:00:00", "$lt": f"{end}T00:00:00"}})
    await log_action(user, "delete_monthly_reports", f"Month: {month}, Reports: {dr.deleted_count}, Logs: {hl.deleted_count}", request, True)
    return {"deleted_reports": dr.deleted_count, "deleted_logs": hl.deleted_count}


@api.get("/admin/analytics")
async def admin_analytics(_: dict = Depends(require_role("admin"))):
    # Get all dealers in one query
    dealers = await db.users.find({"role": "dealer"}, {"_id": 0}).to_list(length=200)
    if not dealers:
        return []

    dealer_ids = [d["id"] for d in dealers]
    dealer_map = {d["id"]: d for d in dealers}

    # Single aggregation for client + worker counts per dealer
    user_counts = await db.users.aggregate([
        {"$match": {"dealer_id": {"$in": dealer_ids}, "role": {"$in": ["user", "worker"]}}},
        {"$group": {"_id": {"dealer_id": "$dealer_id", "role": "$role"}, "count": {"$sum": 1}}},
    ]).to_list(length=1000)

    counts = {}
    for uc in user_counts:
        did = uc["_id"]["dealer_id"]
        role = uc["_id"]["role"]
        if did not in counts:
            counts[did] = {"total_clients": 0, "total_workers": 0}
        if role == "user":
            counts[did]["total_clients"] = uc["count"]
        else:
            counts[did]["total_workers"] = uc["count"]

    # Single aggregation for ticket stats per dealer (including avg resolution time)
    ticket_stats = await db.tickets.aggregate([
        {"$match": {"dealer_id": {"$in": dealer_ids}}},
        {"$group": {
            "_id": "$dealer_id",
            "tickets_all": {"$sum": 1},
            "tickets_open": {"$sum": {"$cond": [{"$in": ["$status", ["open", "assigned", "in_progress"]]}, 1, 0]}},
            "tickets_resolved": {"$sum": {"$cond": [{"$in": ["$status", ["resolved", "closed"]]}, 1, 0]}},
            "resolved_deltas": {
                "$push": {
                    "$cond": [
                        {"$and": [{"$ne": ["$resolved_at", None]}, {"$ne": ["$created_at", None]}]},
                        {"$subtract": [{"$toLong": {"$toDate": "$resolved_at"}}, {"$toLong": {"$toDate": "$created_at"}}]},
                        None,
                    ]
                }
            },
        }},
    ]).to_list(length=1000)

    ts_map = {}
    for ts in ticket_stats:
        did = ts["_id"]
        deltas = [d for d in ts.get("resolved_deltas", []) if d is not None and d > 0]
        avg_mins = round((sum(deltas) / len(deltas) / 60000), 1) if deltas else 0
        ts_map[did] = {
            "tickets_all": ts["tickets_all"],
            "tickets_open": ts["tickets_open"],
            "tickets_resolved": ts["tickets_resolved"],
            "avg_resolution_min": avg_mins,
        }

    result = []
    for d in dealers:
        did = d["id"]
        c = counts.get(did, {"total_clients": 0, "total_workers": 0})
        t = ts_map.get(did, {"tickets_all": 0, "tickets_open": 0, "tickets_resolved": 0, "avg_resolution_min": 0})
        result.append({
            "dealer_id": did, "dealer_name": d["name"], "dealer_code": d.get("dealer_code"),
            "city": d.get("city"), "total_clients": c["total_clients"], "total_workers": c["total_workers"],
            "tickets_all": t["tickets_all"], "tickets_open": t["tickets_open"],
            "tickets_resolved": t["tickets_resolved"],
            "resolve_rate": round(100 * t["tickets_resolved"] / max(1, t["tickets_all"]), 1),
            "avg_resolution_min": t["avg_resolution_min"],
        })
    return result


@api.get("/admin/area-analytics")
async def admin_area_analytics(_: dict = Depends(require_role("admin"))):
    """Aggregate resolve stats by dealer city."""
    pipeline = [
        {"$group": {"_id": "$location", "total": {"$sum": 1},
                    "resolved": {"$sum": {"$cond": [{"$in": ["$status", ["resolved", "closed"]]}, 1, 0]}}}},
        {"$sort": {"total": -1}}, {"$limit": 30},
    ]
    docs = await db.tickets.aggregate(pipeline).to_list(length=30)
    return [{"area": d["_id"], "total": d["total"], "resolved": d["resolved"],
             "rate": round(100 * d["resolved"] / max(1, d["total"]), 1)} for d in docs]


@api.get("/admin/dealers")
async def admin_list_dealers(_: dict = Depends(require_role("admin"))):
    dealers = await db.users.find({"role": "dealer"}, {"_id": 0, "password_hash": 0}).to_list(length=500)
    if not dealers:
        return []

    dealer_ids = [d["id"] for d in dealers]

    # Single aggregation for user counts (workers + clients)
    user_counts = await db.users.aggregate([
        {"$match": {"dealer_id": {"$in": dealer_ids}, "role": {"$in": ["user", "worker"]}}},
        {"$group": {"_id": {"dealer_id": "$dealer_id", "role": "$role"}, "count": {"$sum": 1}}},
    ]).to_list(length=1000)

    ucounts = {}
    for uc in user_counts:
        did = uc["_id"]["dealer_id"]
        if did not in ucounts:
            ucounts[did] = {"worker_count": 0, "client_count": 0}
        if uc["_id"]["role"] == "worker":
            ucounts[did]["worker_count"] = uc["count"]
        else:
            ucounts[did]["client_count"] = uc["count"]

    # Single aggregation for ticket counts per dealer
    ticket_counts = await db.tickets.aggregate([
        {"$match": {"dealer_id": {"$in": dealer_ids}}},
        {"$group": {
            "_id": "$dealer_id",
            "total_tickets": {"$sum": 1},
            "resolved_tickets": {"$sum": {"$cond": [{"$in": ["$status", ["resolved", "closed"]]}, 1, 0]}},
        }},
    ]).to_list(length=1000)

    tcounts = {tc["_id"]: tc for tc in ticket_counts}

    for d in dealers:
        did = d["id"]
        uc = ucounts.get(did, {"worker_count": 0, "client_count": 0})
        tc = tcounts.get(did, {"total_tickets": 0, "resolved_tickets": 0})
        d["worker_count"] = uc["worker_count"]
        d["client_count"] = uc["client_count"]
        d["total_tickets"] = tc["total_tickets"]
        d["resolved_tickets"] = tc["resolved_tickets"]

    return dealers


@api.post("/admin/dealers")
async def admin_create_dealer(body: CreateDealerIn, _: dict = Depends(require_role("admin"))):
    email = body.email.lower().strip()
    if await db.users.find_one({"email": email}):
        raise HTTPException(400, "Email already exists")
    code = body.dealer_code.upper().strip()
    if await db.users.find_one({"dealer_code": code}):
        raise HTTPException(400, "Dealer code already exists")
    uid = str(uuid.uuid4())
    await db.users.insert_one({
        "id": uid, "email": email, "password_hash": hash_pw(body.password), "name": body.name,
        "role": "dealer", "phone": body.phone, "city": body.city, "lat": body.lat, "lng": body.lng,
        "address": getattr(body, "address", "") or body.city,
        "dealer_code": code, "active": True, "created_at": now_iso(),
    })
    return {"id": uid, "dealer_code": code}


@api.delete("/admin/dealers/{dealer_id}")
async def admin_delete_dealer(dealer_id: str, _: dict = Depends(require_role("admin"))):
    dealer = await db.users.find_one({"id": dealer_id, "role": "dealer"})
    if not dealer:
        raise HTTPException(404, "Dealer not found")
    # Cascade: delete workers, clients, routers, tickets, notifications under this dealer
    workers = [w["id"] async for w in db.users.find({"role": "worker", "dealer_id": dealer_id}, {"id": 1})]
    clients = [c["id"] async for c in db.users.find({"role": "user", "dealer_id": dealer_id}, {"id": 1})]
    await db.users.delete_many({"dealer_id": dealer_id})
    await db.routers.delete_many({"dealer_id": dealer_id})
    await db.tickets.delete_many({"dealer_id": dealer_id})
    await db.notifications.delete_many({"user_id": {"$in": [dealer_id] + workers + clients}})
    await db.users.delete_one({"id": dealer_id})
    return {
        "ok": True,
        "removed": {
            "dealer": 1, "workers": len(workers), "clients": len(clients),
        },
    }


@api.get("/admin/dealers/{dealer_id}/workers")
async def admin_dealer_workers(dealer_id: str, _: dict = Depends(require_role("admin"))):
    dealer = await db.users.find_one({"id": dealer_id, "role": "dealer"})
    if not dealer:
        raise HTTPException(404, "Dealer not found")
    workers = await db.users.find(
        {"role": "worker", "dealer_id": dealer_id},
        {"_id": 0, "password_hash": 0},
    ).to_list(length=500)
    return workers


@api.get("/admin/system-stats")
async def admin_system_stats(_: dict = Depends(require_role("admin"))):
    stats = {
        "total_dealers": await db.users.count_documents({"role": "dealer"}),
        "total_workers": await db.users.count_documents({"role": "worker"}),
        "total_clients": await db.users.count_documents({"role": "user"}),
        "total_routers": await db.routers.count_documents({}),
        "open_tickets": await db.tickets.count_documents({"status": {"$in": ["open", "assigned", "in_progress"]}}),
        "resolved_tickets": await db.tickets.count_documents({"status": {"$in": ["resolved", "closed"]}}),
        "pppoe_sessions": 0,
        "mikrotik_online": False,
    }
    # Try to get live PPPoE session count from MikroTik
    try:
        host, port, username, password, use_ssl = await _get_mikrotik_config()
        sessions = await get_pppoe_sessions(host, port, username, password, use_ssl)
        stats["pppoe_sessions"] = len(sessions)
        stats["mikrotik_online"] = True
    except HTTPException:
        stats["mikrotik_online"] = False
    except Exception:
        stats["mikrotik_online"] = False
    return stats


@api.get("/admin/clients/unassigned")
async def admin_unassigned_clients(_: dict = Depends(require_role("admin"))):
    clients = await db.users.find(
        {"role": "user", "$or": [{"dealer_id": None}, {"dealer_id": {"$exists": False}}]},
        {"_id": 0, "password_hash": 0},
    ).to_list(length=500)
    return clients


@api.post("/admin/clients/{client_id}/assign-dealer")
async def admin_assign_client(client_id: str, body: AssignClientIn, _: dict = Depends(require_role("admin"))):
    client = await db.users.find_one({"id": client_id, "role": "user"})
    if not client:
        raise HTTPException(404, "Client not found")
    if client.get("dealer_id"):
        raise HTTPException(400, "Client already assigned to a dealer")
    dealer = await db.users.find_one({"id": body.dealer_id, "role": "dealer"})
    if not dealer:
        raise HTTPException(404, "Dealer not found")
    # Assign client to dealer
    await db.users.update_one({"id": client_id}, {"$set": {"dealer_id": body.dealer_id}})
    # Reassign all their routers
    await db.routers.update_many({"user_id": client_id}, {"$set": {"dealer_id": body.dealer_id}})
    # Reassign all their open tickets
    await db.tickets.update_many(
        {"user_id": client_id, "status": {"$in": ["open", "assigned", "in_progress"]}},
        {"$set": {"dealer_id": body.dealer_id}},
    )
    # Notify dealer
    await db.notifications.insert_one({
        "id": str(uuid.uuid4()), "user_id": body.dealer_id, "ticket_id": None,
        "type": "client_assigned", "title": "Client Assigned",
        "message": f"Admin assigned client {client['name']} to you.",
        "read": False, "created_at": now_iso(),
    })
    return {"ok": True}


# ---------------- Admin: PPPoE Assignment ----------------
@api.get("/admin/pppoe-users")
async def admin_pppoe_users(_: dict = Depends(require_role("admin"))):
    """Get all PPPoE users from MikroTik, with assignment status."""
    host, port, username, password, use_ssl = await _get_mikrotik_config()
    secrets = await get_pppoe_secrets(host, port, username, password, use_ssl)

    assigned_usernames = set()
    assigned_docs = await db.routers.find(
        {"pppoe_username": {"$ne": None, "$ne": ""}},
        {"_id": 0, "pppoe_username": 1}
    ).to_list(length=5000)
    for doc in assigned_docs:
        assigned_usernames.add(doc.get("pppoe_username", ""))

    result = []
    for s in secrets:
        result.append({
            **s,
            "assigned": s["name"] in assigned_usernames,
        })
    return {"users": result, "total": len(result)}


@api.post("/admin/assign-router")
async def admin_assign_router(body: AssignRouterIn, request: Request, user: dict = Depends(require_role("admin"))):
    """Assign a PPPoE user to a client. Creates a router record."""
    client_user = await db.users.find_one({"id": body.pppoe_username}, {"_id": 1})
    if not client_user:
        pass  # pppoe_username is the MikroTik secret name, not user id

    # Validate PPPoE username exists on MikroTik
    host, port, username, password, use_ssl = await _get_mikrotik_config()
    secrets = await get_pppoe_secrets(host, port, username, password, use_ssl)
    secret = next((s for s in secrets if s["name"] == body.pppoe_username), None)
    if not secret:
        raise HTTPException(404, f"PPPoE user '{body.pppoe_username}' not found on MikroTik")

    # Check if already assigned
    existing = await db.routers.find_one({"pppoe_username": body.pppoe_username})
    if existing:
        raise HTTPException(400, f"PPPoE user '{body.pppoe_username}' is already assigned to a client")

    return {"secret": secret, "ok": True}


@api.post("/admin/assign-router/confirm")
async def admin_assign_router_confirm(body: dict, request: Request, user: dict = Depends(require_role("admin"))):
    """Confirm PPPoE assignment — creates the router record."""
    client_id = body.get("client_id")
    pppoe_username = body.get("pppoe_username")

    if not client_id or not pppoe_username:
        raise HTTPException(400, "client_id and pppoe_username required")

    client = await db.users.find_one({"id": client_id, "role": "user"})
    if not client:
        raise HTTPException(404, "Client not found")

    # Check not already assigned
    existing = await db.routers.find_one({"pppoe_username": pppoe_username})
    if existing:
        raise HTTPException(400, f"PPPoE user '{pppoe_username}' is already assigned")

    # Check client doesn't already have a router
    client_has_router = await db.routers.find_one({"user_id": client_id})
    if client_has_router:
        raise HTTPException(400, "Client already has a router assigned")

    # Get PPPoE details from MikroTik
    host, port, username, password, use_ssl = await _get_mikrotik_config()
    secrets = await get_pppoe_secrets(host, port, username, password, use_ssl)
    secret = next((s for s in secrets if s["name"] == pppoe_username), None)
    if not secret:
        raise HTTPException(404, f"PPPoE user '{pppoe_username}' not found on MikroTik")

    # Get live session data if online
    sessions = await get_pppoe_sessions(host, port, username, password, use_ssl)
    session = next((s for s in sessions if s["name"] == pppoe_username), None)

    rid = f"RTR-{uuid.uuid4().hex[:8].upper()}"
    now = now_iso()
    router_doc = {
        "id": str(uuid.uuid4()),
        "router_id": rid,
        "user_id": client_id,
        "dealer_id": client.get("dealer_id"),
        "client_name": client["name"],
        "pppoe_username": pppoe_username,
        "pppoe_profile": secret.get("profile", "default"),
        "pppoe_ip": session["address"] if session else None,
        "status": "online" if session else "offline",
        "health_status": "online" if session else "offline",
        "signal": None,
        "usage_in": session.get("bytes_in", 0) if session else 0,
        "usage_out": session.get("bytes_out", 0) if session else 0,
        "uptime": session.get("uptime", "0s") if session else "0s",
        "last_check": now,
        "created_at": now,
    }
    await db.routers.insert_one(router_doc)

    # Notify client
    await db.notifications.insert_one({
        "id": str(uuid.uuid4()),
        "user_id": client_id,
        "ticket_id": None,
        "type": "router_assigned",
        "title": "Router Assigned",
        "message": f"Admin has assigned your internet connection ({pppoe_username}).",
        "read": False,
        "created_at": now,
    })

    await log_action(user, "assign_router", f"Client: {client['name']}, PPPoE: {pppoe_username}", request)
    return {"ok": True, "router_id": rid}


@api.delete("/admin/unassign-router/{router_id}")
async def admin_unassign_router(router_id: str, request: Request, user: dict = Depends(require_role("admin"))):
    """Remove PPPoE assignment from a client."""
    router = await db.routers.find_one({"router_id": router_id})
    if not router:
        raise HTTPException(404, "Router not found")

    client_id = router.get("user_id")
    pppoe_username = router.get("pppoe_username", router_id)

    await db.routers.delete_one({"router_id": router_id})
    await db.router_health.delete_many({"router_id": router_id})

    if client_id:
        await db.notifications.insert_one({
            "id": str(uuid.uuid4()),
            "user_id": client_id,
            "ticket_id": None,
            "type": "router_unassigned",
            "title": "Router Removed",
            "message": f"Admin has removed your internet connection ({pppoe_username}).",
            "read": False,
            "created_at": now_iso(),
        })

    await log_action(user, "unassign_router", f"Router: {router_id}, PPPoE: {pppoe_username}", request)
    return {"ok": True, "deleted": router_id}


@api.get("/admin/clients/all")
async def admin_all_clients(_: dict = Depends(require_role("admin"))):
    """Get ALL clients (not just unassigned)."""
    clients = await db.users.find(
        {"role": "user"},
        {"_id": 0, "password_hash": 0},
    ).to_list(length=1000)

    # Attach dealer info
    dealer_ids = list(set(c.get("dealer_id") for c in clients if c.get("dealer_id")))
    dealers = await db.users.find(
        {"id": {"$in": dealer_ids}},
        {"_id": 0, "id": 1, "dealer_code": 1, "name": 1}
    ).to_list(length=500)
    dealer_map = {d["id"]: d for d in dealers}

    # Attach router info
    client_ids = [c["id"] for c in clients]
    routers = await db.routers.find(
        {"user_id": {"$in": client_ids}},
        {"_id": 0, "router_id": 1, "user_id": 1, "pppoe_username": 1, "status": 1, "health_status": 1}
    ).to_list(length=1000)
    router_map = {r["user_id"]: r for r in routers}

    for c in clients:
        did = c.get("dealer_id")
        if did and did in dealer_map:
            c["dealer_code"] = dealer_map[did].get("dealer_code", "")
            c["dealer_name"] = dealer_map[did].get("name", "")
        else:
            c["dealer_code"] = None
            c["dealer_name"] = None

        r = router_map.get(c["id"])
        if r:
            c["router_assigned"] = True
            c["router_id"] = r["router_id"]
            c["pppoe_username"] = r.get("pppoe_username")
            c["router_status"] = r.get("health_status") or r.get("status", "unknown")
        else:
            c["router_assigned"] = False
            c["router_id"] = None
            c["pppoe_username"] = None
            c["router_status"] = None

    return clients


# ---------------- MikroTik ----------------
async def _get_mikrotik_config():
    """Get MikroTik config with decrypted password. Returns (host, port, username, password, use_ssl) or raises."""
    config = await db.mikrotik_config.find_one({}, {"_id": 0})
    if not config:
        raise HTTPException(400, "MikroTik not configured. Save config first.")
    password = decrypt_secret(config.get("password_encrypted", ""))
    return config["host"], config["port"], config["username"], password, config.get("use_ssl", True)
@api.post("/admin/mikrotik/config")
async def save_mikrotik_config(body: MikroTikConfigIn, request: Request, user: dict = Depends(require_role("admin"))):
    """Save MikroTik CCR connection config."""
    config = {
        "host": body.host,
        "port": body.port,
        "username": body.username,
        "password_encrypted": encrypt_secret(body.password),
        "use_ssl": body.use_ssl,
        "updated_at": now_iso(),
    }
    await db.mikrotik_config.update_one({}, {"$set": config}, upsert=True)
    await log_action(user, "mikrotik_config_save", f"Host: {body.host}:{body.port}", request)
    return {"ok": True, "message": "MikroTik config saved"}


@api.get("/admin/mikrotik/config")
async def get_mikrotik_config(_: dict = Depends(require_role("admin"))):
    """Get MikroTik config (password masked)."""
    config = await db.mikrotik_config.find_one({}, {"_id": 0})
    if not config:
        return {"configured": False}
    config.pop("password_encrypted", None)
    config["password"] = "***"
    config["configured"] = True
    return config


@api.get("/admin/mikrotik/test")
async def test_mikrotik_connection(request: Request, user: dict = Depends(require_role("admin"))):
    """Test connection to MikroTik CCR."""
    host, port, username, password, use_ssl = await _get_mikrotik_config()
    result = await test_connection(host, port, username, password, use_ssl)
    await log_action(user, "mikrotik_test", f"Host: {host}:{port}, Success: {result.get('connected')}", request)
    return result


@api.get("/admin/mikrotik/sessions")
async def list_pppoe_sessions(_: dict = Depends(require_role("admin"))):
    """Get all active PPPoE sessions from MikroTik CCR."""
    host, port, username, password, use_ssl = await _get_mikrotik_config()
    sessions = await get_pppoe_sessions(host, port, username, password, use_ssl)
    return {"sessions": sessions, "total": len(sessions)}


@api.get("/admin/mikrotik/secrets")
async def list_pppoe_secrets(_: dict = Depends(require_role("admin"))):
    """Get all configured PPPoE users from MikroTik CCR."""
    host, port, username, password, use_ssl = await _get_mikrotik_config()
    secrets = await get_pppoe_secrets(host, port, username, password, use_ssl)
    return {"secrets": secrets, "total": len(secrets)}


@api.get("/admin/mikrotik/profiles")
async def list_pppoe_profiles(_: dict = Depends(require_role("admin"))):
    """Get all PPPoE profiles from MikroTik CCR."""
    host, port, username, password, use_ssl = await _get_mikrotik_config()
    profiles = await get_profiles(host, port, username, password, use_ssl)
    return {"profiles": profiles}


@api.get("/admin/mikrotik/interfaces")
async def list_interfaces(_: dict = Depends(require_role("admin"))):
    """Get all interfaces with traffic stats from MikroTik CCR."""
    host, port, username, password, use_ssl = await _get_mikrotik_config()
    interfaces = await get_interfaces(host, port, username, password, use_ssl)
    return {"interfaces": interfaces}


@api.get("/admin/mikrotik/queues")
async def list_queues(_: dict = Depends(require_role("admin"))):
    """Get all simple queues (bandwidth limits) from MikroTik CCR."""
    host, port, username, password, use_ssl = await _get_mikrotik_config()
    queues = await get_simple_queues(host, port, username, password, use_ssl)
    return {"queues": queues}


@api.get("/admin/mikrotik/usage")
async def client_usage(_: dict = Depends(require_role("admin"))):
    """Get traffic usage per active PPPoE session."""
    host, port, username, password, use_ssl = await _get_mikrotik_config()
    usage = await get_client_usage(host, port, username, password, use_ssl)
    return {"usage": usage}


@api.post("/admin/mikrotik/disconnect/{session_id}")
async def admin_disconnect_client(session_id: str, request: Request, user: dict = Depends(require_role("admin"))):
    """Disconnect an active PPPoE session."""
    host, port, username, password, use_ssl = await _get_mikrotik_config()
    result = await disconnect_client(host, port, username, password, session_id, use_ssl)
    await log_action(user, "mikrotik_disconnect", f"Session: {session_id}", request, result.get("success", False))
    return result


@api.post("/admin/mikrotik/toggle-user/{secret_id}")
async def admin_toggle_client(secret_id: str, disabled: bool = True, request: Request = None, user: dict = Depends(require_role("admin"))):
    """Enable or disable a PPPoE user."""
    host, port, username, password, use_ssl = await _get_mikrotik_config()
    result = await enable_disable_client(host, port, username, password, secret_id, disabled, use_ssl)
    action = "disable" if disabled else "enable"
    await log_action(user, f"mikrotik_{action}", f"User: {secret_id}", request, result.get("success", False))
    return result


@api.post("/admin/mikrotik/change-profile/{secret_id}")
async def admin_change_profile(secret_id: str, new_profile: str, request: Request, user: dict = Depends(require_role("admin"))):
    """Change PPPoE profile for a client."""
    host, port, username, password, use_ssl = await _get_mikrotik_config()
    result = await change_client_profile(host, port, username, password, secret_id, new_profile, use_ssl)
    await log_action(user, "mikrotik_change_profile", f"User: {secret_id}, Profile: {new_profile}", request, result.get("success", False))
    return result


# ---------------- Dealer ----------------
@api.get("/dealer/overview")
async def dealer_overview(user: dict = Depends(require_role("dealer"))):
    did = user["id"]
    return {
        "clients": await db.users.count_documents({"role": "user", "dealer_id": did}),
        "workers": await db.users.count_documents({"role": "worker", "dealer_id": did}),
        "routers": await db.routers.count_documents({"dealer_id": did}),
        "open_tickets": await db.tickets.count_documents({"dealer_id": did, "status": {"$in": ["open", "assigned", "in_progress"]}}),
        "unassigned": await db.tickets.count_documents({"dealer_id": did, "status": "open"}),
        "resolved_today": await db.tickets.count_documents({
            "dealer_id": did, "status": "resolved",
            "resolved_at": {"$gte": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()},
        }),
        "dealer_code": user.get("dealer_code"),
    }


@api.get("/dealer/tickets")
async def dealer_tickets(
    user: dict = Depends(require_role("dealer")),
    status_filter: Optional[str] = Query(None, alias="status"),
):
    VALID_STATUSES = {"open", "assigned", "in_progress", "resolved"}
    q = {"dealer_id": user["id"]}
    if status_filter and status_filter != "all":
        if status_filter not in VALID_STATUSES:
            raise HTTPException(status_code=422, detail="Invalid status filter")
        q["status"] = status_filter
    return await db.tickets.find(q, {"_id": 0}).sort("created_at", -1).limit(200).to_list(length=200)


@api.get("/dealer/tickets/{ticket_id}/nearby-workers")
async def dealer_nearby_workers(ticket_id: str, user: dict = Depends(require_role("dealer"))):
    ticket = await db.tickets.find_one({"id": ticket_id, "dealer_id": user["id"]}, {"_id": 0})
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    workers = await db.users.find({"role": "worker", "dealer_id": user["id"], "active": True}, {"_id": 0, "password_hash": 0}).to_list(length=200)
    tlat, tlng = ticket.get("lat"), ticket.get("lng")
    for w in workers:
        if tlat and tlng and w.get("lat") and w.get("lng"):
            w["distance_km"] = round(haversine_km(tlat, tlng, w["lat"], w["lng"]), 2)
        else:
            w["distance_km"] = None
        # Count active assignments
        w["active_jobs"] = await db.tickets.count_documents(
            {"worker_id": w["id"], "status": {"$in": ["assigned", "in_progress"]}}
        )
    workers.sort(key=lambda w: (w["distance_km"] if w["distance_km"] is not None else 9999, w["active_jobs"]))
    return {"ticket": ticket, "workers": workers}


@api.post("/dealer/tickets/{ticket_id}/assign")
async def dealer_assign_ticket(ticket_id: str, body: AssignTicketIn, user: dict = Depends(require_role("dealer"))):
    ticket = await db.tickets.find_one({"id": ticket_id, "dealer_id": user["id"]})
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    worker = await db.users.find_one({"id": body.worker_id, "role": "worker", "dealer_id": user["id"]})
    if not worker:
        raise HTTPException(404, "Worker not found under this dealer")
    await db.tickets.update_one({"id": ticket_id}, {"$set": {
        "worker_id": worker["id"], "worker_name": worker["name"],
        "status": "assigned", "assigned_at": now_iso(),
    }})
    # Notify worker
    await db.notifications.insert_one({
        "id": str(uuid.uuid4()), "user_id": worker["id"], "ticket_id": ticket_id,
        "type": "assignment", "title": "New Assignment",
        "message": f"You've been assigned to {ticket['ticket_number']} — {ticket['issue_type'].replace('_', ' ')} at {ticket['location']}",
        "read": False, "created_at": now_iso(),
    })
    return {"ok": True}


@api.get("/dealer/workers")
async def dealer_workers(user: dict = Depends(require_role("dealer"))):
    workers = await db.users.find({"role": "worker", "dealer_id": user["id"]}, {"_id": 0, "password_hash": 0}).to_list(length=200)
    for w in workers:
        w["active_jobs"] = await db.tickets.count_documents(
            {"worker_id": w["id"], "status": {"$in": ["assigned", "in_progress"]}}
        )
        w["completed_jobs"] = await db.tickets.count_documents(
            {"worker_id": w["id"], "status": {"$in": ["resolved", "closed"]}}
        )
    return workers


@api.post("/dealer/workers")
async def dealer_create_worker(body: CreateWorkerIn, user: dict = Depends(require_role("dealer"))):
    email = body.email.lower().strip()
    if await db.users.find_one({"email": email}):
        raise HTTPException(400, "Email already exists")
    uid = str(uuid.uuid4())
    await db.users.insert_one({
        "id": uid, "email": email, "password_hash": hash_pw(body.password), "name": body.name,
        "role": "worker", "phone": body.phone, "area": body.area,
        "address": body.address or "", "lat": body.lat, "lng": body.lng,
        "dealer_id": user["id"], "active": True, "created_at": now_iso(),
    })
    return {"id": uid, "email": email}


@api.get("/dealer/clients")
async def dealer_clients(user: dict = Depends(require_role("dealer"))):
    return await db.users.find({"role": "user", "dealer_id": user["id"]}, {"_id": 0, "password_hash": 0}).limit(500).to_list(length=500)


@api.post("/dealer/clients")
async def dealer_create_client(body: CreateClientIn, user: dict = Depends(require_role("dealer"))):
    email = body.email.lower().strip()
    if await db.users.find_one({"email": email}):
        raise HTTPException(400, "Email already exists")
    uid = str(uuid.uuid4())
    await db.users.insert_one({
        "id": uid, "email": email, "password_hash": hash_pw(body.password), "name": body.name,
        "role": "user", "phone": body.phone, "address": body.address, "lat": body.lat, "lng": body.lng,
        "dealer_id": user["id"], "active": True, "created_at": now_iso(),
    })
    return {"id": uid, "email": email}


# ---------------- Worker ----------------
@api.get("/worker/tasks")
async def worker_tasks(user: dict = Depends(require_role("worker"))):
    return await db.tickets.find(
        {"worker_id": user["id"]}, {"_id": 0}
    ).sort("created_at", -1).limit(200).to_list(length=200)


@api.post("/worker/tasks/{ticket_id}/start")
async def worker_start(ticket_id: str, user: dict = Depends(require_role("worker"))):
    t = await db.tickets.find_one({"id": ticket_id, "worker_id": user["id"]})
    if not t:
        raise HTTPException(404, "Task not found")
    await db.tickets.update_one({"id": ticket_id}, {"$set": {"status": "in_progress", "started_at": now_iso()}})
    return {"ok": True}


@api.post("/worker/tasks/{ticket_id}/complete")
async def worker_complete(ticket_id: str, user: dict = Depends(require_role("worker"))):
    t = await db.tickets.find_one({"id": ticket_id, "worker_id": user["id"]})
    if not t:
        raise HTTPException(404, "Task not found")
    await db.tickets.update_one({"id": ticket_id}, {"$set": {
        "status": "resolved", "completed_at": now_iso(), "resolved_at": now_iso(),
    }})
    # Restore router
    await db.routers.update_one(
        {"router_id": t["router_id"]},
        {"$set": {"status": "online", "signal": random.randint(85, 99), "issue_type": None, "last_ping": now_iso()}},
    )
    # Notify client
    await db.notifications.insert_one({
        "id": str(uuid.uuid4()), "user_id": t["user_id"], "ticket_id": ticket_id,
        "type": "restored", "title": "Connection Restored",
        "message": f"Your connection ({t['router_id']}) has been restored by {user['name']}. Please confirm & submit feedback.",
        "read": False, "created_at": now_iso(),
    })
    # Notify dealer
    await db.notifications.insert_one({
        "id": str(uuid.uuid4()), "user_id": t["dealer_id"], "ticket_id": ticket_id,
        "type": "worker_done", "title": "Job Completed",
        "message": f"{user['name']} completed {t['ticket_number']} ({t['client_name']}).",
        "read": False, "created_at": now_iso(),
    })
    return {"ok": True}


# ---------------- User (Client) ----------------
@api.get("/user/routers")
async def user_routers(user: dict = Depends(require_role("user"))):
    """Get user's routers with live PPPoE status from MikroTik."""
    routers = await db.routers.find({"user_id": user["id"]}, {"_id": 0, "admin_password": 0}).to_list(length=50)

    if not routers:
        return []

    # Try to get live PPPoE data from MikroTik
    try:
        host, port, username, password, use_ssl = await _get_mikrotik_config()
        sessions = await get_pppoe_sessions(host, port, username, password, use_ssl)
        usage_data = await get_client_usage(host, port, username, password, use_ssl)
        session_map = {s["name"]: s for s in sessions}
    except Exception:
        session_map = {}
        usage_data = {}

    for r in routers:
        pppoe_name = r.get("pppoe_username")
        if pppoe_name and pppoe_name in session_map:
            session = session_map[pppoe_name]
            r["status"] = "online"
            r["health_status"] = "online"
            r["pppoe_ip"] = session.get("address")
            r["pppoe_uptime"] = session.get("uptime")
            r["usage_in"] = session.get("bytes_in", 0)
            r["usage_out"] = session.get("bytes_out", 0)
            r["last_check"] = now_iso()
        elif pppoe_name:
            r["status"] = "offline"
            r["health_status"] = "offline"
            r["pppoe_ip"] = r.get("pppoe_ip")
            r["pppoe_uptime"] = "0s"
            r["usage_in"] = r.get("usage_in", 0)
            r["usage_out"] = r.get("usage_out", 0)

    return routers


@api.post("/user/routers")
async def user_create_router(body: CreateRouterIn, user: dict = Depends(require_role("user"))):
    if body.router_id and body.router_id.strip():
        rid = body.router_id.strip().upper()
        existing = await db.routers.find_one({"router_id": rid, "user_id": user["id"]})
        if existing:
            raise HTTPException(400, "A router with this ID is already registered to your account")
        any_existing = await db.routers.find_one({"router_id": rid})
        if any_existing:
            raise HTTPException(400, "This router ID is already registered to another account")
    else:
        rid = f"RTR-{uuid.uuid4().hex[:8].upper()}"
    router_doc = {
        "id": str(uuid.uuid4()), "router_id": rid, "user_id": user["id"],
        "dealer_id": user.get("dealer_id"),
        "client_name": user["name"], "location": body.location,
        "lat": body.lat, "lng": body.lng,
        "brand": body.brand, "model": body.model or "",
        "router_ip": body.router_ip, "wan_ip": body.wan_ip or "",
        "mac_address": body.mac_address or "",
        "admin_username": body.admin_username,
        "admin_password": encrypt_secret(body.admin_password) if body.admin_password else "",
        "serial_number": body.serial_number or "",
        "status": "online", "signal": None, "issue_type": None,
        "detection_status": "manual",
        "last_ping": now_iso(), "created_at": now_iso(),
    }
    await db.routers.insert_one(router_doc)
    # Update user address from map
    upd = {}
    if body.location:
        upd["address"] = body.location
    if body.lat is not None:
        upd["lat"] = body.lat
    if body.lng is not None:
        upd["lng"] = body.lng
    if upd:
        await db.users.update_one({"id": user["id"]}, {"$set": upd})
    # Run initial health check
    result = check_router_health(router_doc)
    await save_health_log(db, rid, result)
    await update_router_snapshot(db, rid, result)
    return {"ok": True, "router_id": rid}


@api.delete("/user/routers/{router_id}")
async def user_delete_router(router_id: str, user: dict = Depends(require_role("user"))):
    router = await db.routers.find_one({"router_id": router_id, "user_id": user["id"]})
    if not router:
        raise HTTPException(404, "Router not found")
    await db.routers.delete_one({"router_id": router_id})
    await db.router_health.delete_many({"router_id": router_id})
    return {"ok": True, "deleted": router_id}


@api.post("/user/report-issue")
async def user_report_issue(body: ReportIssueIn, user: dict = Depends(require_role("user"))):
    router = await db.routers.find_one({"router_id": body.router_id, "user_id": user["id"]})
    if not router:
        raise HTTPException(404, "Router not found")
    new_status = "offline" if body.issue_type in ("wire_cut", "outage", "no_signal") else "warning"
    signal = 0 if new_status == "offline" else random.randint(20, 45)
    await db.routers.update_one({"id": router["id"]}, {"$set": {
        "status": new_status, "signal": signal, "issue_type": body.issue_type, "last_ping": now_iso(),
    }})
    tid = str(uuid.uuid4())
    tnum = f"TKT-{random.randint(100000, 999999)}"
    await db.tickets.insert_one({
        "id": tid, "ticket_number": tnum, "router_id": router["router_id"], "user_id": user["id"],
        "dealer_id": user.get("dealer_id"), "worker_id": None, "worker_name": None,
        "client_name": user["name"], "location": router["location"],
        "lat": router.get("lat"), "lng": router.get("lng"),
        "issue_type": body.issue_type,
        "description": body.description or f"Client reported {body.issue_type.replace('_', ' ')}",
        "status": "open", "source": "manual", "created_at": now_iso(),
        "assigned_at": None, "started_at": None, "completed_at": None, "resolved_at": None, "feedback": None,
    })
    # Notify dealer
    if user.get("dealer_id"):
        await db.notifications.insert_one({
            "id": str(uuid.uuid4()), "user_id": user["dealer_id"], "ticket_id": tid,
            "type": "new_ticket", "title": "New Client Report",
            "message": f"{user['name']} reported {body.issue_type.replace('_', ' ')} at {router['location']}",
            "read": False, "created_at": now_iso(),
        })
    return {"ticket_number": tnum, "id": tid}


@api.get("/user/tickets")
async def user_tickets(user: dict = Depends(require_role("user"))):
    return await db.tickets.find({"user_id": user["id"]}, {"_id": 0}).sort("created_at", -1).to_list(length=200)


@api.post("/user/tickets/{ticket_id}/feedback")
async def user_feedback(ticket_id: str, body: FeedbackIn, user: dict = Depends(require_role("user"))):
    t = await db.tickets.find_one({"id": ticket_id, "user_id": user["id"]})
    if not t:
        raise HTTPException(404, "Ticket not found")
    if t["status"] != "resolved":
        raise HTTPException(400, "Feedback allowed only after resolution")
    fb = {"working": body.working, "reason": body.reason or "", "submitted_at": now_iso()}
    upd = {"feedback": fb}
    if body.working:
        upd["status"] = "closed"
    else:
        upd["status"] = "open"
        upd["resolved_at"] = None
        upd["worker_id"] = None
        upd["worker_name"] = None
        await db.routers.update_one(
            {"router_id": t["router_id"]},
            {"$set": {"status": "warning", "signal": random.randint(20, 50), "issue_type": "unresolved"}},
        )
    await db.tickets.update_one({"id": ticket_id}, {"$set": upd})
    # Notify dealer
    if t.get("dealer_id"):
        await db.notifications.insert_one({
            "id": str(uuid.uuid4()), "user_id": t["dealer_id"], "ticket_id": ticket_id,
            "type": "feedback" if body.working else "reopened",
            "title": "Feedback Received" if body.working else "Ticket Reopened",
            "message": f"{user['name']}: {'Working ✓' if body.working else 'Not working — ' + (body.reason or 'no reason')}",
            "read": False, "created_at": now_iso(),
        })
    return {"ok": True}


@api.delete("/user/tickets/{ticket_id}")
async def user_delete_ticket(ticket_id: str, body: TicketDeleteIn, user: dict = Depends(require_role("user"))):
    t = await db.tickets.find_one({"id": ticket_id, "user_id": user["id"]})
    if not t:
        raise HTTPException(404, "Ticket not found")
    if t["status"] != "open":
        raise HTTPException(400, "Only open tickets can be deleted")
    # Restore router to online
    await db.routers.update_one(
        {"router_id": t["router_id"]},
        {"$set": {"status": "online", "signal": random.randint(85, 99), "issue_type": None, "last_ping": now_iso()}},
    )
    # Notify dealer about deletion with reason
    if t.get("dealer_id"):
        await db.notifications.insert_one({
            "id": str(uuid.uuid4()), "user_id": t["dealer_id"], "ticket_id": ticket_id,
            "type": "ticket_deleted", "title": "Ticket Deleted by Client",
            "message": f"{user['name']} deleted ticket {t['ticket_number']}. Reason: {body.reason}",
            "read": False, "created_at": now_iso(),
        })
    # Delete ticket and its related notifications
    await db.tickets.delete_one({"id": ticket_id})
    await db.notifications.delete_many({"ticket_id": ticket_id})
    return {"ok": True}


# ---------------- Notifications (shared) ----------------
@api.get("/notifications")
async def get_notifications(user: dict = Depends(current_user)):
    return await db.notifications.find({"user_id": user["id"]}, {"_id": 0}).sort("created_at", -1).limit(50).to_list(length=50)


@api.post("/notifications/{nid}/read")
async def read_notification(nid: str, user: dict = Depends(current_user)):
    await db.notifications.update_one({"id": nid, "user_id": user["id"]}, {"$set": {"read": True}})
    return {"ok": True}


# ---------------- Router Health Monitoring ----------------
@api.post("/user/routers/register")
async def user_register_router(body: RegisterRouterIn, user: dict = Depends(require_role("user"))):
    """Register real router details for an existing router."""
    router = await db.routers.find_one({"router_id": body.router_id, "user_id": user["id"]})
    if not router:
        raise HTTPException(404, "Router not found. Make sure the router_id is correct.")

    update_fields = {
        "brand": body.brand,
        "model": body.model or "",
        "router_ip": body.router_ip,
        "wan_ip": body.wan_ip or "",
        "mac_address": body.mac_address or "",
        "admin_username": body.admin_username,
        "admin_password": encrypt_secret(body.admin_password) if body.admin_password else "",
        "serial_number": body.serial_number or "",
        "detection_status": "manual",
    }
    await db.routers.update_one(
        {"router_id": body.router_id},
        {"$set": update_fields}
    )
    # Run immediate health check
    result = check_router_health({**router, **update_fields})
    await save_health_log(db, body.router_id, result)
    await update_router_snapshot(db, body.router_id, result)
    return {"ok": True, "health": result}


@api.get("/user/routers/{router_id}/health")
async def user_router_health(router_id: str, user: dict = Depends(require_role("user"))):
    """Get latest health status for a user's router."""
    router = await db.routers.find_one({"router_id": router_id, "user_id": user["id"]})
    if not router:
        raise HTTPException(404, "Router not found")
    return {
        "router_id": router_id,
        "health_status": router.get("health_status", "unknown"),
        "wan_status": router.get("wan_status", "unknown"),
        "wan_ip": router.get("wan_ip"),
        "signal": router.get("signal"),
        "connected_devices": router.get("connected_devices"),
        "internet_uptime": router.get("internet_uptime"),
        "last_health_check": router.get("last_health_check"),
        "last_seen_online": router.get("last_seen_online"),
    }


@api.get("/user/routers/{router_id}/health/history")
async def user_router_health_history(
    router_id: str,
    hours: int = Query(24, ge=1, le=168),
    user: dict = Depends(require_role("user"))
):
    """Get health history for a user's router."""
    router = await db.routers.find_one({"router_id": router_id, "user_id": user["id"]})
    if not router:
        raise HTTPException(404, "Router not found")

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    logs = await db.router_health.find(
        {"router_id": router_id, "timestamp": {"$gte": cutoff}},
        {"_id": 0}
    ).sort("timestamp", 1).to_list(length=1000)
    return logs


@api.post("/user/routers/{router_id}/check-now")
async def user_check_router_now(router_id: str, user: dict = Depends(require_role("user"))):
    """Trigger an immediate health check for a router."""
    router = await db.routers.find_one({"router_id": router_id, "user_id": user["id"]})
    if not router:
        raise HTTPException(404, "Router not found")
    if not router.get("router_ip"):
        raise HTTPException(400, "Router IP not configured. Register router details first.")
    result = await check_single_router(db, router_id)
    return result


# ---------------- Speed Test ----------------
@api.get("/speedtest/ping")
async def speedtest_ping():
    """Simple ping endpoint for latency measurement."""
    return {"timestamp": now_iso()}


@api.get("/speedtest/test-file/{size}")
async def speedtest_download(size: int):
    """Serve test files for download speed test. Size in MB (1, 10, 50)."""
    if size not in (1, 10, 50):
        raise HTTPException(400, "Size must be 1, 10, or 50 MB")
    # Generate random data of specified size
    data = os.urandom(size * 1024 * 1024)
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="speedtest-{size}mb.bin"'},
    )


@api.post("/speedtest/upload")
async def speedtest_upload(request: Request):
    """Receive uploaded test file for upload speed measurement."""
    body = await request.body()
    size_received = len(body)
    return {"received": True, "bytes": size_received}


# ---------------- Dealer: Health overview for all their routers ----------------
@api.get("/dealer/routers/health/summary")
async def dealer_routers_health_summary(user: dict = Depends(require_role("dealer"))):
    """Get health summary for all routers under this dealer."""
    routers = await db.routers.find(
        {"dealer_id": user["id"]},
        {"_id": 0, "admin_password": 0}
    ).to_list(length=2000)
    return routers


@api.get("/dealer/routers/{router_id}/health/history")
async def dealer_router_health_history(
    router_id: str,
    hours: int = Query(24, ge=1, le=168),
    user: dict = Depends(require_role("dealer"))
):
    """Get health history for a dealer's router."""
    router = await db.routers.find_one({"router_id": router_id, "dealer_id": user["id"]})
    if not router:
        raise HTTPException(404, "Router not found")

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    logs = await db.router_health.find(
        {"router_id": router_id, "timestamp": {"$gte": cutoff}},
        {"_id": 0}
    ).sort("timestamp", 1).to_list(length=1000)
    return logs


@api.post("/dealer/routers/{router_id}/check-now")
async def dealer_check_router_now(router_id: str, user: dict = Depends(require_role("dealer"))):
    """Trigger immediate health check for a dealer's router."""
    router = await db.routers.find_one({"router_id": router_id, "dealer_id": user["id"]})
    if not router:
        raise HTTPException(404, "Router not found")
    if not router.get("router_ip"):
        raise HTTPException(400, "Router IP not configured.")
    result = await check_single_router(db, router_id)
    return result


@api.get("/health")
async def health():
    return {"status": "ok", "time": now_iso()}


# ---------------- SEED ----------------
async def seed():
    # Indexes are created in ensure_indexes() on startup
    admin_email = os.environ.get("ADMIN_EMAIL", "admin@netops.io")
    admin_password = os.environ.get("ADMIN_PASSWORD", "admin123")
    existing_admin = await db.users.find_one({"email": admin_email})
    if existing_admin is None:
        await db.users.insert_one({
            "id": str(uuid.uuid4()), "email": admin_email,
            "password_hash": hash_pw(admin_password), "name": "Baglan Ops Admin", "role": "admin",
            "phone": "", "active": True, "created_at": now_iso(),
        })
        log.info(f"Seeded admin: {admin_email}")


@app.on_event("startup")
async def on_start():
    await seed()
    await ensure_indexes()
    start_health_scheduler(db)


async def ensure_indexes():
    """Create compound indexes for common query patterns."""
    try:
        await db.users.create_index([("role", 1), ("dealer_id", 1)], background=True)
        await db.users.create_index([("email", 1)], background=True, unique=True)
        await db.tickets.create_index([("dealer_id", 1), ("status", 1)], background=True)
        await db.tickets.create_index([("user_id", 1), ("status", 1)], background=True)
        await db.tickets.create_index([("router_id", 1)], background=True)
        await db.routers.create_index([("user_id", 1)], background=True)
        await db.routers.create_index([("dealer_id", 1)], background=True)
        await db.routers.create_index([("router_id", 1)], background=True, unique=True)
        await db.audit_logs.create_index([("timestamp", -1)], background=True)
        await db.audit_logs.create_index([("user_id", 1)], background=True)
        await db.daily_reports.create_index([("router_id", 1), ("date", -1)], background=True)
        await db.daily_reports.create_index([("date", -1)], background=True)
        log.info("Database indexes ensured")
    except Exception as e:
        log.error(f"Index creation error: {e}")


@app.on_event("shutdown")
async def on_stop():
    client.close()


app.include_router(api)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "").split(",") if os.environ.get("CORS_ORIGINS") else ["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
