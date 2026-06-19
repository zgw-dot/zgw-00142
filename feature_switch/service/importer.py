from __future__ import annotations

import json
import os
from typing import Any

from ..audit import AuditTrail
from ..core.enums import AuditAction, VersionStatus
from ..core.models import SwitchVersion, _now_iso
from ..storage.repository import SwitchRepository
from ..validator.validators import (
    ValidationError,
    parse_json,
    parse_yaml,
    validate_dependencies,
    validate_switch_payload,
)

try:
    import yaml as _yaml  # type: ignore
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


SCHEMA_VERSION = "1.0"


def _dump_yaml(data: Any) -> str:
    if _HAS_YAML:
        return _yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    # Minimal fallback: JSON is valid YAML
    return json.dumps(data, ensure_ascii=False, indent=2)


class ConfigExporter:
    def __init__(self, repo: SwitchRepository, audit: AuditTrail) -> None:
        self.repo = repo
        self.audit = audit

    def export_effective(self, *, actor: str, env: str | None = None) -> dict:
        versions = self.repo.list_published_switches(env=env)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "exported_at": _now_iso(),
            "exported_by": actor,
            "count": len(versions),
            "switches": [v.effective_snapshot() for v in versions],
        }
        for v in versions:
            self.audit.record(
                actor=actor,
                action=AuditAction.EXPORT_CONFIG,
                env=v.env,
                switch_name=v.name,
                version=v.version,
                details={"exported_at": payload["exported_at"], "env": env},
            )
        return payload

    def export_effective_yaml(self, *, actor: str, env: str | None = None) -> str:
        return _dump_yaml(self.export_effective(actor=actor, env=env))

    def export_effective_json(self, *, actor: str, env: str | None = None) -> str:
        return json.dumps(
            self.export_effective(actor=actor, env=env),
            ensure_ascii=False,
            indent=2,
        )


class ConfigImporter:
    """Imports a switches document.

    Validation order (fail fast, DB untouched on any error):
      1. File syntax (YAML / JSON)
      2. schema_version match
      3. Per-switch payload shape / types (validate_switch_payload)
      4. Inter-switch dependency availability within the batch
      5. DB-level dependency availability
      6. Only *then* do we BEGIN transaction and write rows.
    """

    def __init__(self, repo: SwitchRepository, audit: AuditTrail) -> None:
        self.repo = repo
        self.audit = audit

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def import_file(self, *, actor: str, path: str) -> dict:
        raw = self._read(path)
        ext = os.path.splitext(path)[1].lower()
        if ext in (".yaml", ".yml"):
            return self.import_yaml(actor=actor, raw=raw, source=path)
        if ext == ".json":
            return self.import_json(actor=actor, raw=raw, source=path)
        # auto-detect
        stripped = raw.lstrip()
        if stripped.startswith("{") or stripped.startswith("["):
            return self.import_json(actor=actor, raw=raw, source=path)
        return self.import_yaml(actor=actor, raw=raw, source=path)

    def import_yaml(self, *, actor: str, raw: str, source: str = "<yaml>") -> dict:
        doc = parse_yaml(raw)  # raises ValidationError on bad syntax
        return self._import_document(actor=actor, doc=doc, source=source)

    def import_json(self, *, actor: str, raw: str, source: str = "<json>") -> dict:
        doc = parse_json(raw)  # raises ValidationError on bad syntax
        return self._import_document(actor=actor, doc=doc, source=source)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _read(path: str) -> str:
        if not os.path.isfile(path):
            raise ValidationError(f"导入文件不存在: {path}")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError as exc:
            raise ValidationError(f"读取导入文件失败: {exc}")

    def _import_document(self, *, actor: str, doc: Any, source: str) -> dict:
        if not isinstance(doc, dict):
            raise ValidationError("根文档必须是一个对象 (mapping)")
        schema = doc.get("schema_version")
        if schema != SCHEMA_VERSION:
            raise ValidationError(
                f"schema_version 不匹配: 期望 {SCHEMA_VERSION!r}，收到 {schema!r}"
            )
        switches = doc.get("switches")
        if not isinstance(switches, list) or not switches:
            raise ValidationError("'switches' 字段必须是非空列表")

        # Step 3: per-switch structural + type validation
        validated: list[dict] = []
        seen_keys: set[str] = set()
        for idx, item in enumerate(switches):
            if not isinstance(item, dict):
                raise ValidationError(
                    f"switches[{idx}] 必须是对象，收到 {type(item).__name__}"
                )
            enriched = dict(item)
            enriched.setdefault("author", actor)
            try:
                cleaned = validate_switch_payload(enriched)
            except ValidationError as exc:
                raise ValidationError(f"switches[{idx}] {exc}")
            key = f"{cleaned['env']}:{cleaned['name']}"
            if key in seen_keys:
                raise ValidationError(
                    f"switches[{idx}] 重复定义: {key}"
                )
            seen_keys.add(key)
            validated.append(cleaned)

        # Step 4: intra-batch dependency satisfaction (names only, same env)
        by_env: dict[str, set[str]] = {}
        for s in validated:
            by_env.setdefault(s["env"], set()).add(s["name"])
        for idx, s in enumerate(validated):
            bad = [d for d in s["dependencies"] if d not in by_env.get(s["env"], set())]
            # unresolved ones will be re-checked against DB below

        # Step 5: DB-level dependency check (without writing yet)
        # We allow: published switches + switches in this batch.
        for idx, s in enumerate(validated):
            validate_dependencies(
                s["env"],
                s["dependencies"],
                self.repo,
                extra_available=by_env.get(s["env"], set()),
            )

        # Step 6: single transaction writes every row (or none)
        imported: list[dict] = []
        with self.repo.transaction() as conn:
            for idx, s in enumerate(validated):
                switch = self.repo.get_or_create_switch(
                    s["env"], s["name"], conn=conn
                )
                ver_no = self.repo.next_version(switch.id, conn=conn)
                version = SwitchVersion(
                    switch_id=switch.id,
                    env=s["env"],
                    name=s["name"],
                    author=s["author"],
                    status=VersionStatus.DRAFT,
                    version=ver_no,
                    rollout_ratio=s["rollout_ratio"],
                    whitelist=s["whitelist"],
                    dependencies=s["dependencies"],
                    default_value=s["default_value"],
                )
                version = self.repo.insert_version(version, conn=conn)
                self.audit.record(
                    actor=actor,
                    action=AuditAction.IMPORT_CONFIG,
                    env=s["env"],
                    switch_name=s["name"],
                    version=version.version,
                    new_status=VersionStatus.DRAFT,
                    details={
                        "source": source,
                        "import_index": idx,
                        "batch_size": len(validated),
                    },
                    conn=conn,
                )
                imported.append(
                    {
                        "env": s["env"],
                        "name": s["name"],
                        "version": version.version,
                        "status": VersionStatus.DRAFT.value,
                    }
                )

        return {
            "schema_version": SCHEMA_VERSION,
            "source": source,
            "imported_by": actor,
            "count": len(imported),
            "imported": imported,
        }
