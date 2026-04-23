from typing import Any, Generic, Optional, TypeVar
from pydantic import BaseModel


T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    code: int = 200
    data: Optional[T] = None
    message: str = "success"

    @classmethod
    def success(cls, data: T = None, message: str = "success") -> "ApiResponse[T]":
        return cls(code=200, data=data, message=message)

    @classmethod
    def error(cls, code: int = 400, message: str = "error", data: T = None) -> "ApiResponse[T]":
        return cls(code=code, data=data, message=message)


class ErrorResponse(BaseModel):
    code: int
    message: str
    detail: Optional[str] = None
