from __future__ import annotations

import json
from typing import Any, Optional, TYPE_CHECKING

from ..core.enums import AuditAction, VersionStatus
from ..core.models import AuditLog, _now_iso

if TYPE_CHECKING:
    from ..storage.repository import SwitchRepository


class AuditTrail:
    """Thin wrapper around the audit_log table.

    Keeps every mutation recorded with actor + action + old/new status
    + a free-form JSON details blob.
    """

    def __init__(self, repo: "SwitchRepository") -> None:
        self._repo = repo

    def record(
        self,
        actor: str,
        action: AuditAction,
        env: str,
        switch_name: str,
        *,
        version: Optional[int] = None,
        old_status: Optional[VersionStatus] = None,
        new_status: Optional[VersionStatus] = None,
        details: Optional[dict[str, Any]] = None,
        conn: Any = None,
    ) -> AuditLog:
        log = AuditLog(
            actor=actor,
            action=action,
            env=env,
            switch_name=switch_name,
            version=version,
            old_status=old_status.value if isinstance(old_status, VersionStatus) else old_status,
            new_status=new_status.value if isinstance(new_status, VersionStatus) else new_status,
            details=json.dumps(details or {}, ensure_ascii=False),
            timestamp=_now_iso(),
        )
        return self._repo.append_audit(log, conn=conn)
