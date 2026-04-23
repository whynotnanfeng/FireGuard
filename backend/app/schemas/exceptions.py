from fastapi import Request, status
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from jose import JWTError
from sqlmodel import SQLModel


async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "code": 422,
            "message": "Validation error",
            "detail": exc.errors(),
        },
    )


async def jwt_exception_handler(request: Request, exc: JWTError):
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={
            "code": 401,
            "message": "Invalid or expired token",
            "detail": str(exc),
        },
    )


async def sqlmodel_exception_handler(request: Request, exc: SQLModel):
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "code": 500,
            "message": "Database error",
            "detail": "An internal database error occurred",
        },
    )
