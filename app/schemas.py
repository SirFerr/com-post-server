import re

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


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


class UserAdminUpdate(BaseModel):
    current_password: str = Field(min_length=8, max_length=128)
    role: str | None = None
    is_blocked: bool | None = None
    ban_reason: str | None = None


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
