from datetime import date

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
    """依勞基法和留停狀態計算應有特休天數"""
    if on_leave_suspend:
        return 0
    total_months = years * 12 + months
    if total_months < 6:
        return 0
    if total_months < 12:
        return 3
    if total_months < 24:
        return 7
    # 2年以上
    days = 10 + (years - 2)
    return min(days, 30)
