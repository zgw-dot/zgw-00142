from enum import Enum


class VersionStatus(str, Enum):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    PUBLISHED = "PUBLISHED"
    ROLLED_BACK = "ROLLED_BACK"
    DEPRECATED = "DEPRECATED"

    @classmethod
    def active_flow(cls) -> list["VersionStatus"]:
        return [
            cls.DRAFT,
            cls.PENDING_APPROVAL,
            cls.PUBLISHED,
            cls.ROLLED_BACK,
            cls.DEPRECATED,
        ]

    @classmethod
    def is_editable(cls, status: "VersionStatus") -> bool:
        return status in (cls.DRAFT,)

    @classmethod
    def is_effective(cls, status: "VersionStatus") -> bool:
        return status == cls.PUBLISHED


class AuditAction(str, Enum):
    CREATE_DRAFT = "CREATE_DRAFT"
    EDIT_DRAFT = "EDIT_DRAFT"
    SUBMIT_APPROVAL = "SUBMIT_APPROVAL"
    APPROVE_AND_PUBLISH = "APPROVE_AND_PUBLISH"
    REJECT_APPROVAL = "REJECT_APPROVAL"
    ROLLBACK = "ROLLBACK"
    DEPRECATE = "DEPRECATE"
    IMPORT_CONFIG = "IMPORT_CONFIG"
    EXPORT_CONFIG = "EXPORT_CONFIG"
    MIGRATION_PACKAGE_CREATE = "MIGRATION_PACKAGE_CREATE"
    MIGRATION_PACKAGE_PREVIEW = "MIGRATION_PACKAGE_PREVIEW"
    MIGRATION_PACKAGE_IMPORT = "MIGRATION_PACKAGE_IMPORT"
    MIGRATION_PACKAGE_EXPORT = "MIGRATION_PACKAGE_EXPORT"


class MigrationStatus(str, Enum):
    CREATED = "CREATED"
    PREVIEWED = "PREVIEWED"
    IMPORTED_DRAFT = "IMPORTED_DRAFT"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    ROLLED_BACK = "ROLLED_BACK"


class ChangeType(str, Enum):
    NEW = "NEW"
    MODIFIED = "MODIFIED"
    UNCHANGED = "UNCHANGED"
    CONFLICT_DRAFT = "CONFLICT_DRAFT"
    CONFLICT_PENDING = "CONFLICT_PENDING"


VALID_TRANSITIONS: dict[VersionStatus, list[VersionStatus]] = {
    VersionStatus.DRAFT: [VersionStatus.PENDING_APPROVAL, VersionStatus.DEPRECATED],
    VersionStatus.PENDING_APPROVAL: [
        VersionStatus.DRAFT,
        VersionStatus.PUBLISHED,
        VersionStatus.DEPRECATED,
    ],
    VersionStatus.PUBLISHED: [VersionStatus.ROLLED_BACK, VersionStatus.DEPRECATED],
    VersionStatus.ROLLED_BACK: [VersionStatus.PUBLISHED, VersionStatus.DEPRECATED],
    VersionStatus.DEPRECATED: [],
}
