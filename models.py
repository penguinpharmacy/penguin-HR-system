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
    """
    依勞基法第38條計算應有特休天數（週年制）：
      • 工作未滿6個月：0天
      • 滿6個月未滿1年：3天
      • 滿1年未滿2年：7天
      • 滿2年未滿3年：10天
      • 滿3年未滿5年：14天
      • 滿5年未滿10年：15天
      • 滿10年起，每滿1年加1天，上限30天
      • 若處於留職停薪，則特休歸零
    """
    if on_leave_suspend:
        return 0

    total_months = years * 12 + months

    # 6個月以下無特休
    if total_months < 6:
        return 0
    # 6個月以上未滿1年
    if total_months < 12:
        return 3
    # 1年以上未滿2年
    if total_months < 24:
        return 7
    # 2年以上未滿3年
    if total_months < 36:
        return 10
    # 3年以上未滿5年
    if total_months < 60:
        return 14
    # 5年以上未滿10年
    if total_months < 120:
        return 15

    # 10年以上，每滿1年加1天，上限30天
    # 例如：年資10年=>16天，11年=>17天，以此類推
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

