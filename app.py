from flask import Flask, render_template, request, redirect, url_for, abort, jsonify, g, make_response, send_file
from models import (
    calculate_seniority,
    entitled_leave_days,
    entitled_sick_days,
    entitled_personal_days,
    entitled_marriage_days,
)
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
import os
import psycopg
import socket
from urllib.parse import urlparse
from types import SimpleNamespace
import base64
import io
import csv
import zipfile
import json

app = Flask(__name__)

# 可調整「到期提醒視窗」天數（預設 60 天）
ALERT_WINDOW_DAYS = int(os.environ.get("LEAVE_EXPIRY_ALERT_DAYS", "60"))

# 制度設定：calendar=曆年制 / anniversary=週年制
LEAVE_POLICY = os.environ.get("LEAVE_POLICY", "anniversary")

# 週年制遞延（月數）：0 = 不遞延（到期折現）；12 = 遞延一年
ANNIV_CARRYOVER_MONTHS = int(os.environ.get("ANNIV_CARRYOVER_MONTHS", "0"))

# ========== 基本認證（可關閉：不設定 ADMIN_USER/PASS 即停用） ==========
ADMIN_USER = os.environ.get('ADMIN_USER')
ADMIN_PASS = os.environ.get('ADMIN_PASS')
BACKUP_TOKEN = os.environ.get('BACKUP_TOKEN')  # /admin/backup 用

def _parse_basic_auth(auth_header: str):
    if not auth_header or not auth_header.startswith('Basic '):
        return None, None
    try:
        raw = base64.b64decode(auth_header[6:]).decode('utf-8')
        username, password = raw.split(':', 1)
        return username, password
    except Exception:
        return None, None

@app.before_request
def _guard():
    # 未設定帳密 → 不啟用認證（方便本機/開發）
    if not ADMIN_USER or not ADMIN_PASS:
        g.current_user = 'dev'
        return
    # 靜態不擋
    if request.endpoint in ('static',):
        return
    auth = request.headers.get('Authorization') or ''
    u, p = _parse_basic_auth(auth)
    if u == ADMIN_USER and p == ADMIN_PASS:
        g.current_user = u
        return
    resp = make_response('Authentication required', 401)
    resp.headers['WWW-Authenticate'] = 'Basic realm="HR System"'
    return resp

# -------------------------
# DB 連線
# -------------------------
def get_conn():
    dsn = os.environ['DATABASE_URL']
    if 'sslmode' not in dsn:
        dsn += ('&' if '?' in dsn else '?') + 'sslmode=require'
    result = urlparse(dsn)
    host = result.hostname
    port = result.port or 5432
    user = result.username
    password = result.password
    dbname = result.path.lstrip('/')
    ipv4 = socket.getaddrinfo(host, port, socket.AF_INET)[0][4][0]
    return psycopg.connect(
        host=ipv4,
        port=port,
        user=user,
        password=password,
        dbname=dbname,
        sslmode='require'
    )

# -------------------------
# 小工具
# -------------------------
def is_active_by_end_date(ed: date | None) -> bool:
    return (ed is None) or (ed >= date.today())

def _is_employee_active(conn, emp_id: int) -> bool:
    with conn.cursor() as c:
        c.execute("""
          SELECT (end_date IS NULL OR end_date >= CURRENT_DATE) AND COALESCE(is_active, TRUE)
          FROM employees WHERE id=%s
        """, (emp_id,))
        r = c.fetchone()
        return bool(r and r[0])

def _parse_half_hour(value_str) -> Decimal:
    """驗證小時數為 0.5 的倍數且 > 0"""
    try:
        h = Decimal(str(value_str))
    except (InvalidOperation, TypeError):
        raise ValueError("請輸入數字")
    if h <= 0:
        raise ValueError("時數需大於 0")
    if (h * 2) % 1 != 0:
        raise ValueError("請以 0.5 小時為單位")
    return h

def _parse_half_hour_any(value_str):
    """允許正負、以 0.5 為單位；空值當 0"""
    if value_str in (None, ''):
        return Decimal('0')
    try:
        v = Decimal(str(value_str))
    except (InvalidOperation, TypeError):
        raise ValueError("請輸入數字（0.5 小時為單位）")
    if (v * 2) % 1 != 0:
        raise ValueError("請以 0.5 小時為單位")
    return v

def _form_used_leave_hours() -> Decimal:
    """讀「已用特休」表單欄位（相容舊天數 ×8）。"""
    val_hours = request.form.get('used_leave_hours')
    if val_hours not in (None, ''):
        try:
            h = Decimal(str(val_hours))
        except InvalidOperation:
            h = Decimal('0')
        return h if h >= 0 else Decimal('0')
    val_days = request.form.get('used_leave')
    if val_days not in (None, ''):
        try:
            d = Decimal(str(val_days))
        except InvalidOperation:
            d = Decimal('0')
        return d * 8 if d >= 0 else Decimal('0')
    return Decimal('0')

def _form_hours_or_days(hours_name='hours', days_name='days') -> Decimal:
    """讀請假表單：優先小時（0.5 單位），否則天 ×8。"""
    val_h = request.form.get(hours_name)
    if val_h not in (None, ''):
        return _parse_half_hour(val_h)
    val_d = request.form.get(days_name)
    if val_d not in (None, ''):
        try:
            d = Decimal(str(val_d))
        except InvalidOperation:
            d = Decimal('0')
        if d <= 0:
            raise ValueError("天數需大於 0")
        return d * 8
    raise ValueError("請輸入請假時數")

def _ensure_date(d):
    """DB 取出的若已是 date 就直接回傳；若是 str（YYYY-MM-DD）則轉換。"""
    if d is None:
        return None
    if isinstance(d, date):
        return d
    return datetime.strptime(str(d), "%Y-%m-%d").date()

def next_anniversary(start: date, today: date) -> date:
    """回傳『今天之後最近的一次到職週年日』（處理 2/29）。"""
    if start.month == 2 and start.day == 29:
        try_this = date(today.year, 2, 29)
        this_year_anniv = try_this if try_this.month == 2 and try_this.day == 29 else date(today.year, 2, 28)
    else:
        try:
            this_year_anniv = start.replace(year=today.year)
        except ValueError:
            this_year_anniv = date(today.year, 2, 28)

    if this_year_anniv <= today:
        ny = today.year + 1
        if start.month == 2 and start.day == 29:
            try_next = date(ny, 2, 29)
            return try_next if (try_next.month == 2 and try_next.day == 29) else date(ny, 2, 28)
        else:
            try:
                return start.replace(year=ny)
            except ValueError:
                return date(ny, 2, 28)
    return this_year_anniv

def days_until(target: date, today: date) -> int:
    return (target - today).days

from calendar import monthrange
from datetime import timedelta

def _add_months(d: date, months: int) -> date:
    """日期加月，月底安全處理。"""
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    last = monthrange(y, m)[1]
    return date(y, m, min(d.day, last))

def compute_expiry_dates(grant_date: date, policy: str):
    """
    回傳 (本期到期日, 最終到期日)。
    - 曆年制：本期=當年12/31，最終=次年12/31
    - 週年制：本期=入職+12個月-1天；最終=入職+(12+ANNIV_CARRYOVER_MONTHS)個月-1天
              若 ANNIV_CARRYOVER_MONTHS=0 → 不遞延 → 最終=本期
    """
    if not grant_date:
        return None, None

    if policy == "calendar":
        first_expiry = date(grant_date.year, 12, 31)
        final_expiry = date(grant_date.year + 1, 12, 31)
    else:  # anniversary
        from calendar import monthrange
        def _add_months(d, months):
            y = d.year + (d.month - 1 + months) // 12
            m = (d.month - 1 + months) % 12 + 1
            last = monthrange(y, m)[1]
            return date(y, m, min(d.day, last))
        first_expiry = _add_months(grant_date, 12) - timedelta(days=1)
        if ANNIV_CARRYOVER_MONTHS > 0:
            final_expiry = _add_months(grant_date, 12 + ANNIV_CARRYOVER_MONTHS) - timedelta(days=1)
        else:
            final_expiry = first_expiry
    return first_expiry, final_expiry



# -------------------------
# 資料表初始化/升級（含分店、部門、審核欄位、審計表）
# -------------------------
def init_db():
    with get_conn() as conn, conn.cursor() as c:
        # ========== stores（分店 / 企業單位） ==========
        c.execute('''
            CREATE TABLE IF NOT EXISTS stores (
              id SERIAL PRIMARY KEY,
              name TEXT UNIQUE NOT NULL,
              short_code TEXT UNIQUE,
              is_active BOOLEAN DEFAULT TRUE
            );
        ''')
        conn.commit()

        # ========== store_departments（每個分店的部門清單） ==========
        c.execute('''
            CREATE TABLE IF NOT EXISTS store_departments (
              id SERIAL PRIMARY KEY,
              store_id INTEGER REFERENCES stores(id),
              name TEXT NOT NULL,
              is_active BOOLEAN DEFAULT TRUE,
              UNIQUE(store_id, name)
            );
        ''')
        conn.commit()

        # ========== employees ==========
        c.execute('''
            CREATE TABLE IF NOT EXISTS employees (
              id SERIAL PRIMARY KEY,
              name TEXT, start_date DATE, end_date DATE,
              department TEXT, job_level TEXT, salary_grade TEXT,
              base_salary INTEGER, position_allowance INTEGER,
              on_leave_suspend BOOLEAN,
              used_leave INTEGER,            -- 舊：天
              entitled_leave INTEGER,        -- 舊：天
              entitled_sick INTEGER, used_sick INTEGER,
              entitled_personal INTEGER, used_personal INTEGER,
              entitled_marriage INTEGER, used_marriage INTEGER,
              is_active BOOLEAN DEFAULT TRUE,
              entitled_leave_hours NUMERIC(8,1),  -- 新：小時
              used_leave_hours NUMERIC(8,1),      -- 新：小時
              leave_adjust_hours NUMERIC(8,1) DEFAULT 0, -- 新：調整（小時，可±）
              store_id INTEGER REFERENCES stores(id)      -- 新：分店
            );
        ''')
        conn.commit()
        # 補欄位（若缺）
        c.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;")
        c.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS job_level TEXT;")
        c.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS base_salary INTEGER;")
        c.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS position_allowance INTEGER;")
        c.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS entitled_leave INTEGER;")
        c.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS entitled_sick INTEGER;")
        c.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS used_sick INTEGER;")
        c.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS entitled_personal INTEGER;")
        c.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS used_personal INTEGER;")
        c.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS entitled_marriage INTEGER;")
        c.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS used_marriage INTEGER;")
        c.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS entitled_leave_hours NUMERIC(8,1);")
        c.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS used_leave_hours NUMERIC(8,1);")
        c.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS leave_adjust_hours NUMERIC(8,1) DEFAULT 0;")
        c.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS store_id INTEGER REFERENCES stores(id);")
        conn.commit()

        # ========== insurances ==========
        c.execute("""
            CREATE TABLE IF NOT EXISTS insurances (
              id SERIAL PRIMARY KEY,
              employee_id INTEGER UNIQUE REFERENCES employees(id),
              personal_labour INTEGER DEFAULT 0,
              personal_health INTEGER DEFAULT 0,
              company_labour INTEGER DEFAULT 0,
              company_health INTEGER DEFAULT 0,
              retirement6 INTEGER DEFAULT 0,
              occupational_ins INTEGER DEFAULT 0,
              total_company INTEGER DEFAULT 0,
              note TEXT DEFAULT ''
            );
        """)
        conn.commit()
        c.execute("CREATE SEQUENCE IF NOT EXISTS insurances_id_seq;")
        c.execute("ALTER TABLE insurances ALTER COLUMN id SET DEFAULT nextval('insurances_id_seq');")
        c.execute("ALTER SEQUENCE insurances_id_seq OWNED BY insurances.id;")
        conn.commit()

        # ========== leave_records（加入 hours + 審核欄位 + 軟刪） ==========
        c.execute('''
            CREATE TABLE IF NOT EXISTS leave_records (
              id           SERIAL PRIMARY KEY,
              employee_id  INTEGER REFERENCES employees(id),
              leave_type   TEXT    NOT NULL,
              date_from    DATE    NOT NULL,
              date_to      DATE    NOT NULL,
              days         INTEGER,
              hours        NUMERIC(8,1),
              note         TEXT,
              created_at   TIMESTAMP DEFAULT NOW(),
              status       TEXT DEFAULT 'approved', -- pending/approved/rejected/canceled
              created_by   TEXT,
              approved_by  TEXT,
              approved_at  TIMESTAMP,
              deleted      BOOLEAN DEFAULT FALSE,
              deleted_at   TIMESTAMP
            );
        ''')
        c.execute("ALTER TABLE leave_records ADD COLUMN IF NOT EXISTS hours NUMERIC(8,1);")
        c.execute("ALTER TABLE leave_records ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'approved';")
        c.execute("ALTER TABLE leave_records ADD COLUMN IF NOT EXISTS created_by TEXT;")
        c.execute("ALTER TABLE leave_records ADD COLUMN IF NOT EXISTS approved_by TEXT;")
        c.execute("ALTER TABLE leave_records ADD COLUMN IF NOT EXISTS approved_at TIMESTAMP;")
        c.execute("ALTER TABLE leave_records ADD COLUMN IF NOT EXISTS deleted BOOLEAN DEFAULT FALSE;")
        c.execute("ALTER TABLE leave_records ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP;")
        c.execute("UPDATE leave_records SET status = COALESCE(status, 'approved');")
        conn.commit()

        # ========== audit_logs ==========
        c.execute("""
            CREATE TABLE IF NOT EXISTS audit_logs (
              id SERIAL PRIMARY KEY,
              table_name TEXT,
              row_id INTEGER,
              action TEXT,                -- insert/update/delete/approve/reject/backup/report
              before_json TEXT,
              after_json  TEXT,
              acted_by    TEXT,
              acted_at    TIMESTAMP DEFAULT NOW()
            );
        """)
        conn.commit()

        # 回填：員工小時欄位用天數*8 補上；歷史紀錄 hours 用 days*8 補上
        c.execute("""
          UPDATE employees
             SET entitled_leave_hours = COALESCE(entitled_leave_hours, COALESCE(entitled_leave,0) * 8.0),
                 used_leave_hours     = COALESCE(used_leave_hours,     COALESCE(used_leave,0)     * 8.0)
        """)
        c.execute("""
          UPDATE leave_records
             SET hours = COALESCE(hours, COALESCE(days,0) * 8.0)
        """)
        conn.commit()

        # 預設分店種子資料（若不存在就建立）
        c.execute("SELECT COUNT(*) FROM stores")
        cnt = c.fetchone()[0] or 0
        if cnt == 0:
            c.execute("INSERT INTO stores (name, short_code) VALUES (%s,%s)", ('企鵝藥局', 'PHARM'))
            c.execute("INSERT INTO stores (name, short_code) VALUES (%s,%s)", ('企鵝藥妝', 'DRUGS'))
            conn.commit()
            # 預設部門
            c.execute("SELECT id FROM stores WHERE name=%s", ('企鵝藥局',))
            sid1 = c.fetchone()[0]
            c.execute("SELECT id FROM stores WHERE name=%s", ('企鵝藥妝',))
            sid2 = c.fetchone()[0]
            for sid in (sid1, sid2):
                c.execute("INSERT INTO store_departments (store_id, name) VALUES (%s,%s) ON CONFLICT DO NOTHING", (sid, '門市'))
                c.execute("INSERT INTO store_departments (store_id, name) VALUES (%s,%s) ON CONFLICT DO NOTHING", (sid, '行政'))
                c.execute("INSERT INTO store_departments (store_id, name) VALUES (%s,%s) ON CONFLICT DO NOTHING", (sid, '倉儲'))
            conn.commit()

# === Audit Log 寫入小工具 ===
def write_audit(conn, table, row_id, action, before_obj=None, after_obj=None, acted_by=None):
    with conn.cursor() as c:
        c.execute("""
          INSERT INTO audit_logs (table_name, row_id, action, before_json, after_json, acted_by)
          VALUES (%s,%s,%s,%s,%s,%s)
        """, (table, row_id, action,
              json.dumps(before_obj or {}, ensure_ascii=False),
              json.dumps(after_obj  or {}, ensure_ascii=False),
              acted_by or getattr(g, 'current_user', None)))
    conn.commit()

# === 從 leave_records 動態彙總（只計 approved） ===
def _fetch_leave_usage_hours(conn):
    """
    回傳格式：
    { emp_id: { '病假': 小時, '事假': 小時, '婚假': 小時, '特休': 小時 }, ... }
    只統計：status='approved' AND deleted=false
    """
    data = {}
    with conn.cursor() as c:
        c.execute("""
            SELECT employee_id, leave_type, COALESCE(SUM(hours),0)
              FROM leave_records
             WHERE status='approved' AND COALESCE(deleted,FALSE)=FALSE
             GROUP BY employee_id, leave_type
        """)
        for emp_id, ltype, hrs in c.fetchall():
            d = data.setdefault(emp_id, {})
            d[str(ltype)] = float(hrs or 0.0)
    return data


# -------------------------
# 首頁：員工特休總覽（分店過濾 + 分頁）
# -------------------------
@app.route('/')
def index():
    init_db()
    show_all = (request.args.get('all') == '1')

    # 分店與分頁參數
    try:
        current_store_id = int(request.args.get('store_id')) if request.args.get('store_id') else None
    except ValueError:
        current_store_id = None
    try:
        page = max(int(request.args.get('page', '1')), 1)
    except ValueError:
        page = 1
    try:
        page_size = max(min(int(request.args.get('page_size', '20')), 200), 5)
    except ValueError:
        page_size = 20
    offset = (page - 1) * page_size

    # 分店列表
    with get_conn() as conn, conn.cursor() as c:
        c.execute("SELECT id, name, is_active FROM stores WHERE COALESCE(is_active, TRUE)=TRUE ORDER BY id")
        stores = c.fetchall()

    where = []
    params = []
    if not show_all:
        where.append("(e.end_date IS NULL OR e.end_date >= CURRENT_DATE)")
        where.append("COALESCE(e.is_active, TRUE) = TRUE")
    if current_store_id:
        where.append("e.store_id = %s")
        params.append(current_store_id)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    # 總數
    with get_conn() as conn, conn.cursor() as c:
        c.execute(f"SELECT COUNT(*) FROM employees e {where_sql}", tuple(params))
        total_count = c.fetchone()[0] or 0

    # 主查詢
    with get_conn() as conn, conn.cursor() as c:
        base_select = f'''
            SELECT
                e.id, e.name, e.start_date, e.end_date,
                e.department, e.job_level,
                e.salary_grade, e.base_salary, e.position_allowance,
                e.on_leave_suspend, e.used_leave, e.entitled_leave,
                e.entitled_leave_hours, e.used_leave_hours,
                e.entitled_sick, e.used_sick,
                e.entitled_personal, e.used_personal,
                e.entitled_marriage, e.used_marriage,
                e.is_active,
                e.leave_adjust_hours,
                e.store_id,
                s.name AS store_name
            FROM employees e
            LEFT JOIN stores s ON s.id = e.store_id
            {where_sql}
            ORDER BY e.id
            LIMIT %s OFFSET %s
        '''
        c.execute(base_select, tuple(params) + (page_size, offset))
        rows = c.fetchall()
        usage_map = _fetch_leave_usage_hours(conn)  # 彙總 approved 假單

    employees = []
    for (sid, name, sd, ed, dept, level, grade, base, allowance,
         suspend, used_days, ent_days,
         ent_hours, used_hours,
         sick_ent, sick_used, per_ent, per_used, mar_ent, mar_used,
         is_active, adj_hours, store_id, store_name) in rows:

        # 特休（小時制）
        ent_h_base = float(ent_hours) if ent_hours is not None else float((ent_days or 0) * 8)
        used_h     = float(used_hours) if used_hours is not None else float((used_days or 0) * 8)
        adj        = float(adj_hours or 0)
        ent_h      = max(ent_h_base + adj, 0.0)

        # 動態計算（只計 approved）
        u = usage_map.get(sid, {})
        # 覆寫特休已用（小時）：以已核准假單加總為準
        used_h = float(u.get('特休', used_h))

        sick_used_hours     = float(u.get('病假', 0.0))
        personal_used_hours = float(u.get('事假', 0.0))
        marriage_used_hours = float(u.get('婚假', 0.0))

        # 以天呈現病/事/婚
        sick_ent_days      = int(sick_ent or 0)
        personal_ent_days  = int(per_ent or 0)
        marriage_ent_days  = int(mar_ent or 0)

        sick_used_days     = sick_used_hours / 8.0
        personal_used_days = personal_used_hours / 8.0
        marriage_used_days = marriage_used_hours / 8.0

        remaining_sick_days     = max(sick_ent_days - sick_used_days, 0.0)
        remaining_personal_days = max(personal_ent_days - personal_used_days, 0.0)
        remaining_marriage_days = max(marriage_ent_days - marriage_used_days, 0.0)

        # 年資
        ref_date = ed or sd
        if isinstance(ref_date, str):
            ref_date = datetime.strptime(ref_date, '%Y-%m-%d').date()
        years, months = calculate_seniority(ref_date)

        employees.append({
            'id': sid,
            'name': name,
            'start_date': sd,
            'end_date': ed or '',
            'department': dept,
            'job_level': level,
            'salary_grade': grade,
            'base_salary': base,
            'position_allowance': allowance,
            'years': years,
            'months': months,

            'entitled': ent_h,
            'used': used_h,
            'remaining': max(ent_h - used_h, 0.0),

            'suspend': suspend,

            'entitled_sick': sick_ent_days,
            'used_sick': sick_used_days,
            'remaining_sick': remaining_sick_days,

            'entitled_personal': personal_ent_days,
            'used_personal': personal_used_days,
            'remaining_personal': remaining_personal_days,

            'entitled_marriage': marriage_ent_days,
            'used_marriage': marriage_used_days,
            'remaining_marriage': remaining_marriage_days,

            'is_active': is_active,
            'store_id': store_id,
            'store_name': store_name or '未分店',
        })

    total_pages = (total_count + page_size - 1) // page_size
    pagination = {
        'page': page,
        'page_size': page_size,
        'total': total_count,
        'total_pages': total_pages,
        'has_prev': page > 1,
        'has_next': page < total_pages
    }

    return render_template('index.html',
                           employees=employees,
                           show_all=show_all,
                           stores=stores,
                           current_store_id=current_store_id,
                           pagination=pagination,
                           page_size=page_size)

# -------------------------
# 分店管理（列表 + 新增/編輯/啟用）
# -------------------------
@app.get('/stores')
def store_list():
    init_db()
    with get_conn() as conn, conn.cursor() as c:
        c.execute("""
          SELECT s.id, s.name, s.short_code, s.is_active,
                 COALESCE(cnt.cnt,0)
            FROM stores s
            LEFT JOIN (
              SELECT store_id, COUNT(*) AS cnt
                FROM employees
               GROUP BY store_id
            ) cnt ON cnt.store_id = s.id
           ORDER BY s.id
        """)
        rows = c.fetchall()
    # 簡易頁面（用 template 會更漂亮，先用最小可用）
    html = ["<h1>分店管理</h1>", '<a href="/">← 返回</a><br><br>']
    html.append("""
    <form method="post" action="/stores/add" style="margin-bottom:16px">
      名稱：<input name="name" required>
      短代碼：<input name="short_code" placeholder="可留空">
      <button type="submit">新增</button>
    </form>
    """)
    html.append("<table border=1 cellpadding=6><tr><th>ID</th><th>名稱</th><th>代碼</th><th>在職人數</th><th>啟用</th><th>操作</th></tr>")
    for sid, name, code, active, cnt in rows:
        html.append(f"""
          <tr>
            <td>{sid}</td>
            <td>{name}</td>
            <td>{code or ''}</td>
            <td>{cnt}</td>
            <td>{"是" if active else "否"}</td>
            <td>
              <form method="post" action="/stores/{sid}/edit" style="display:inline">
                名稱 <input name="name" value="{name}" required>
                代碼 <input name="short_code" value="{code or ''}">
                <button type="submit">儲存</button>
              </form>
              <form method="post" action="/stores/{sid}/toggle" style="display:inline;margin-left:8px">
                <button type="submit">{'停用' if active else '啟用'}</button>
              </form>
              <a href="/stores/{sid}/departments" style="margin-left:8px">部門管理</a>
            </td>
          </tr>
        """)
    html.append("</table>")
    return "\n".join(html)

@app.post('/stores/add')
def store_add():
    init_db()
    name = request.form.get('name','').strip()
    code = (request.form.get('short_code') or '').strip() or None
    if not name:
        return abort(400, 'name required')
    with get_conn() as conn, conn.cursor() as c:
        c.execute("INSERT INTO stores (name, short_code) VALUES (%s,%s) RETURNING id", (name, code))
        sid = c.fetchone()[0]
        conn.commit()
        write_audit(conn, 'stores', sid, 'insert', None, {'name': name, 'short_code': code})
    return redirect(url_for('store_list'))

@app.post('/stores/<int:store_id>/edit')
def store_edit(store_id):
    init_db()
    name = request.form.get('name','').strip()
    code = (request.form.get('short_code') or '').strip() or None
    if not name:
        return abort(400, 'name required')
    with get_conn() as conn, conn.cursor() as c:
        c.execute("SELECT name, short_code FROM stores WHERE id=%s", (store_id,))
        before = c.fetchone() or ('','')
        c.execute("UPDATE stores SET name=%s, short_code=%s WHERE id=%s", (name, code, store_id))
        conn.commit()
        write_audit(conn, 'stores', store_id, 'update',
                    {'name': before[0], 'short_code': before[1]},
                    {'name': name, 'short_code': code})
    return redirect(url_for('store_list'))

@app.post('/stores/<int:store_id>/toggle')
def store_toggle(store_id):
    init_db()
    with get_conn() as conn, conn.cursor() as c:
        c.execute("SELECT is_active FROM stores WHERE id=%s", (store_id,))
        row = c.fetchone()
        if not row:
            return abort(404)
        newv = not bool(row[0])
        c.execute("UPDATE stores SET is_active=%s WHERE id=%s", (newv, store_id))
        conn.commit()
        write_audit(conn, 'stores', store_id, 'update', {'is_active': not newv}, {'is_active': newv})
    return redirect(url_for('store_list'))

# -------------------------
# 分店部門管理（簡易頁面 + API）
# -------------------------
@app.get('/stores/<int:store_id>/departments')
def dept_page(store_id):
    init_db()
    with get_conn() as conn, conn.cursor() as c:
        c.execute("SELECT id, name FROM stores WHERE id=%s", (store_id,))
        srow = c.fetchone()
        if not srow:
            return abort(404)
        sname = srow[1]
        c.execute("SELECT id, name, is_active FROM store_departments WHERE store_id=%s ORDER BY id", (store_id,))
        rows = c.fetchall()
    html = [f"<h1>部門管理 — {sname}</h1>", '<a href="/stores">← 返回分店</a><br><br>']
    html.append(f"""
    <form method="post" action="/api/stores/{store_id}/departments" style="margin-bottom:16px">
      新增部門：<input name="name" required>
      <button type="submit">新增</button>
    </form>
    """)
    html.append("<table border=1 cellpadding=6><tr><th>ID</th><th>名稱</th><th>啟用</th><th>操作</th></tr>")
    for did, name, active in rows:
        html.append(f"""
          <tr>
            <td>{did}</td>
            <td>{name}</td>
            <td>{"是" if active else "否"}</td>
            <td>
              <form method="post" action="/api/stores/{store_id}/departments/{did}/toggle">
                <button type="submit">{'停用' if active else '啟用'}</button>
              </form>
            </td>
          </tr>
        """)
    html.append("</table>")
    return "\n".join(html)

@app.get('/api/stores')
def api_stores():
    init_db()
    with get_conn() as conn, conn.cursor() as c:
        c.execute("SELECT id, name FROM stores WHERE COALESCE(is_active, TRUE)=TRUE ORDER BY id")
        rows = [{'id': r[0], 'name': r[1]} for r in c.fetchall()]
    return jsonify(rows)

@app.get('/api/stores/<int:store_id>/departments')
def api_store_departments(store_id):
    init_db()
    with get_conn() as conn, conn.cursor() as c:
        c.execute("SELECT id, name FROM store_departments WHERE store_id=%s AND COALESCE(is_active,TRUE)=TRUE ORDER BY id", (store_id,))
        rows = [{'id': r[0], 'name': r[1]} for r in c.fetchall()]
    return jsonify(rows)

@app.post('/api/stores/<int:store_id>/departments')
def api_store_departments_add(store_id):
    init_db()
    name = (request.form.get('name') or request.json.get('name') if request.is_json else '').strip()
    if not name:
        return abort(400, 'name required')
    with get_conn() as conn, conn.cursor() as c:
        c.execute("INSERT INTO store_departments (store_id, name) VALUES (%s,%s) ON CONFLICT DO NOTHING RETURNING id", (store_id, name))
        row = c.fetchone()
        if row:
            did = row[0]
            conn.commit()
            write_audit(conn, 'store_departments', did, 'insert', None, {'store_id': store_id, 'name': name})
    return redirect(url_for('dept_page', store_id=store_id))

@app.post('/api/stores/<int:store_id>/departments/<int:dep_id>/toggle')
def api_store_departments_toggle(store_id, dep_id):
    init_db()
    with get_conn() as conn, conn.cursor() as c:
        c.execute("SELECT is_active FROM store_departments WHERE id=%s AND store_id=%s", (dep_id, store_id))
        row = c.fetchone()
        if not row:
            return abort(404)
        newv = not bool(row[0])
        c.execute("UPDATE store_departments SET is_active=%s WHERE id=%s", (newv, dep_id))
        conn.commit()
        write_audit(conn, 'store_departments', dep_id, 'update', {'is_active': not newv}, {'is_active': newv})
    return redirect(url_for('dept_page', store_id=store_id))

# -------------------------
# 新增員工（支援調整值 + 分店）
# -------------------------
@app.route('/add', methods=['GET','POST'])
def add_employee():
    init_db()
    if request.method == 'POST':
        name       = request.form['name']
        start_date = request.form['start_date']
        end_date_s = request.form.get('end_date') or None
        dept       = request.form['department']
        level      = request.form.get('job_level') or ''
        grade      = request.form['salary_grade']
        base       = int(request.form.get('base_salary') or 0)
        allowance  = int(request.form.get('position_allowance') or 0)
        suspend    = bool(request.form.get('suspend'))
        store_id_s = request.form.get('store_id')  # 新：分店下拉
        store_id   = int(store_id_s) if store_id_s else None

        sd_date = datetime.strptime(start_date, '%Y-%m-%d').date()
        ed_date = datetime.strptime(end_date_s, '%Y-%m-%d').date() if end_date_s else None
        years, months = calculate_seniority(sd_date)

        entitled_days  = entitled_leave_days(years, months, suspend)
        entitled_hours = Decimal(str(entitled_days)) * 8
        used_hours     = _form_used_leave_hours()
        adj_hours      = _parse_half_hour_any(request.form.get('leave_adjust_hours'))

        sick_ent = entitled_sick_days(years, months)
        per_ent  = entitled_personal_days(years, months)
        mar_ent  = entitled_marriage_days()
        is_active = is_active_by_end_date(ed_date)

        with get_conn() as conn, conn.cursor() as c:
            c.execute('''
                INSERT INTO employees (
                  name, start_date, end_date,
                  department, job_level, salary_grade,
                  base_salary, position_allowance,
                  on_leave_suspend,
                  used_leave, entitled_leave,
                  entitled_leave_hours, used_leave_hours,
                  entitled_sick, used_sick,
                  entitled_personal, used_personal,
                  entitled_marriage, used_marriage,
                  is_active, leave_adjust_hours, store_id
                ) VALUES (
                  %s,%s,%s,
                  %s,%s,%s,
                  %s,%s,
                  %s,
                  %s,%s,
                  %s,%s,
                  %s,0,
                  %s,0,
                  %s,0,
                  %s,%s,%s
                )
            ''', (
                name, start_date, ed_date,
                dept, level, grade,
                base, allowance,
                suspend,
                int(used_hours / 8), int(entitled_days),
                str(entitled_hours), str(used_hours),
                sick_ent,
                per_ent,
                mar_ent,
                is_active, str(adj_hours), store_id
            ))
            conn.commit()
        return redirect(url_for('index'))
    # 分店清單供表單下拉
    with get_conn() as conn, conn.cursor() as c:
        c.execute("SELECT id, name FROM stores WHERE COALESCE(is_active, TRUE)=TRUE ORDER BY id")
        stores = c.fetchall()
    return render_template('add_employee.html', stores=stores)

# -------------------------
# 編輯員工（支援調整值 + 分店）
# -------------------------
@app.route('/edit/<int:emp_id>', methods=['GET','POST'])
def edit_employee(emp_id):
    init_db()
    if request.method == 'POST':
        name       = request.form['name']
        start_date = request.form['start_date']
        end_date_s = request.form.get('end_date') or None
        dept       = request.form['department']
        level      = request.form.get('job_level') or ''
        grade      = request.form['salary_grade']
        base       = int(request.form.get('base_salary') or 0)
        allowance  = int(request.form.get('position_allowance') or 0)
        suspend    = bool(request.form.get('suspend'))
        store_id_s = request.form.get('store_id')
        store_id   = int(store_id_s) if store_id_s else None

        sd_date = datetime.strptime(start_date, '%Y-%m-%d').date()
        ed_date = datetime.strptime(end_date_s, '%Y-%m-%d').date() if end_date_s else None
        years, months = calculate_seniority(sd_date)

        entitled_days  = entitled_leave_days(years, months, suspend)
        entitled_hours = Decimal(str(entitled_days)) * 8
        used_hours     = _form_used_leave_hours()
        adj_hours      = _parse_half_hour_any(request.form.get('leave_adjust_hours'))

        sick_ent = entitled_sick_days(years, months)
        per_ent  = entitled_personal_days(years, months)
        mar_ent  = entitled_marriage_days()
        is_active = is_active_by_end_date(ed_date)

        with get_conn() as conn, conn.cursor() as c:
            c.execute('''
                UPDATE employees SET
                  name               = %s,
                  start_date         = %s,
                  end_date           = %s,
                  department         = %s,
                  job_level          = %s,
                  salary_grade       = %s,
                  base_salary        = %s,
                  position_allowance = %s,
                  on_leave_suspend   = %s,
                  used_leave         = %s,
                  entitled_leave     = %s,
                  entitled_leave_hours = %s,
                  used_leave_hours     = %s,
                  entitled_sick      = %s,  used_sick      = %s,
                  entitled_personal  = %s,  used_personal  = %s,
                  entitled_marriage  = %s,  used_marriage  = %s,
                  is_active          = %s,
                  leave_adjust_hours = %s,
                  store_id           = %s
                WHERE id = %s
            ''', (
                name, sd_date, ed_date,
                dept, level, grade,
                base, allowance,
                suspend,
                int(used_hours / 8), int(entitled_days),
                str(entitled_hours), str(used_hours),
                sick_ent, 0,
                per_ent,  0,
                mar_ent,  0,
                is_active,
                str(adj_hours),
                store_id,
                emp_id
            ))
            conn.commit()
        return redirect(url_for('index'))

    with get_conn() as conn, conn.cursor() as c:
        c.execute('''
            SELECT
              id, name, start_date, end_date,
              department, job_level, salary_grade,
              base_salary, position_allowance,
              on_leave_suspend, used_leave, entitled_leave,
              entitled_leave_hours, used_leave_hours,
              leave_adjust_hours, store_id
            FROM employees
            WHERE id = %s
        ''', (emp_id,))
        r = c.fetchone()
        c.execute("SELECT id, name FROM stores WHERE COALESCE(is_active, TRUE)=TRUE ORDER BY id")
        stores = c.fetchall()
    return render_template('edit_employee.html', emp=r, stores=stores)

# -------------------------
# 保險列表（預設只顯示在職）
# -------------------------
@app.route('/insurance')
def list_insurance():
    init_db()
    show_all = (request.args.get('all') == '1')

    where_clause = ""
    if not show_all:
        where_clause = """
        WHERE
          (e.end_date IS NULL OR e.end_date >= CURRENT_DATE)
          AND COALESCE(e.is_active, TRUE) = TRUE
          AND COALESCE(e.on_leave_suspend, FALSE) = FALSE
        """

    with get_conn() as conn, conn.cursor() as c:
        c.execute(f'''
            SELECT e.id, e.name,
                   i.personal_labour, i.personal_health,
                   i.company_labour, i.company_health,
                   i.retirement6, i.occupational_ins,
                   i.total_company, i.note
              FROM employees e
              LEFT JOIN insurances i ON e.id = i.employee_id
              {where_clause}
              ORDER BY e.id
        ''')
        rows = c.fetchall()

    return render_template('insurance.html', items=rows, show_all=show_all)

# -------------------------
# 編輯保險（不允許離職/非在職）
# -------------------------
@app.route('/insurance/edit/<int:emp_id>', methods=['GET','POST'])
def edit_insurance(emp_id):
    init_db()
    with get_conn() as conn:
        if not _is_employee_active(conn, emp_id):
            return abort(400, description="離職或非在職員工不可編輯保險")

        if request.method == 'POST':
            pl  = int(request.form.get('personal_labour')   or 0)
            ph  = int(request.form.get('personal_health')   or 0)
            cl  = int(request.form.get('company_labour')    or 0)
            ch  = int(request.form.get('company_health')    or 0)
            r6  = int(request.form.get('retirement6')       or 0)
            oi  = int(request.form.get('occupational_ins')  or 0)
            tot = int(request.form.get('total_company')     or 0)
            note= request.form.get('note','')

            with conn.cursor() as c:
                c.execute('SELECT id FROM insurances WHERE employee_id=%s', (emp_id,))
                if c.fetchone():
                    c.execute('''
                        UPDATE insurances SET
                          personal_labour  = %s,
                          personal_health  = %s,
                          company_labour   = %s,
                          company_health   = %s,
                          retirement6      = %s,
                          occupational_ins = %s,
                          total_company    = %s,
                          note             = %s
                        WHERE employee_id = %s
                    ''', (pl, ph, cl, ch, r6, oi, tot, note, emp_id))
                else:
                    c.execute('''
                        INSERT INTO insurances (
                          employee_id,
                          personal_labour, personal_health,
                          company_labour,  company_health,
                          retirement6,     occupational_ins,
                          total_company,   note
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ''', (emp_id, pl, ph, cl, ch, r6, oi, tot, note))
                conn.commit()
        with conn.cursor() as c:
            c.execute('''
                SELECT id, employee_id,
                       personal_labour, personal_health,
                       company_labour, company_health,
                       retirement6, occupational_ins,
                       total_company, note
                  FROM insurances
                 WHERE employee_id=%s
            ''', (emp_id,))
            r = c.fetchone() or [None, emp_id, 0, 0, 0, 0, 0, 0, 0, '']

    return render_template('edit_insurance.html', emp_id=emp_id, ins=r)

# -------------------------
# 請假紀錄（小時制 + 審核）
# -------------------------
@app.route('/history/<int:emp_id>/<leave_type>')
def leave_history(emp_id, leave_type):
    init_db()
    with get_conn() as conn, conn.cursor() as c:
        c.execute('SELECT name FROM employees WHERE id=%s', (emp_id,))
        name = c.fetchone()[0]
        c.execute('''
            SELECT id, date_from, date_to, hours, days, note, created_at, status, created_by, approved_by, approved_at
              FROM leave_records
             WHERE employee_id=%s AND leave_type=%s
             ORDER BY date_from DESC
        ''', (emp_id, leave_type))
        rows = c.fetchall()

    records = []
    for rid, df, dt, hours, days, note, created, status, created_by, approved_by, approved_at in rows:
        records.append(SimpleNamespace(
            id=rid,
            start_date = df.strftime('%Y-%m-%d'),
            end_date   = dt.strftime('%Y-%m-%d'),
            hours      = float(hours or 0),
            days       = int(days or 0),
            note       = note or '',
            created_at = created.strftime('%Y-%m-%d %H:%M'),
            status     = status or 'approved',
            created_by = created_by or '',
            approved_by= approved_by or '',
            approved_at= approved_at.strftime('%Y-%m-%d %H:%M') if approved_by else ''
        ))

    return render_template('history.html',
                           emp_id=emp_id,
                           name=name,
                           leave_type=leave_type,
                           records=records)

@app.route('/history/<int:emp_id>/<leave_type>/add', methods=['GET','POST'])
def add_leave_record(emp_id, leave_type):
    init_db()
    if request.method == 'POST':
        df   = request.form['start_date']
        dt   = request.form['end_date']
        note = request.form.get('note','')
        hours = _form_hours_or_days('hours', 'days')
        days_int = int(hours // 8)

        with get_conn() as conn, conn.cursor() as c:
            c.execute('''
                INSERT INTO leave_records
                  (employee_id, leave_type, date_from, date_to, hours, days, note, status, created_by, approved_by, approved_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'approved',%s,%s,NOW())
                RETURNING id
            ''', (emp_id, leave_type, df, dt, str(hours), days_int, note,
                  getattr(g,'current_user', None), getattr(g,'current_user', None)))
            rid = c.fetchone()[0]
            conn.commit()
            write_audit(conn, 'leave_records', rid, 'insert', None, {
                'employee_id': emp_id, 'leave_type': leave_type,
                'hours': float(hours), 'note': note, 'status':'approved'
            })
        return redirect(url_for('leave_history', emp_id=emp_id, leave_type=leave_type))

    return render_template('add_leave.html', emp_id=emp_id, leave_type=leave_type)


@app.route('/history/<int:emp_id>/<leave_type>/edit/<int:record_id>', methods=['GET','POST'])
def edit_leave_record(emp_id, leave_type, record_id):
    init_db()
    if request.method == 'POST':
        df   = request.form['start_date']
        dt   = request.form['end_date']
        note = request.form.get('note','')
        hours = _form_hours_or_days('hours', 'days')
        days_int = int(hours // 8)

        with get_conn() as conn, conn.cursor() as c:
            c.execute('SELECT date_from, date_to, hours, days, note, status FROM leave_records WHERE id=%s', (record_id,))
            bdf, bdt, bhrs, bdays, bnote, bstatus = c.fetchone()

            c.execute('''
                UPDATE leave_records
                   SET date_from   = %s,
                       date_to     = %s,
                       hours       = %s,
                       days        = %s,
                       note        = %s,
                       status      = 'approved',
                       approved_by = %s,
                       approved_at = NOW()
                 WHERE id = %s
            ''', (df, dt, str(hours), days_int, note, getattr(g,'current_user', None), record_id))
            conn.commit()
            write_audit(conn, 'leave_records', record_id, 'update', {
                'date_from': bdf.strftime('%Y-%m-%d'), 'date_to': bdt.strftime('%Y-%m-%d'),
                'hours': float(bhrs or 0), 'days': int(bdays or 0),
                'note': bnote or '', 'status': bstatus or ''
            }, {
                'date_from': df, 'date_to': dt, 'hours': float(hours),
                'days': days_int, 'note': note, 'status': 'approved'
            })
        return redirect(url_for('leave_history', emp_id=emp_id, leave_type=leave_type))

    with get_conn() as conn, conn.cursor() as c:
        c.execute('''
            SELECT date_from, date_to, hours, days, note
              FROM leave_records
             WHERE id=%s
        ''', (record_id,))
        df, dt, hours, days, note = c.fetchone()

    # （以下是你原本 + 到期提示變數；若你還沒加 compute_expiry_dates，可刪掉這 4 行）
    first_expiry, final_expiry = compute_expiry_dates(_ensure_date(df), LEAVE_POLICY)
    days_left = (final_expiry - date.today()).days if final_expiry else None

    return render_template('edit_leave.html',
                           emp_id=emp_id,
                           leave_type=leave_type,
                           record_id=record_id,
                           start_date=df.strftime('%Y-%m-%d'),
                           end_date  =dt.strftime('%Y-%m-%d'),
                           hours     =float(hours or 0),
                           days      =int(days or 0),
                           note      =note or '',
                           policy_name="週年制" if LEAVE_POLICY=="anniversary" else "曆年制",
                           reminder_window_days=ALERT_WINDOW_DAYS,
                           expiry_date=final_expiry.isoformat() if final_expiry else "",
                           days_to_expiry=days_left)


@app.post('/history/<int:emp_id>/<leave_type>/approve/<int:record_id>')
def approve_leave(emp_id, leave_type, record_id):
    init_db()
    with get_conn() as conn, conn.cursor() as c:
        c.execute('SELECT status FROM leave_records WHERE id=%s', (record_id,))
        row = c.fetchone()
        if not row:
            return abort(404)
        before = {'status': row[0]}
        c.execute("""
          UPDATE leave_records
             SET status='approved', approved_by=%s, approved_at=NOW()
           WHERE id=%s
        """, (getattr(g,'current_user', None), record_id))
        conn.commit()
        write_audit(conn, 'leave_records', record_id, 'approve', before, {'status':'approved'})
    return redirect(url_for('leave_history', emp_id=emp_id, leave_type=leave_type))


@app.post('/history/<int:emp_id>/<leave_type>/reject/<int:record_id>')
def reject_leave(emp_id, leave_type, record_id):
    init_db()
    with get_conn() as conn, conn.cursor() as c:
        c.execute('SELECT status FROM leave_records WHERE id=%s', (record_id,))
        row = c.fetchone()
        if not row:
            return abort(404)
        before = {'status': row[0]}
        c.execute("""
          UPDATE leave_records
             SET status='rejected', approved_by=%s, approved_at=NOW()
           WHERE id=%s
        """, (getattr(g,'current_user', None), record_id))
        conn.commit()
        write_audit(conn, 'leave_records', record_id, 'reject', before, {'status':'rejected'})
    return redirect(url_for('leave_history', emp_id=emp_id, leave_type=leave_type))


@app.post('/history/<int:emp_id>/<leave_type>/cancel/<int:record_id>')
def cancel_leave_record(emp_id, leave_type, record_id):
    """作廢：僅更新 status=canceled，不列入特休扣抵"""
    init_db()
    with get_conn() as conn, conn.cursor() as c:
        c.execute("SELECT status, deleted FROM leave_records WHERE id=%s", (record_id,))
        row = c.fetchone()
        if not row:
            return abort(404)
        before = {'status': row[0], 'deleted': row[1]}
        c.execute("""
          UPDATE leave_records
             SET status='canceled',
                 approved_by=%s,
                 approved_at=NOW()
           WHERE id=%s
        """, (getattr(g,'current_user', None), record_id))
        conn.commit()
        write_audit(conn, 'leave_records', record_id, 'cancel', before, {'status': 'canceled'})
    return redirect(url_for('leave_history', emp_id=emp_id, leave_type=leave_type))


@app.post('/history/<int:emp_id>/<leave_type>/delete/<int:record_id>')
def delete_leave_record(emp_id, leave_type, record_id):
    """刪除（軟刪）：deleted=true，不列入任何計算"""
    init_db()
    with get_conn() as conn, conn.cursor() as c:
        c.execute("SELECT status, deleted FROM leave_records WHERE id=%s", (record_id,))
        row = c.fetchone()
        if not row:
            return abort(404)
        before = {'status': row[0], 'deleted': row[1]}
        c.execute("""
          UPDATE leave_records
             SET deleted=TRUE,
                 deleted_at=NOW()
           WHERE id=%s
        """, (record_id,))
        conn.commit()
        write_audit(conn, 'leave_records', record_id, 'delete', before, {'deleted': True})
    return redirect(url_for('leave_history', emp_id=emp_id, leave_type=leave_type))


# -------------------------
# 薪資/保險明細
# -------------------------
@app.route('/salary/<int:emp_id>')
def salary_detail(emp_id):
    init_db()
    with get_conn() as conn, conn.cursor() as c:
        c.execute('SELECT name, salary_grade FROM employees WHERE id=%s', (emp_id,))
        row = c.fetchone()
        if not row:
            return abort(404)
        name, grade = row

    with get_conn() as conn, conn.cursor() as c:
        c.execute('''
            SELECT personal_labour, personal_health,
                   company_labour, company_health,
                   retirement6, occupational_ins, total_company, note
              FROM insurances
             WHERE employee_id = %s
        ''', (emp_id,))
        ins = c.fetchone() or (0,0,0,0,0,0,0,'')
        (pl, ph, cl, ch, ret6, oi, total, note) = ins

    return render_template('salary_detail.html',
                           emp_id=emp_id,
                           name=name,
                           grade=grade,
                           personal_labour=pl,
                           personal_health=ph,
                           company_labour=cl,
                           company_health=ch,
                           retirement6=ret6,
                           occupational_ins=oi,
                           total_company=total,
                           note=note)

# -------------------------
# 軟刪除/還原
# -------------------------
@app.route('/delete/<int:emp_id>')
def delete_employee(emp_id):
    init_db()
    today = date.today()
    with get_conn() as conn, conn.cursor() as c:
        c.execute('''
            UPDATE employees
               SET is_active = FALSE,
                   end_date  = %s
             WHERE id = %s
        ''', (today, emp_id))
        conn.commit()
    return redirect(url_for('index'))

@app.route('/restore/<int:emp_id>')
def restore_employee(emp_id):
    init_db()
    with get_conn() as conn, conn.cursor() as c:
        c.execute('''
            UPDATE employees
               SET is_active = TRUE,
                   end_date   = NULL
             WHERE id = %s
        ''', (emp_id,))
        conn.commit()
    return redirect(url_for('index', all='1'))

# -------------------------
# 特休到期提醒（到職日制）
# -------------------------
def _fetch_active_employees_for_expiry():
    """抓取在職員工與計算特休剩餘（小時），並給出『最終到期日』（週年制=入職+24個月-1天）。"""
    with get_conn() as conn, conn.cursor() as c:
        c.execute('''
            SELECT
                id, name, start_date, end_date,
                COALESCE(entitled_leave_hours, COALESCE(entitled_leave,0)*8.0) AS ent_hours,
                COALESCE(used_leave_hours,     COALESCE(used_leave,0)    *8.0) AS used_hours,
                COALESCE(leave_adjust_hours, 0) AS adj_hours,
                COALESCE(is_active, TRUE) AS is_active
            FROM employees
            WHERE (end_date IS NULL OR end_date >= CURRENT_DATE)
              AND COALESCE(is_active, TRUE) = TRUE
            ORDER BY id
        ''')
        rows = c.fetchall()

    today = date.today()
    result = []
    for (eid, name, sd, ed, ent_h, used_h, adj_h, is_act) in rows:
        sd = _ensure_date(sd)
        if not sd:
            continue

        # 🔽 週年制：用 compute_expiry_dates 計算；以「最終到期日」為提醒基準
        first_expiry, final_expiry = compute_expiry_dates(sd, LEAVE_POLICY)
        expiry = final_expiry or next_anniversary(sd, today)  # 沒算到時退回原本周年日

        entitled = max(float(ent_h or 0) + float(adj_h or 0), 0.0)
        used = float(used_h or 0)
        remaining = max(entitled - used, 0.0)
        if remaining <= 0:
            continue

        result.append({
            "id": eid,
            "name": name,
            "start_date": sd.isoformat(),
            "expiry_date": expiry.isoformat(),
            "days_left": days_until(expiry, today),
            "remain_hours": round(remaining, 1)
        })
    return result


@app.route('/alerts/leave-expiring')
def leave_expiring():
    init_db()
    data = _fetch_active_employees_for_expiry()
    data = [d for d in data if 0 <= d["days_left"] <= ALERT_WINDOW_DAYS]
    data.sort(key=lambda x: x["days_left"])

    html_rows = []
    for d in data:
        badge = ''
        if d["days_left"] <= 30:
            badge = '<span style="background:#fee2e2;color:#b91c1c;padding:2px 8px;border-radius:9999px;font-size:12px;">緊急</span>'
        qty_tag = ''
        if d["remain_hours"] >= 40:
            qty_tag = '<span style="background:#fef3c7;color:#92400e;padding:2px 8px;border-radius:9999px;font-size:12px;margin-left:6px;">偏多</span>'
        html_rows.append(f"""
        <tr>
          <td>{d['name']}</td>
          <td>{d['remain_hours']}{qty_tag}</td>
          <td>{d['expiry_date']}</td>
          <td>{d['days_left']} {badge}</td>
        </tr>
        """)

    content = f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="utf-8" />
      <title>特休即將到期</title>
      <link href="/static/style.css" rel="stylesheet">
      <style>
        table {{ width:100%; border-collapse:collapse; }}
        th, td {{ padding:8px 10px; border-bottom:1px solid #eee; text-align:left; }}
        th {{ font-weight:600; color:#374151; }}
        .card {{ border:1px solid #e5e7eb; border-radius:12px; padding:16px; margin-bottom:16px; }}
      </style>
    </head>
    <body class="p-4">
      <h1 class="text-2xl mb-4">特休即將到期</h1>

      <div class="card">
        <div>今天：{date.today().isoformat()}　|　提醒視窗：{ALERT_WINDOW_DAYS} 天內</div>
        <div>共有 <strong>{len(data)}</strong> 位員工特休即將到期</div>
      </div>

      <table>
        <thead>
          <tr>
            <th>姓名</th>
            <th>剩餘特休（小時）</th>
            <th>到期日</th>
            <th>剩餘天數</th>
          </tr>
        </thead>
        <tbody>
          {''.join(html_rows) if html_rows else f'<tr><td colspan="4" style="color:#6b7280;">未來 {ALERT_WINDOW_DAYS} 天內沒有到期的特休</td></tr>'}
        </tbody>
      </table>

      <div style="margin-top:16px;">
        <a href="{url_for('index')}" class="text-blue-600">← 返回首頁</a>
      </div>
    </body>
    </html>
    """
    return content

# ====== 分店管理（branch_management） ======

@app.get('/branches')
def branch_management():
    """分店管理：列出分店、快速新增/啟用關閉。"""
    init_db()
    with get_conn() as conn, conn.cursor() as c:
        c.execute("SELECT id, name, COALESCE(short_code,''), COALESCE(is_active, TRUE) FROM stores ORDER BY id;")
        stores = c.fetchall()

    # 簡單內嵌頁面，避免再建模板檔
    rows_html = []
    for sid, name, code, active in stores:
        badge = '<span style="padding:2px 8px;border-radius:9999px;background:#ecfdf5;color:#065f46;font-size:12px">啟用</span>' \
                if active else \
                '<span style="padding:2px 8px;border-radius:9999px;background:#fef2f2;color:#991b1b;font-size:12px">關閉</span>'
        rows_html.append(f"""
        <tr>
          <td class="px-2 py-1">{sid}</td>
          <td class="px-2 py-1">{name}</td>
          <td class="px-2 py-1">{code}</td>
          <td class="px-2 py-1">{badge}</td>
          <td class="px-2 py-1">
            <form action="/branches/{sid}/toggle" method="post" style="display:inline">
              <button type="submit" class="px-2 py-1" style="border:1px solid #e5e7eb;border-radius:8px;background:#fff">{'關閉' if active else '啟用'}</button>
            </form>
            <details style="display:inline-block;margin-left:8px">
              <summary style="cursor:pointer;color:#2563eb">重新命名</summary>
              <form action="/branches/{sid}/rename" method="post" style="margin-top:6px">
                <input type="text" name="name" value="{name}" class="border px-2 py-1" required>
                <input type="text" name="short_code" value="{code}" class="border px-2 py-1" placeholder="代碼（選填）">
                <button type="submit" class="px-2 py-1" style="border:1px solid #e5e7eb;border-radius:8px;background:#fff">儲存</button>
              </form>
            </details>
          </td>
        </tr>
        """)

    html = f"""
    <!DOCTYPE html>
    <html lang="zh-Hant">
    <head>
      <meta charset="utf-8">
      <title>分店管理</title>
      <link href="/static/style.css" rel="stylesheet">
      <style>
        body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Noto Sans TC','Helvetica Neue',Arial,'PingFang TC','Microsoft JhengHei',sans-serif}}
        .container{{max-width:960px;margin:0 auto;padding:20px}}
        table{{width:100%;border-collapse:collapse}}
        th,td{{border-bottom:1px solid #e5e7eb;text-align:left}}
      </style>
    </head>
    <body class="p-4">
      <div class="container">
        <h1 class="text-2xl mb-4">分店管理</h1>

        <form action="/branches/add" method="post" class="mb-4" style="margin-bottom:16px">
          <b>新增分店：</b>
          <input type="text" name="name" placeholder="分店名稱" class="border px-2 py-1" required>
          <input type="text" name="short_code" placeholder="代碼（選填）" class="border px-2 py-1">
          <button type="submit" class="px-3 py-1" style="border:1px solid #e5e7eb;border-radius:8px;background:#fff">新增</button>
          <a href="{url_for('index')}" style="margin-left:10px;color:#2563eb">← 返回首頁</a>
        </form>

        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th class="px-2 py-2">ID</th>
                <th class="px-2 py-2">名稱</th>
                <th class="px-2 py-2">代碼</th>
                <th class="px-2 py-2">狀態</th>
                <th class="px-2 py-2">操作</th>
              </tr>
            </thead>
            <tbody>
              {''.join(rows_html) if rows_html else '<tr><td colspan="5" class="px-2 py-3" style="color:#6b7280">尚無分店</td></tr>'}
            </tbody>
          </table>
        </div>
      </div>
    </body>
    </html>
    """
    return html

@app.post('/branches/add')
def add_branch():
    init_db()
    name = (request.form.get('name') or '').strip()
    short_code = (request.form.get('short_code') or '').strip() or None
    if not name:
        return abort(400, description='名稱必填')
    with get_conn() as conn, conn.cursor() as c:
        c.execute("INSERT INTO stores (name, short_code, is_active) VALUES (%s,%s,TRUE) RETURNING id;", (name, short_code))
        new_id = c.fetchone()[0]
        conn.commit()
        write_audit(conn, 'stores', new_id, 'insert', None, {'name': name, 'short_code': short_code, 'is_active': True})
    return redirect(url_for('branch_management'))

@app.post('/branches/<int:store_id>/toggle')
def toggle_branch(store_id: int):
    init_db()
    with get_conn() as conn, conn.cursor() as c:
        c.execute("SELECT is_active, name, COALESCE(short_code,'') FROM stores WHERE id=%s;", (store_id,))
        row = c.fetchone()
        if not row:
            return abort(404)
        active, name, code = row
        before = {'name': name, 'short_code': code, 'is_active': bool(active)}
        c.execute("UPDATE stores SET is_active = NOT COALESCE(is_active, TRUE) WHERE id=%s;", (store_id,))
        conn.commit()
        c.execute("SELECT is_active FROM stores WHERE id=%s;", (store_id,))
        now_active = c.fetchone()[0]
        write_audit(conn, 'stores', store_id, 'update', before, {'is_active': bool(now_active)})
    return redirect(url_for('branch_management'))

@app.post('/branches/<int:store_id>/rename')
def rename_branch(store_id: int):
    init_db()
    name = (request.form.get('name') or '').strip()
    short_code = (request.form.get('short_code') or '').strip() or None
    if not name:
        return abort(400, description='名稱必填')
    with get_conn() as conn, conn.cursor() as c:
        c.execute("SELECT name, COALESCE(short_code,''), COALESCE(is_active,TRUE) FROM stores WHERE id=%s;", (store_id,))
        row = c.fetchone()
        if not row:
            return abort(404)
        before = {'name': row[0], 'short_code': row[1], 'is_active': bool(row[2])}
        c.execute("UPDATE stores SET name=%s, short_code=%s WHERE id=%s;", (name, short_code, store_id))
        conn.commit()
        write_audit(conn, 'stores', store_id, 'update', before, {'name': name, 'short_code': short_code})
    return redirect(url_for('branch_management'))


@app.route('/alerts/leave-expiring/json')
def leave_expiring_json():
    init_db()
    data = _fetch_active_employees_for_expiry()
    data = [d for d in data if 0 <= d["days_left"] <= ALERT_WINDOW_DAYS]
    data.sort(key=lambda x: x["days_left"])
    return jsonify({
        "today": date.today().isoformat(),
        "alert_within_days": ALERT_WINDOW_DAYS,
        "count": len(data),
        "items": data
    })

# -------------------------
# 月結報表（ZIP：請假彙總/員工清單/保險）
# -------------------------
@app.get('/reports')
def monthly_reports():
    init_db()
    month = request.args.get('month')
    if not month:
        month = date.today().strftime('%Y-%m')
    start = f'{month}-01'
    y, m = map(int, month.split('-'))
    if m == 12:
        next_month = f'{y+1}-01-01'
    else:
        next_month = f'{y}-{m+1:02d}-01'

    buf = io.BytesIO()
    zf = zipfile.ZipFile(buf, mode='w', compression=zipfile.ZIP_DEFLATED)

    with get_conn() as conn, conn.cursor() as c:
        # 1) 本月請假彙總（approved）
        c.execute("""
          SELECT e.id, e.name, lr.leave_type,
                 COALESCE(SUM(lr.hours),0) AS total_hours
            FROM leave_records lr
            JOIN employees e ON e.id = lr.employee_id
           WHERE lr.status='approved'
             AND lr.date_from >= %s AND lr.date_from < %s
           GROUP BY e.id, e.name, lr.leave_type
           ORDER BY e.id, lr.leave_type
        """, (start, next_month))
        rows = c.fetchall()
        s = io.StringIO(); w = csv.writer(s)
        w.writerow(['員工ID','姓名','假別','本月合計(小時)'])
        for r in rows:
            w.writerow(r)
        zf.writestr('leave_summary.csv', '\ufeff' + s.getvalue())

        # 2) 當月在職員工清單
        c.execute("""
          SELECT id, name, department, job_level, salary_grade, base_salary, position_allowance, start_date, end_date, store_id
            FROM employees
           WHERE (end_date IS NULL OR end_date >= %s)
           ORDER BY id
        """, (start,))
        s = io.StringIO(); w = csv.writer(s)
        w.writerow(['ID','姓名','部門','職等','薪資級距','底薪','職務津貼','到職日','離職日','分店ID'])
        for r in c.fetchall():
            w.writerow(r)
        zf.writestr('employees.csv', '\ufeff' + s.getvalue())

        # 3) 保險負擔（在職）
        c.execute("""
          SELECT e.id, e.name,
                 i.personal_labour, i.personal_health,
                 i.company_labour, i.company_health,
                 i.retirement6, i.occupational_ins, i.total_company, i.note
            FROM employees e
            LEFT JOIN insurances i ON e.id = i.employee_id
           WHERE (e.end_date IS NULL OR e.end_date >= %s)
           ORDER BY e.id
        """, (start,))
        s = io.StringIO(); w = csv.writer(s)
        w.writerow(['ID','姓名','個人勞保','個人健保','公司勞保','公司健保','退6%','職保','公司負擔合計','備註'])
        for r in c.fetchall():
            w.writerow(r)
        zf.writestr('insurances.csv', '\ufeff' + s.getvalue())

    zf.close()
    buf.seek(0)
    with get_conn() as conn:
        write_audit(conn, 'reports', 0, 'report', None, {'month': month})
    return send_file(buf, mimetype='application/zip', as_attachment=True,
                     download_name=f'reports_{month}.zip')

# -------------------------
# 全庫備份（CSV ZIP）
# -------------------------
@app.get('/admin/backup')
def admin_backup():
    init_db()
    if BACKUP_TOKEN and request.args.get('token') != BACKUP_TOKEN:
        return abort(403)

    tables = ['stores', 'store_departments', 'employees', 'insurances', 'leave_records', 'audit_logs']
    buf = io.BytesIO()
    zf = zipfile.ZipFile(buf, mode='w', compression=zipfile.ZIP_DEFLATED)

    with get_conn() as conn, conn.cursor() as c:
        for t in tables:
            c.execute(f"SELECT * FROM {t}")
            colnames = [desc[0] for desc in c.description]
            s = io.StringIO(); w = csv.writer(s)
            w.writerow(colnames)
            for row in c.fetchall():
                w.writerow(row)
            zf.writestr(f'{t}.csv', '\ufeff' + s.getvalue())

    zf.close()
    buf.seek(0)
    with get_conn() as conn:
        write_audit(conn, 'backup', 0, 'backup', None, {'by': getattr(g,'current_user', None)})
    return send_file(buf, mimetype='application/zip', as_attachment=True,
                     download_name=f'backup_{date.today().isoformat()}.zip')

# -------------------------
# 啟動
# -------------------------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
