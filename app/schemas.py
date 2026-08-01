import re

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    device_name: str = Field(default="Unknown device", max_length=255)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=32, max_length=512)


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=2, max_length=255)

    @field_validator("password")
    @classmethod
    def strong_password(cls, value: str) -> str:
        if not re.search(r"[A-Z]", value) or not re.search(r"[a-z]", value) or not re.search(r"\d", value):
            raise ValueError("Password must contain upper and lower case letters and a digit")
        return value

    @field_validator("full_name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if len(cleaned) < 2:
            raise ValueError("Full name is too short")
        return cleaned


class AccessRequest(BaseModel):
    latitude: float
    longitude: float


class CommandAck(BaseModel):
    command_id: str
    status: str


class ModerateRequest(BaseModel):
    approved: bool
    violation_reason: str | None = None
    comment: str | None = None
    incident_kind: str | None = Field(default=None, max_length=80)
    create_incident: bool = False
    annotations: list[dict[str, float | str]] = Field(default_factory=list, max_length=100)

    @field_validator("annotations")
    @classmethod
    def valid_annotations(cls, value: list[dict[str, float | str]]) -> list[dict[str, float | str]]:
        for box in value:
            for key in ("x", "y", "width", "height"):
                number = float(box.get(key, -1))
                if number < 0 or number > 1:
                    raise ValueError("Annotation coordinates must be normalized from 0 to 1")
            if float(box["width"]) <= 0 or float(box["height"]) <= 0:
                raise ValueError("Annotation dimensions must be positive")
        return value


class UserAdminUpdate(BaseModel):
    current_password: str = Field(min_length=8, max_length=128)
    role: str | None = None
    is_blocked: bool | None = None
    ban_reason: str | None = None
    ban_until: str | None = None
    warning_message: str | None = Field(default=None, max_length=500)


class ScoreAdjustment(BaseModel):
    current_password: str = Field(min_length=8, max_length=128)
    amount: int = Field(ge=-10_000, le=10_000)
    reason: str = Field(min_length=3, max_length=255)


class IncidentCreate(BaseModel):
    composter_id: str | None = None
    kind: str = Field(min_length=2, max_length=80)
    severity: str = Field(default="MEDIUM", pattern="^(LOW|MEDIUM|HIGH|CRITICAL)$")
    title: str = Field(default="", max_length=255)
    description: str = Field(default="", max_length=5000)
    assigned_to: str | None = None
    due_at: str | None = None


class IncidentUpdate(BaseModel):
    status: str | None = Field(default=None, pattern="^(OPEN|IN_PROGRESS|RESOLVED|CANCELLED)$")
    severity: str | None = Field(default=None, pattern="^(LOW|MEDIUM|HIGH|CRITICAL)$")
    assigned_to: str | None = None
    description: str | None = Field(default=None, max_length=5000)


class MaintenanceCreate(BaseModel):
    action: str = Field(min_length=3, max_length=120)
    notes: str = Field(default="", max_length=5000)


class MaintenanceModeRequest(BaseModel):
    enabled: bool


class ComposterCreate(BaseModel):
    name: str
    device_id: str
    latitude: float
    longitude: float
    radius_m: float = 100
    secret: str | None = Field(default=None, min_length=32, max_length=128)
    is_available: bool = True


class ComposterUpdate(BaseModel):
    current_password: str | None = Field(default=None, min_length=8, max_length=128)
    name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    radius_m: float | None = None
    is_available: bool | None = None


class FullStateRequest(BaseModel):
    needs_emptying: bool


class TelemetryRequest(BaseModel):
    battery_level: int = Field(ge=0, le=100)
    fill_level: int = Field(ge=0, le=100)
    lock_state: str


class DebugCommandRequest(BaseModel):
    action: str


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=8, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def strong_password(cls, value: str) -> str:
        if not re.search(r"[A-Z]", value) or not re.search(r"[a-z]", value) or not re.search(r"\d", value):
            raise ValueError("Password must contain upper and lower case letters and a digit")
        return value


class PasswordConfirmation(BaseModel):
    current_password: str = Field(min_length=8, max_length=128)


class OrmModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)
