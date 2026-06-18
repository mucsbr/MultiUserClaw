"""Admin API routes for user and system management."""

from __future__ import annotations

from datetime import datetime, timedelta

import docker
from docker.errors import NotFound as DockerNotFound
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import cast, Date, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from app.audit import write_audit_log
from app.auth.dependencies import require_admin
from app.auth.service import hash_password
from app.container.manager import destroy_container, pause_container, resume_container
from app.db.engine import get_db
from app.db.models import AuditLog, Container, SharedAgentBinding, UsageRecord, User

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


class UserSummary(BaseModel):
    id: str
    username: str
    email: str
    role: str
    quota_tier: str
    runtime_mode: str
    is_active: bool
    created_at: str | None = None
    container_status: str | None = None
    container_docker_id: str | None = None
    container_created_at: str | None = None
    shared_agent_id: str | None = None
    shared_agent_status: str | None = None
    tokens_used_today: int = 0
    tokens_used_7d: int = 0
    tokens_used_total: int = 0


class PaginatedUsers(BaseModel):
    items: list[UserSummary]
    total: int
    page: int
    page_size: int


class UpdateUserRequest(BaseModel):
    role: str | None = None
    quota_tier: str | None = None
    runtime_mode: str | None = None
    is_active: bool | None = None


class ResetPasswordRequest(BaseModel):
    new_password: str


async def _sync_container_status(db: AsyncSession, docker_id: str, db_status: str | None) -> str | None:
    """Sync container status from Docker API to database.
    
    Returns the real status from Docker, or None if container doesn't exist.
    """
    if not docker_id:
        return db_status
    
    try:
        client = docker.from_env()
        container = client.containers.get(docker_id)
        real_status = container.status  # running, exited, paused, created, etc.
        
        # Map Docker status to our DB status
        if real_status == "running":
            new_status = "running"
        elif real_status == "paused":
            new_status = "paused"
        elif real_status in ("exited", "dead", "removing"):
            new_status = "stopped"
        else:
            new_status = db_status  # keep DB status for other states like "creating"
        
        # Update DB if different
        if new_status != db_status:
            await db.execute(
                update(Container)
                .where(Container.docker_id == docker_id)
                .values(status=new_status)
            )
        
        return new_status
    except DockerNotFound:
        # Container was deleted externally, mark as stopped
        if db_status != "stopped":
            await db.execute(
                update(Container)
                .where(Container.docker_id == docker_id)
                .values(status="stopped")
            )
        return "stopped"
    except Exception:
        return db_status


@router.get("/users", response_model=PaginatedUsers)
async def list_users(
    search: str | None = None,
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
):
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=6)  # today + previous 6 days = 7-day window

    # Subquery: today's token usage per user
    usage_sub = (
        select(
            UsageRecord.user_id,
            func.coalesce(func.sum(UsageRecord.total_tokens), 0).label("tokens_today"),
        )
        .where(UsageRecord.created_at >= today_start)
        .group_by(UsageRecord.user_id)
        .subquery()
    )

    # Subquery: last 7 days token usage per user
    usage_7d_sub = (
        select(
            UsageRecord.user_id,
            func.coalesce(func.sum(UsageRecord.total_tokens), 0).label("tokens_7d"),
        )
        .where(UsageRecord.created_at >= week_start)
        .group_by(UsageRecord.user_id)
        .subquery()
    )

    # Subquery: all-time total token usage per user
    usage_total_sub = (
        select(
            UsageRecord.user_id,
            func.coalesce(func.sum(UsageRecord.total_tokens), 0).label("tokens_total"),
        )
        .group_by(UsageRecord.user_id)
        .subquery()
    )

    # Base query with outerjoin to Container and usage subquery
    query = (
        select(
            User.id,
            User.username,
            User.email,
            User.role,
            User.quota_tier,
            User.runtime_mode,
            User.is_active,
            User.created_at.label("user_created_at"),
            Container.status.label("container_status"),
            Container.docker_id.label("container_docker_id"),
            Container.created_at.label("container_created_at"),
            SharedAgentBinding.openclaw_agent_id.label("shared_agent_id"),
            SharedAgentBinding.status.label("shared_agent_status"),
            func.coalesce(usage_sub.c.tokens_today, 0).label("tokens_used_today"),
            func.coalesce(usage_7d_sub.c.tokens_7d, 0).label("tokens_used_7d"),
            func.coalesce(usage_total_sub.c.tokens_total, 0).label("tokens_used_total"),
        )
        .outerjoin(Container, Container.user_id == User.id)
        .outerjoin(SharedAgentBinding, SharedAgentBinding.user_id == User.id)
        .outerjoin(usage_sub, usage_sub.c.user_id == User.id)
        .outerjoin(usage_7d_sub, usage_7d_sub.c.user_id == User.id)
        .outerjoin(usage_total_sub, usage_total_sub.c.user_id == User.id)
    )

    # Search filter – escape SQL LIKE wildcards to prevent injection
    if search:
        safe = search.replace("%", r"\%").replace("_", r"\_")
        pattern = f"%{safe}%"
        query = query.where(
            (User.username.ilike(pattern))
            | (User.email.ilike(pattern))
            | (Container.docker_id.ilike(pattern))
        )

    # Total count (before pagination)
    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar_one()

    # Paginate
    query = query.order_by(User.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(query)).all()

    items = [
        UserSummary(
            id=row.id,
            username=row.username,
            email=row.email,
            role=row.role,
            quota_tier=row.quota_tier,
            runtime_mode=row.runtime_mode,
            is_active=row.is_active,
            created_at=row.user_created_at.isoformat() if row.user_created_at else None,
            container_status=row.container_status,
            container_docker_id=row.container_docker_id,
            container_created_at=row.container_created_at.isoformat() if row.container_created_at else None,
            shared_agent_id=row.shared_agent_id,
            shared_agent_status=row.shared_agent_status,
            tokens_used_today=row.tokens_used_today,
            tokens_used_7d=row.tokens_used_7d,
            tokens_used_total=row.tokens_used_total,
        )
        for row in rows
    ]

    return PaginatedUsers(items=items, total=total, page=page, page_size=page_size)


@router.put("/users/{user_id}")
async def update_user(
    user_id: str,
    req: UpdateUserRequest,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin),
):
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    values = {k: v for k, v in req.model_dump().items() if v is not None}
    if "runtime_mode" in values and values["runtime_mode"] not in {"dedicated", "shared"}:
        raise HTTPException(status_code=400, detail="runtime_mode must be dedicated or shared")
    if values:
        await db.execute(update(User).where(User.id == user_id).values(**values))
        await write_audit_log(
            db,
            action="user_update",
            user_id=admin_user.id,
            resource=user_id,
            detail={"fields": values},
        )
        await db.commit()
    return {"ok": True}


@router.put("/users/{user_id}/password")
async def reset_user_password(
    user_id: str,
    req: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin),
):
    if len(req.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    await db.execute(
        update(User).where(User.id == user_id).values(password_hash=hash_password(req.new_password))
    )
    await write_audit_log(
        db,
        action="password_reset",
        user_id=admin_user.id,
        resource=user_id,
        detail={"by_admin": admin_user.username},
    )
    await db.commit()
    return {"message": "Password updated"}


@router.delete("/users/{user_id}/container")
async def delete_user_container(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin),
):
    if await destroy_container(db, user_id):
        await write_audit_log(
            db,
            action="container_destroy",
            user_id=admin_user.id,
            resource=user_id,
            detail={"by_admin": admin_user.username},
        )
        await db.commit()
        return {"ok": True}
    raise HTTPException(status_code=404, detail="Container not found")


@router.post("/containers/sync")
async def sync_all_container_statuses(
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin),
):
    """Sync all container statuses from Docker to database.
    
    Returns the count of updated containers.
    """
    result = await db.execute(
        select(Container.id, Container.user_id, Container.docker_id, Container.status)
    )
    containers = result.all()
    
    if not containers:
        return {"updated": 0, "message": "No containers found"}
    
    updated_count = 0
    for container in containers:
        if container.docker_id:
            real_status = await _sync_container_status(db, container.docker_id, container.status)
            if real_status != container.status:
                updated_count += 1
    
    await write_audit_log(
        db,
        action="container_sync_all",
        user_id=admin_user.id,
        resource="all",
        detail={"updated": updated_count},
    )
    await db.commit()
    
    return {"updated": updated_count, "message": f"Synced {updated_count} containers"}


@router.post("/users/{user_id}/container/sync")
async def sync_single_container_status(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin),
):
    """Sync a single user's container status from Docker to database."""
    result = await db.execute(
        select(Container).where(Container.user_id == user_id)
    )
    container = result.scalar_one_or_none()
    
    if container is None:
        raise HTTPException(status_code=404, detail="Container not found")
    
    if not container.docker_id:
        raise HTTPException(status_code=400, detail="No docker_id for this container")
    
    real_status = await _sync_container_status(db, container.docker_id, container.status)
    await write_audit_log(
        db,
        action="container_sync",
        user_id=admin_user.id,
        resource=user_id,
        detail={"status": real_status, "docker_id": container.docker_id},
    )
    await db.commit()
    
    return {"status": real_status, "docker_id": container.docker_id}


@router.post("/users/{user_id}/container/pause")
async def pause_user_container(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin),
):
    if await pause_container(db, user_id):
        await write_audit_log(
            db,
            action="container_pause",
            user_id=admin_user.id,
            resource=user_id,
            detail={"by_admin": admin_user.username},
        )
        await db.commit()
        return {"ok": True}
    raise HTTPException(status_code=404, detail="Container not running")


@router.post("/users/{user_id}/container/resume")
async def resume_user_container(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin),
):
    if await resume_container(db, user_id):
        await write_audit_log(
            db,
            action="container_resume",
            user_id=admin_user.id,
            resource=user_id,
            detail={"by_admin": admin_user.username},
        )
        await db.commit()
        return {"ok": True}
    raise HTTPException(status_code=404, detail="Container not found or cannot be resumed")


@router.get("/usage/summary")
async def usage_summary(db: AsyncSession = Depends(get_db)):
    """Global usage summary for the platform."""
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    total_today = (await db.execute(
        select(func.coalesce(func.sum(UsageRecord.total_tokens), 0)).where(
            UsageRecord.created_at >= today_start,
        )
    )).scalar_one()
    total_users = (await db.execute(select(func.count(User.id)))).scalar_one()
    
    # Get containers with real running status from Docker
    try:
        client = docker.from_env()
        all_containers = client.containers.list(all=True)
        real_running = sum(1 for c in all_containers if c.status == "running")
    except Exception:
        real_running = 0
    
    # Fallback to DB status if Docker query fails
    db_active = (await db.execute(
        select(func.count(Container.id)).where(Container.status == "running")
    )).scalar_one()

    return {
        "total_tokens_today": total_today,
        "total_users": total_users,
        "active_containers": real_running or db_active,
    }


@router.get("/usage/history")
async def usage_history(
    days: int = 30,
    user_id: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Usage history with daily and by-model aggregations."""
    cutoff = datetime.utcnow() - timedelta(days=days)

    # --- Daily aggregation ---
    daily_q = (
        select(
            cast(UsageRecord.created_at, Date).label("date"),
            func.coalesce(func.sum(UsageRecord.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(UsageRecord.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(UsageRecord.total_tokens), 0).label("total_tokens"),
        )
        .where(UsageRecord.created_at >= cutoff)
        .group_by(cast(UsageRecord.created_at, Date))
        .order_by(cast(UsageRecord.created_at, Date))
    )
    if user_id:
        daily_q = daily_q.where(UsageRecord.user_id == user_id)

    daily_rows = (await db.execute(daily_q)).all()
    daily = [
        {
            "date": str(r.date),
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "total_tokens": r.total_tokens,
        }
        for r in daily_rows
    ]

    # --- By model aggregation ---
    model_q = (
        select(
            UsageRecord.model,
            func.coalesce(func.sum(UsageRecord.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(UsageRecord.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(UsageRecord.total_tokens), 0).label("total_tokens"),
        )
        .where(UsageRecord.created_at >= cutoff)
        .group_by(UsageRecord.model)
        .order_by(func.sum(UsageRecord.total_tokens).desc())
    )
    if user_id:
        model_q = model_q.where(UsageRecord.user_id == user_id)

    model_rows = (await db.execute(model_q)).all()
    by_model = [
        {
            "model": r.model,
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "total_tokens": r.total_tokens,
        }
        for r in model_rows
    ]

    return {"daily": daily, "by_model": by_model}


# ---------------------------------------------------------------------------
# Audit logs
# ---------------------------------------------------------------------------

class AuditLogItem(BaseModel):
    id: str
    user_id: str | None
    username: str | None
    action: str
    resource: str | None
    detail: str | None
    created_at: str


class PaginatedAuditLogs(BaseModel):
    items: list[AuditLogItem]
    total: int
    page: int
    page_size: int


@router.get("/audit", response_model=PaginatedAuditLogs)
async def list_audit_logs(
    page: int = 1,
    page_size: int = 20,
    user_id: str | None = None,
    action: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Paginated audit log with optional filters."""
    query = (
        select(
            AuditLog.id,
            AuditLog.user_id,
            User.username.label("username"),
            AuditLog.action,
            AuditLog.resource,
            AuditLog.detail,
            AuditLog.created_at,
        )
        .outerjoin(User, User.id == AuditLog.user_id)
    )

    if user_id:
        query = query.where(AuditLog.user_id == user_id)
    if action:
        query = query.where(AuditLog.action == action)

    # Total count
    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar_one()

    # Paginate
    query = query.order_by(AuditLog.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(query)).all()

    items = [
        AuditLogItem(
            id=row.id,
            user_id=row.user_id,
            username=row.username,
            action=row.action,
            resource=row.resource,
            detail=row.detail,
            created_at=row.created_at.isoformat() if row.created_at else "",
        )
        for row in rows
    ]

    return PaginatedAuditLogs(items=items, total=total, page=page, page_size=page_size)
