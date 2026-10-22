from logging import raiseExceptions
import httpx
from fastapi.responses import FileResponse, RedirectResponse
from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from datetime import date
from starlette.middleware.sessions import SessionMiddleware
from auth import check_password, require_access, SESSION_SECRET

from database import engine, SessionLocal, Base
from models import StudentDB, FeeRecordDB


Base.metadata.create_all(bind=engine)

app = FastAPI()

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    max_age=60 * 60 * 12,   # stay logged in for 12 hours
    same_site="lax",
)

class StudentIn(BaseModel):
    name: str
    class_name: str
    parent_phone: str
    monthly_fee: int


class StudentOut(StudentIn):
    id: int
    model_config = ConfigDict(from_attributes=True)

class FeeOut(BaseModel):
    fee_id: int
    student_name: str
    parent_phone: str
    month: str
    amount_due: int
    status: str

class ChildFee(BaseModel):
    fee_id: int
    student_name: str
    month: str
    amount_due: int

class FamilyPayIn(BaseModel):
    fee_ids: list[int]

class FamilyOut(BaseModel):
    parent_phone: str
    children: list[ChildFee]
    total: int


class ReportOut(BaseModel):
    month: str
    billed: int
    collected: int
    pending: int
    paid_count: int
    unpaid_count: int
    overdue_before: int
    total_outstanding: int


class StudentReportRow(BaseModel):
    student_name: str
    class_name: str
    month_fee: int
    status: str
    paid_on: str | None
    overdue_before: int
    total_due: int

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()



N8N_RECEIPT_URL = "http://localhost:5678/webhook/fee-paid"

def notify_n8n(payload: dict):
    try:
        httpx.post(N8N_RECEIPT_URL, json=payload, timeout=10)
    except httpx.HTTPError as error:
        print("Could not notify n8n:", error)

def normalize_phone(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    if digits.startswith("92"):
        digits = digits[2:]
    return digits.lstrip("0")

class LoginIn(BaseModel):
    password: str

@app.get("/login")
def login_page():
    return FileResponse("login.html")

@app.post("/login")
def login(data: LoginIn, request: Request):
    if not check_password(data.password):
        raise HTTPException(status_code=401, detail="Wrong password")
    request.session["owner"] = True
    return {"ok": True}

@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}

@app.get("/dashboard")
def dashboard(request: Request):
    if not request.session.get("owner"):
        return RedirectResponse("/login")
    return FileResponse("dashboard.html")

@app.get("/fees", response_model=list[FeeOut], dependencies=[Depends(require_access)])
def list_fees(month: str, db: Session = Depends(get_db)):
    rows = db.execute(
        select(FeeRecordDB, StudentDB)
        .join(StudentDB, FeeRecordDB.student_id == StudentDB.id)
        .where(FeeRecordDB.month == month)
    ).all()
    return [
        FeeOut(
            fee_id=f.id,
            student_name=s.name,
            parent_phone=s.parent_phone,
            month=f.month,
            amount_due=f.amount_due,
            status=f.status,
        )
        for f, s in rows
    ]

@app.get("/")
def home(): return {'message': 'Academy fee system is running'}

@app.post("/students", response_model=StudentOut, dependencies=[Depends(require_access)])
def add_student(student: StudentIn, db: Session = Depends(get_db)):
    new_student = StudentDB(**student.model_dump())
    db.add(new_student)
    db.commit()
    db.refresh(new_student)
    return new_student

@app.get("/students", response_model=list[StudentOut], dependencies=[Depends(require_access)])
def list_students(db: Session = Depends(get_db)):
    return db.scalars(select(StudentDB)).all()


@app.post("/fees/generate/{month}", dependencies=[Depends(require_access)])
def generate_fees(month: str, db: Session = Depends(get_db)):
    students = db.scalars(select(StudentDB)).all()
    created = 0
    for s in students:
        existing = db.scalar(select(FeeRecordDB).where(FeeRecordDB.student_id == s.id, FeeRecordDB.month == month,))
        if existing is None:
            db.add(FeeRecordDB(
                student_id = s.id,month=month,
                amount_due=s.monthly_fee
            ))
            created += 1
            db.commit()
    return {"month": month, "created": created}

@app.get("/fees/unpaid", response_model = list[FeeOut], dependencies=[Depends(require_access)])
def unpaid_fees(month: str, db:Session= Depends(get_db)):
    rows = db.execute(select(FeeRecordDB, StudentDB).join(StudentDB, FeeRecordDB.student_id == StudentDB.id).where(FeeRecordDB.month == month,FeeRecordDB.status == "unpaid")).all()
    return [
        FeeOut(
            fee_id=f.id,student_name = s.name,parent_phone= s.parent_phone,
            month=f.month,amount_due=f.amount_due,status=f.status
        )
        for (f, s) in rows
    ]

@app.post("/fees/{fee_id}/pay", dependencies=[Depends(require_access)])
def mark_paid(fee_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    fee = db.get(FeeRecordDB, fee_id)
    if fee is None:
        raise HTTPException(status_code=404, detail="Fee record not found")
    if fee.status == "paid":
        raise HTTPException(status_code=400, detail="Already marked as paid")

    fee.status = "paid"
    fee.paid_on = date.today()
    db.commit()

    student = db.get(StudentDB, fee.student_id)
    background_tasks.add_task(notify_n8n, {
        "parent_phone": student.parent_phone,
        "children": [{
            "student_name": student.name,
            "month": fee.month,
            "amount": fee.amount_due,
        }],
        "total": fee.amount_due,
        "paid_on": str(fee.paid_on),
    })
    return {"fee_id": fee.id, "status": fee.status, "paid_on": fee.paid_on}

@app.get("/families/unpaid", response_model = list[FamilyOut], dependencies=[Depends(require_access)])
def unpaid_by_family(month: str, db:Session = Depends(get_db)):
    rows = db.execute(select(FeeRecordDB, StudentDB).join(StudentDB, FeeRecordDB.student_id == StudentDB.id).where(FeeRecordDB.month <= month, FeeRecordDB.status == "unpaid").order_by(FeeRecordDB.month)).all()
    families = {}
    for fee, student in rows:
        key = normalize_phone(student.parent_phone)
        if key not in families:
            families[key] = {
                "parent_phone": student.parent_phone,
                "children": [],
                "total": 0,
            }
        families[key]["children"].append({
                "fee_id": fee.id,
                "student_name": student.name,
                "month": fee.month,
                "amount_due": fee.amount_due,
            })
        families[key]["total"] += fee.amount_due
    return list(families.values())

@app.post("/families/pay", dependencies=[Depends(require_access)])
def pay_family(data: FamilyPayIn, background_tasks: BackgroundTasks,
               db: Session = Depends(get_db)):
    fee_ids = list(dict.fromkeys(data.fee_ids))   # removes duplicates, keeps order
    if not fee_ids:
        raise HTTPException(status_code=400, detail="No fees selected")

    # Pass 1: check every fee, change nothing yet
    fees = []
    for fee_id in fee_ids:
        fee = db.get(FeeRecordDB, fee_id)
        if fee is None:
            raise HTTPException(status_code=404, detail="Fee not found")
        if fee.status == "paid":
            raise HTTPException(status_code=400, detail="Fee already paid")
        fees.append(fee)

    # Pass 2: change everything, then save once
    today = date.today()
    children = []
    total = 0
    for fee in fees:
        student = db.get(StudentDB, fee.student_id)
        fee.status = "paid"
        fee.paid_on = today
        children.append({
            "student_name": student.name,
            "month": fee.month,
            "amount": fee.amount_due,
        })
        total += fee.amount_due
        parent_phone = student.parent_phone

    db.commit()

    background_tasks.add_task(notify_n8n, {
        "parent_phone": parent_phone,
        "children": children,
        "total": total,
        "paid_on": str(today),
    })
    return {"settled": len(fees), "total": total}

@app.get("/report", response_model=ReportOut, dependencies=[Depends(require_access)])
def monthly_report(month: str, db: Session = Depends(get_db)):
    fees = db.scalars(
        select(FeeRecordDB).where(FeeRecordDB.month == month)
    ).all()

    billed = 0
    collected = 0
    paid_count = 0
    unpaid_count = 0

    overdue = db.scalar(
    select(func.coalesce(func.sum(FeeRecordDB.amount_due), 0))
    .where(FeeRecordDB.month < month, FeeRecordDB.status == "unpaid")
)

    for fee in fees:
        billed += fee.amount_due
        if fee.status == "paid":
            collected += fee.amount_due
            paid_count += 1

    return {
        "month": month,
        "billed": billed,
        "collected": collected,
        "pending": billed - collected,
        "paid_count": paid_count,
        "unpaid_count": unpaid_count,
        "overdue_before": overdue,
        "total_outstanding": (billed - collected) + overdue,
    }

@app.get("/report/students", response_model=list[StudentReportRow],
         dependencies=[Depends(require_access)])
def student_report(month: str, db: Session = Depends(get_db)):
    students = db.scalars(select(StudentDB).order_by(StudentDB.name)).all()

    # student_id -> this month's fee record
    this_month = {
        f.student_id: f
        for f in db.scalars(
            select(FeeRecordDB).where(FeeRecordDB.month == month)
        ).all()
    }

    # student_id -> total of unpaid fees from earlier months
    overdue_rows = db.execute(
        select(FeeRecordDB.student_id, func.sum(FeeRecordDB.amount_due))
        .where(FeeRecordDB.month < month, FeeRecordDB.status == "unpaid")
        .group_by(FeeRecordDB.student_id)
    ).all()
    overdue = {student_id: total for student_id, total in overdue_rows}

    rows = []
    for student in students:
        fee = this_month.get(student.id)   # None if no record this month
        month_fee = fee.amount_due if fee else 0                      # (a)
        status = fee.status if fee else "no fee"                      # (b)
        paid_on = str(fee.paid_on) if fee and fee.paid_on else None   # (c)
        overdue_before = overdue.get(student.id, 0)                   # (d)
        this_month_owed = month_fee if status == "unpaid" else 0      # (e)
        total_due = this_month_owed + overdue_before
        rows.append({
            "student_name": student.name,
            "class_name": student.class_name,
            "month_fee": month_fee,
            "status": status,
            "paid_on": paid_on,
            "overdue_before": overdue_before,
            "total_due": total_due,
        })
    return rows