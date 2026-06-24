from __future__ import annotations
import uuid
from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class OpportunityType(str, Enum):
    NEW_BUSINESS = "new_business"
    EXISTING_CLIENT_ALERT = "existing_client_alert"
    CONFLICT_FLAG = "conflict_flag"
    NOT_RELEVANT = "not_relevant"


class OpportunityStatus(str, Enum):
    NEW = "new"
    REVIEWED = "reviewed"
    PURSUING = "pursuing"
    PASSED = "passed"


class PracticeArea(str, Enum):
    LITIGATION = "litigation"
    CORPORATE = "corporate"
    EMPLOYMENT = "employment"
    INTELLECTUAL_PROPERTY = "intellectual_property"
    ENVIRONMENTAL = "environmental"
    TAX = "tax"
    REAL_ESTATE = "real_estate"
    BANKRUPTCY = "bankruptcy"
    OTHER = "other"


# ---------------------------------------------------------------------------
# Docket models
# ---------------------------------------------------------------------------

class Party(BaseModel):
    name: str
    role: str                           # plaintiff, defendant, appellant, etc.
    normalized_name: Optional[str] = None


class DocketEntry(BaseModel):
    entry_number: int
    date_filed: datetime
    description: str
    document_url: Optional[str] = None


class Docket(BaseModel):
    case_id: str
    case_name: str
    case_number: str
    court: str
    jurisdiction: str                   # e.g. "S.D.N.Y.", "Cal. Super. Ct."
    date_filed: datetime
    practice_area: Optional[PracticeArea] = None
    parties: list[Party] = Field(default_factory=list)
    claims: list[str] = Field(default_factory=list)
    summary: Optional[str] = None
    source: str                         # "courtlistener", "docket_alarm", etc.
    source_url: Optional[str] = None
    raw_text: Optional[str] = None


# ---------------------------------------------------------------------------
# Client models
# ---------------------------------------------------------------------------

class Client(BaseModel):
    client_id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    industry: Optional[str] = None
    practice_areas: list[PracticeArea] = Field(default_factory=list)
    jurisdictions: list[str] = Field(default_factory=list)
    relationship_partner: Optional[str] = None
    notes: Optional[str] = None


class EntityMatch(BaseModel):
    client: Client
    matched_party: Party
    confidence: float                   # 0.0 – 1.0
    match_reason: str


class Conflict(BaseModel):
    client: Client
    adverse_party: Party
    conflict_description: str


# ---------------------------------------------------------------------------
# Opportunity models
# ---------------------------------------------------------------------------

class Opportunity(BaseModel):
    opportunity_id: Optional[str] = None
    docket: Docket
    opportunity_type: OpportunityType
    matched_clients: list[EntityMatch] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)
    status: OpportunityStatus = OpportunityStatus.NEW
    summary: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    assigned_to: Optional[str] = None


class ConflictCheck(BaseModel):
    """Immutable audit record created every time check_conflicts is called."""
    check_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_at: datetime = Field(default_factory=datetime.utcnow)
    caller_id: str
    parties_queried: list[str]
    results: list[Conflict]
    result_summary: str  # "CLEAR" | "CONFLICT_FOUND"
    action_taken: Optional[str] = None
    signed_off_by: Optional[str] = None
    signed_off_at: Optional[datetime] = None
