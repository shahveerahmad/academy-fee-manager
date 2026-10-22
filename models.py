from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from datetime import date
from database import Base

class StudentDB(Base):
    __tablename__ = "Students"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    class_name: Mapped[str]
    parent_phone: Mapped[str]
    monthly_fee: Mapped[int]


class FeeRecordDB(Base):
    __tablename__ = "fee_records"
    __table_args__ = (UniqueConstraint("student_id", "month"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("Students.id"))
    month: Mapped[str]
    amount_due: Mapped[int]
    status: Mapped[str] = mapped_column(default = "unpaid")
    paid_on: Mapped[date | None] = mapped_column(default=None)