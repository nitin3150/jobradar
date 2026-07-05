from app.models.application import Application, ApplicationStatus
from app.models.company import Company, CompanyStatus, FundingStage
from app.models.job import ATSType, Job, JobStatus
from app.models.outreach import OutreachMessage, OutreachType
from app.models.qa_bank import AnswerType, QABankEntry
from app.models.resume import MAX_RESUME_BYTES, MAX_TAGS_PER_RESUME, Resume

# `Preferences` imports last so it doesn't shadow the lowercase `settings`
# singleton from app.config — users of `app.models.Preferences` are explicit.
from app.models.preferences import (  # noqa: E402
    DEFAULT_JOB_FIT_THRESHOLD,
    DEFAULT_REVIEW_WINDOW_HOURS,
    DEFAULT_SEND_FOLLOWUP_EMAILS,
    DEFAULT_TARGET_ROLES,
    Preferences,
)

__all__ = [
    "Application",
    "ApplicationStatus",
    "AnswerType",
    "ATSType",
    "Company",
    "CompanyStatus",
    "DEFAULT_JOB_FIT_THRESHOLD",
    "DEFAULT_REVIEW_WINDOW_HOURS",
    "DEFAULT_SEND_FOLLOWUP_EMAILS",
    "DEFAULT_TARGET_ROLES",
    "FundingStage",
    "Job",
    "JobStatus",
    "MAX_RESUME_BYTES",
    "MAX_TAGS_PER_RESUME",
    "OutreachMessage",
    "OutreachType",
    "Preferences",
    "QABankEntry",
    "Resume",
]
