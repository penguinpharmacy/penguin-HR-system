from datetime import date, datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

# ---------------------------
# 工具函式：計算年資與各種假別
# ---------------------------

def calculate_seniority(start_date):
    """回傳年資（整年, 剩餘月）"""
    today = date.today()
    years = today.year - start_date.year
    months = today.month - start_date.month
    if today.day < start_date.day:
        months -= 1
    if months < 0:
        years -= 1
        months += 12
    return years, months

def entitled_leave_days(years, months, on_leave_suspend):
    """
    依勞基法第38條計算應有特休天數（週年制）
    """
    if on_leave_suspend:
        return 0

    total_months = years * 12 + months

    if total_months < 6:
        return 0
    if total_months < 12:
        return 3
    if total_months < 24:
        return 7
    if total_months < 36:
        return 10
    if total_months < 60:
        return 14
    if total_months < 120:
        return 15

    extra_years = years - 9
    days = 15 + extra_years
    return min(days, 30)

def entitled_sick_days(years, months):
    """依勞基法，每年可用病假上限為30天"""
    return 30

def entitled_personal_days(years, months):
    """依勞基法，每年可用事假上限為14天"""
    return 14

def entitled_marriage_days():
    """依勞基法，婚假一次給8天"""
    return 8


# ---------------------------
# 資料表 Models
# ---------------------------

class Employee(db.Model):
    __tablename__ = "employees"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    on_leave_suspend = db.Column(db.Boolean, default=False)

    # 關聯到請假紀錄
    leaves = db.relationship("Leave", backref="employee", lazy=True)


class Leave(db.Model):
    __tablename__ = "leaves"
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employees.id"), nullable=False)

    start_date = db.Column(db.Date, nullable=False)
    end_date   = db.Column(db.Date, nullable=False)
    hours      = db.Column(db.Float, nullable=False)  # 請假時數，最小 0.5
    note       = db.Column(db.String(200))

    # 狀態與軟刪支援
    # pending / approved / rejected / canceled
    status     = db.Column(db.String(20), nullable=False, default="approved")
    deleted    = db.Column(db.Boolean, nullable=False, default=False)
    deleted_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow
    )
