"""Account Management - admin-only user lifecycle endpoints."""

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
from typing import Any, Optional
from pydantic import BaseModel, Field
import io
import pandas as pd

from app.core.dependencies import ROLE_MAP, require_roles
from app.core.database import get_supabase

router = APIRouter(prefix="/accounts", tags=["IAM - Account Management"])


def _svc():
    svc = get_supabase(service_role=True)
    if not svc:
        raise HTTPException(status_code=500, detail="Missing SUPABASE_SERVICE_ROLE_KEY")
    return svc


def _role_slug(role_id: int | None) -> str:
    name = ROLE_MAP.get(role_id)
    if name == "Admin":
        return "admin"
    if name == "Lecturer":
        return "lecturer"
    return "student"


def _faculty_name(svc, faculty_id: int | None) -> str | None:
    if not faculty_id:
        return None
    q = svc.table("faculties").select("name").eq("id", faculty_id).limit(1).execute()
    if q.data:
        return q.data[0].get("name")
    return None


def _department_info(svc, department_id: int | None) -> dict[str, Any] | None:
    if not department_id:
        return None
    q = (
        svc.table("departments")
        .select("id, name, from_faculty")
        .eq("id", department_id)
        .limit(1)
        .execute()
    )
    if not q.data:
        return None
    row = q.data[0]
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "from_faculty": row.get("from_faculty"),
    }


def _norm_gpa(value: Any) -> float | None:
    if value is None or value == "":
        return None
    num = round(float(value), 2)
    if num < 0 or num > 10:
        raise HTTPException(status_code=400, detail="GPA must be in 0..10 scale")
    return num


def _role_id_from_any(role: Any) -> int:
    if role is None:
        return 3
    text = str(role).strip().lower()
    if text in {"1", "admin"}:
        return 1
    if text in {"2", "lecturer"}:
        return 2
    if text in {"3", "student"}:
        return 3
    raise HTTPException(status_code=400, detail=f"Invalid role value: {role}")


class AccountCreate(BaseModel):
    email: str
    password: str = Field(min_length=6)
    role_id: int = Field(description="1=Admin, 2=Lecturer, 3=Student")
    full_name: str | None = None
    is_active: bool = True
    faculty_id: int | None = None
    department_id: int | None = None
    student_id: str | None = None
    lecturer_id: str | None = None
    gender: str | None = None
    major: str | None = None
    enrolled_year: int | None = None
    date_of_birth: str | None = None
    current_gpa: float | None = None
    cumulative_gpa: float | None = None
    qualification: str | None = None
    phone_number: str | None = None
    student_class: str | None = Field(None, max_length=120, description="Cohort / group label (student_profiles.class)")


class AccountUpdate(BaseModel):
    full_name: str | None = None
    email: str | None = None
    role_id: int | None = None
    is_active: bool | None = None
    faculty_id: int | None = None
    department_id: int | None = None
    student_id: str | None = None
    lecturer_id: str | None = None
    gender: str | None = None
    major: str | None = None
    enrolled_year: int | None = None
    date_of_birth: str | None = None
    current_gpa: float | None = None
    cumulative_gpa: float | None = None
    qualification: str | None = None
    phone_number: str | None = None
    student_class: str | None = Field(None, max_length=120)


class AccountOut(BaseModel):
    id: str
    email: str | None = None
    full_name: str | None = None
    role_id: int | None = None
    role: str
    is_active: bool
    faculty_name: str | None = None
    department_id: int | None = None
    department_name: str | None = None
    student_id: str | None = None
    lecturer_id: str | None = None
    gender: str | None = None
    major: str | None = None
    enrolled_year: int | None = None
    current_gpa: float | None = None
    cumulative_gpa: float | None = None
    qualification: str | None = None
    phone_number: str | None = None
    student_class: str | None = None
    created_at: str | None = None


class FacultyOut(BaseModel):
    id: int
    name: str | None = None


class DepartmentOut(BaseModel):
    id: int
    name: str | None = None
    from_faculty: int | None = None


class StudentClassOut(BaseModel):
    value: str


def _as_account_out(svc, user_row: dict[str, Any]) -> dict[str, Any]:
    user_id = user_row["user_id"]
    role_id = user_row.get("role_id")
    role = _role_slug(role_id)

    student = None
    lecturer = None
    if role_id == 3:
        q = svc.table("student_profiles").select("*").eq("user_id", user_id).limit(1).execute()
        student = q.data[0] if q.data else None
    elif role_id == 2:
        q = svc.table("lecturer_profiles").select("*").eq("user_id", user_id).limit(1).execute()
        lecturer = q.data[0] if q.data else None

    faculty_id = None
    department_id = None
    department_name = None
    if student:
        faculty_id = student.get("faculty_id")
        department_id = student.get("department_id")
        dept = _department_info(svc, department_id)
        department_name = (dept or {}).get("name")
    if lecturer:
        department = _department_info(svc, lecturer.get("department_id"))
        faculty_id = (department or {}).get("from_faculty") or lecturer.get("faculty_id")
        department_id = lecturer.get("department_id")
        department_name = (department or {}).get("name")

    return {
        "id": user_id,
        "email": user_row.get("email"),
        "full_name": user_row.get("full_name"),
        "role_id": role_id,
        "role": role,
        "is_active": bool(user_row.get("is_active", True)),
        "faculty_name": _faculty_name(svc, faculty_id),
        "department_id": int(department_id) if department_id is not None else None,
        "department_name": department_name,
        "student_id": (student or {}).get("student_id"),
        "lecturer_id": (lecturer or {}).get("lecturer_id"),
        "gender": (student or lecturer or {}).get("gender"),
        "major": (student or {}).get("major"),
        "enrolled_year": (student or {}).get("enrolled_year"),
        "current_gpa": (student or {}).get("current_gpa"),
        "cumulative_gpa": (student or {}).get("cumulative_gpa"),
        "qualification": (lecturer or {}).get("qualification"),
        "phone_number": (student or lecturer or {}).get("phone_number"),
        "student_class": (student or {}).get("class"),
        "created_at": user_row.get("created_at"),
    }


@router.get("/meta/faculties", response_model=list[FacultyOut], summary="List faculties")
async def list_faculties(user=Depends(require_roles(["Admin"]))):
    svc = _svc()
    rows = svc.table("faculties").select("id, name").order("id").execute().data or []
    return rows


@router.get("/meta/departments", response_model=list[DepartmentOut], summary="List departments")
async def list_departments(user=Depends(require_roles(["Admin"]))):
    svc = _svc()
    rows = (
        svc.table("departments")
        .select("id, name, from_faculty")
        .order("id")
        .execute()
        .data
        or []
    )
    return rows


@router.get("/meta/student-classes", response_model=list[StudentClassOut], summary="List distinct student classes")
async def list_student_classes(user=Depends(require_roles(["Admin", "Lecturer"]))):
    svc = _svc()
    rows = svc.table("student_profiles").select("class").execute().data or []
    values = sorted({str(r.get("class", "")).strip() for r in rows if str(r.get("class", "")).strip()})
    return [{"value": v} for v in values]


@router.post("/", response_model=AccountOut, status_code=status.HTTP_201_CREATED, summary="Create account")
async def create_account(account: AccountCreate, user=Depends(require_roles(["Admin"]))):
    svc = _svc()
    try:
        auth_res = svc.auth.admin.create_user(
            {"email": account.email, "password": account.password, "email_confirm": True}
        )
        if not auth_res.user:
            raise HTTPException(status_code=400, detail="Cannot create auth user")
        user_id = auth_res.user.id

        db_ins = (
            svc.table("users")
            .insert(
                {
                    "user_id": user_id,
                    "email": account.email,
                    "full_name": account.full_name,
                    "role_id": account.role_id,
                    "is_active": account.is_active,
                    "created_by": user["user_id"],
                }
            )
            .execute()
        )
        if not db_ins.data:
            raise HTTPException(status_code=400, detail="Insert DB failed")

        if account.role_id == 2:
            svc.table("lecturer_profiles").upsert(
                {
                    "user_id": user_id,
                    "lecturer_id": account.lecturer_id,
                    "gender": account.gender,
                    "qualification": account.qualification,
                    "phone_number": account.phone_number,
                    "faculty_id": account.faculty_id,
                    "department_id": account.department_id,
                }
            ).execute()
        elif account.role_id == 3:
            cohort = None
            if account.student_class is not None:
                cohort = str(account.student_class).strip() or None
            svc.table("student_profiles").upsert(
                {
                    "user_id": user_id,
                    "student_id": account.student_id,
                    "gender": account.gender,
                    "major": account.major,
                    "enrolled_year": account.enrolled_year,
                    "date_of_birth": account.date_of_birth,
                    "current_gpa": _norm_gpa(account.current_gpa),
                    "cumulative_gpa": _norm_gpa(account.cumulative_gpa),
                    "phone_number": account.phone_number,
                    "faculty_id": account.faculty_id,
                    "department_id": account.department_id,
                    "class": cohort,
                }
            ).execute()

        row = svc.table("users").select("*").eq("user_id", user_id).limit(1).execute()
        return _as_account_out(svc, row.data[0])
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/", response_model=list[AccountOut], summary="List accounts")
async def list_accounts(
    role_id: Optional[int] = None,
    search: Optional[str] = None,
    student_class: Optional[str] = None,
    faculty_id: Optional[int] = None,
    department_id: Optional[int] = None,
    user=Depends(require_roles(["Admin"])),
):
    svc = _svc()
    query = svc.table("users").select("*").order("created_at", desc=True)
    if role_id:
        query = query.eq("role_id", role_id)
    filtered_uids: set[str] | None = None
    if faculty_id is not None or department_id is not None:
        candidate_uids: set[str] = set()
        sp_q = svc.table("student_profiles").select("user_id")
        if faculty_id is not None:
            sp_q = sp_q.eq("faculty_id", faculty_id)
        if department_id is not None:
            sp_q = sp_q.eq("department_id", department_id)
        for r in sp_q.execute().data or []:
            uid = r.get("user_id")
            if uid:
                candidate_uids.add(uid)

        lp_q = svc.table("lecturer_profiles").select("user_id")
        if faculty_id is not None:
            lp_q = lp_q.eq("faculty_id", faculty_id)
        if department_id is not None:
            lp_q = lp_q.eq("department_id", department_id)
        for r in lp_q.execute().data or []:
            uid = r.get("user_id")
            if uid:
                candidate_uids.add(uid)

        filtered_uids = candidate_uids

    if student_class and student_class.strip():
        token = student_class.strip()
        sp = svc.table("student_profiles").select("user_id").ilike("class", f"%{token}%").execute()
        class_uids = {r["user_id"] for r in (sp.data or []) if r.get("user_id")}
        if filtered_uids is None:
            filtered_uids = class_uids
        else:
            filtered_uids = filtered_uids.intersection(class_uids)
        if not filtered_uids:
            return []
    if filtered_uids is not None:
        if not filtered_uids:
            return []
        query = query.in_("user_id", list(filtered_uids))
    if search:
        token = search.strip()
        query = query.or_(f"email.ilike.%{token}%,full_name.ilike.%{token}%")
    rows = query.execute().data or []
    return [_as_account_out(svc, r) for r in rows]


@router.get("/{account_id}", response_model=AccountOut, summary="Get account by user_id")
async def get_account(account_id: str, user=Depends(require_roles(["Admin"]))):
    svc = _svc()
    res = svc.table("users").select("*").eq("user_id", account_id).limit(1).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="User not found")
    return _as_account_out(svc, res.data[0])


@router.patch("/{account_id}", response_model=AccountOut, summary="Update account/profile")
async def update_account(
    account_id: str,
    payload: AccountUpdate,
    user=Depends(require_roles(["Admin"])),
):
    svc = _svc()
    existing = svc.table("users").select("*").eq("user_id", account_id).limit(1).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="User not found")
    row = existing.data[0]
    role_id = payload.role_id if payload.role_id is not None else row.get("role_id")

    user_update = {}
    for key in ("full_name", "email", "role_id", "is_active"):
        value = getattr(payload, key)
        if value is not None:
            user_update[key] = value
    if user_update:
        updated = svc.table("users").update(user_update).eq("user_id", account_id).execute()
        if not updated.data:
            raise HTTPException(status_code=500, detail="Failed to update user")

    if role_id == 2:
        svc.table("lecturer_profiles").upsert(
            {
                "user_id": account_id,
                "lecturer_id": payload.lecturer_id,
                "gender": payload.gender,
                "qualification": payload.qualification,
                "phone_number": payload.phone_number,
                "faculty_id": payload.faculty_id,
                "department_id": payload.department_id,
            }
        ).execute()
    elif role_id == 3:
        cohort = None
        if payload.student_class is not None:
            cohort = str(payload.student_class).strip() or None
        svc.table("student_profiles").upsert(
            {
                "user_id": account_id,
                "student_id": payload.student_id,
                "gender": payload.gender,
                "major": payload.major,
                "enrolled_year": payload.enrolled_year,
                "date_of_birth": payload.date_of_birth,
                "current_gpa": _norm_gpa(payload.current_gpa),
                "cumulative_gpa": _norm_gpa(payload.cumulative_gpa),
                "phone_number": payload.phone_number,
                "faculty_id": payload.faculty_id,
                "department_id": payload.department_id,
                "class": cohort,
            }
        ).execute()

    fresh = svc.table("users").select("*").eq("user_id", account_id).limit(1).execute()
    return _as_account_out(svc, fresh.data[0])


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete account")
async def delete_account(account_id: str, user=Depends(require_roles(["Admin"]))):
    svc = _svc()
    svc.table("users").delete().eq("user_id", account_id).execute()
    return


@router.post("/import", status_code=status.HTTP_200_OK, summary="Bulk import accounts via CSV/XLSX")
async def import_accounts(
    sheet: UploadFile = File(..., description="CSV/XLSX with account/profile columns"),
    user=Depends(require_roles(["Admin"])),
):
    filename = (sheet.filename or "").lower()
    try:
        raw = await sheet.read()
        if filename.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(raw))
        elif filename.endswith(".xlsx") or filename.endswith(".xls"):
            df = pd.read_excel(io.BytesIO(raw))
        else:
            raise HTTPException(status_code=400, detail="Only .csv, .xlsx, .xls are supported")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid sheet file: {e}")

    expected = {"email", "password"}
    missing = [c for c in expected if c not in df.columns]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing required columns: {', '.join(missing)}")

    created = 0
    failed = 0
    errors: list[dict[str, Any]] = []
    for idx, row in df.fillna("").iterrows():
        try:
            role_id = _role_id_from_any(row.get("role") or row.get("role_id"))
            payload = AccountCreate(
                email=str(row.get("email", "")).strip(),
                password=str(row.get("password", "")).strip(),
                role_id=role_id,
                full_name=(str(row.get("full_name", "")).strip() or None),
                is_active=str(row.get("is_active", "true")).strip().lower() not in {"false", "0", "no"},
                faculty_id=int(row["faculty_id"]) if str(row.get("faculty_id", "")).strip() else None,
                department_id=int(row["department_id"]) if str(row.get("department_id", "")).strip() else None,
                student_id=(str(row.get("student_id", "")).strip() or None),
                lecturer_id=(str(row.get("lecturer_id", "")).strip() or None),
                gender=(str(row.get("gender", "")).strip() or None),
                major=(str(row.get("major", "")).strip() or None),
                enrolled_year=int(row["enrolled_year"]) if str(row.get("enrolled_year", "")).strip() else None,
                date_of_birth=(str(row.get("date_of_birth", "")).strip() or None),
                current_gpa=_norm_gpa(row.get("current_gpa")),
                cumulative_gpa=_norm_gpa(row.get("cumulative_gpa")),
                qualification=(str(row.get("qualification", "")).strip() or None),
                phone_number=(str(row.get("phone_number", "")).strip() or None),
                student_class=(
                    str(row.get("student_class", "") or row.get("class", "")).strip() or None
                ),
            )
            await create_account(payload, user)
            created += 1
        except Exception as e:
            failed += 1
            errors.append({"row": int(idx) + 2, "error": str(e)})

    return {"total": int(len(df.index)), "created": created, "failed": failed, "errors": errors[:30]}