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
    RELEASE_ORDER_CREATE = "RELEASE_ORDER_CREATE"
    RELEASE_ORDER_PREVIEW = "RELEASE_ORDER_PREVIEW"
    RELEASE_ORDER_SUBMIT = "RELEASE_ORDER_SUBMIT"
    RELEASE_ORDER_APPROVE = "RELEASE_ORDER_APPROVE"
    RELEASE_ORDER_REJECT = "RELEASE_ORDER_REJECT"
    RELEASE_ORDER_EXECUTE = "RELEASE_ORDER_EXECUTE"
    RELEASE_ORDER_ROLLBACK = "RELEASE_ORDER_ROLLBACK"
    RELEASE_ORDER_CANCEL = "RELEASE_ORDER_CANCEL"
    RELEASE_ORDER_COPY = "RELEASE_ORDER_COPY"
    RELEASE_ORDER_EXPORT = "RELEASE_ORDER_EXPORT"
    RELEASE_ORDER_IMPORT = "RELEASE_ORDER_IMPORT"


class MigrationStatus(str, Enum):
    CREATED = "CREATED"
    PREVIEWED = "PREVIEWED"
    IMPORTED_DRAFT = "IMPORTED_DRAFT"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    ROLLED_BACK = "ROLLED_BACK"


class ReleaseOrderStatus(str, Enum):
    CREATED = "CREATED"
    PREVIEWED = "PREVIEWED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"
    EXECUTED = "EXECUTED"
    EXECUTE_FAILED = "EXECUTE_FAILED"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"
    CANCELLED = "CANCELLED"


VALID_RELEASE_TRANSITIONS: dict[ReleaseOrderStatus, list[ReleaseOrderStatus]] = {
    ReleaseOrderStatus.CREATED: [ReleaseOrderStatus.PREVIEWED, ReleaseOrderStatus.CANCELLED],
    ReleaseOrderStatus.PREVIEWED: [ReleaseOrderStatus.PENDING_APPROVAL, ReleaseOrderStatus.CANCELLED],
    ReleaseOrderStatus.PENDING_APPROVAL: [
        ReleaseOrderStatus.APPROVED,
        ReleaseOrderStatus.REJECTED,
        ReleaseOrderStatus.CANCELLED,
    ],
    ReleaseOrderStatus.APPROVED: [
        ReleaseOrderStatus.EXECUTING,
        ReleaseOrderStatus.CANCELLED,
    ],
    ReleaseOrderStatus.EXECUTING: [
        ReleaseOrderStatus.EXECUTED,
        ReleaseOrderStatus.EXECUTE_FAILED,
    ],
    ReleaseOrderStatus.EXECUTED: [
        ReleaseOrderStatus.ROLLING_BACK,
    ],
    ReleaseOrderStatus.EXECUTE_FAILED: [
        ReleaseOrderStatus.ROLLING_BACK,
        ReleaseOrderStatus.CANCELLED,
    ],
    ReleaseOrderStatus.ROLLING_BACK: [
        ReleaseOrderStatus.ROLLED_BACK,
        ReleaseOrderStatus.ROLLBACK_FAILED,
    ],
    ReleaseOrderStatus.ROLLED_BACK: [],
    ReleaseOrderStatus.ROLLBACK_FAILED: [],
    ReleaseOrderStatus.REJECTED: [],
    ReleaseOrderStatus.CANCELLED: [],
}


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
