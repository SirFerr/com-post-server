from pydantic import BaseModel, ConfigDict, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=2, max_length=255)


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


class UserAdminUpdate(BaseModel):
    role: str | None = None
    is_blocked: bool | None = None
    ban_reason: str | None = None


class ComposterCreate(BaseModel):
    name: str
    device_id: str
    latitude: float
    longitude: float
    radius_m: float = 100


class ComposterUpdate(BaseModel):
    name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    radius_m: float | None = None
    is_available: bool | None = None


class TelemetryRequest(BaseModel):
    battery_level: int = Field(ge=0, le=100)
    fill_level: int = Field(ge=0, le=100)
    lock_state: str


class OrmModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)
