from flask import Flask, render_template, request, redirect, url_for, abort
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

app = Flask(__name__)

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

def _form_used_leave_hours() -> Decimal:
    """
    讀「已用特休」：優先 used_leave_hours（小時），否則讀 used_leave（天）×8。
    表單還沒改成小時也沒關係。
    """
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
    """
    讀請假表單：優先讀 hours（小時，0.5 單位），否則讀 days（天）×8。
    """
    val_h = request.form.get(hours_name)
    if val_h not in (None, ''):
        return _parse_half_hour(val_h)
    val_d = request.form.get(days_name)
    if val_d not in (None, ''):
        # 舊表單：天數（整天）→ 轉成小時
        try:
            d = Decimal(str(val_d))
        except InvalidOperation:
            d = Decimal('0')
        if d <= 0:
            raise ValueError("天數需大於 0")
        return d * 8
    raise ValueError("請輸入請假時數")

# -------------------------
# 資料表初始化/升級（自動跑）
# -------------------------
def init_db():
    with get_conn() as conn, conn.cursor() as c:
        # employees
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
              used_leave_hours NUMERIC(8,1)       -- 新：小時
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
        # employees：特休調整（小時，允許正負），預設 0
        c.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS leave_adjust_hours NUMERIC(8,1) DEFAULT 0;")
        conn.commit()

        # insurances
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

        # leave_records（加入 hours）
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
              created_at   TIMESTAMP DEFAULT NOW()
            );
        ''')
        # 確保 hours 欄位存在（若舊表沒有）
        c.execute("ALTER TABLE leave_records ADD COLUMN IF NOT EXISTS hours NUMERIC(8,1);")
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

# -------------------------
# 首頁：員工特休總覽（特休用小時呈現）
# -------------------------
@app.route('/')
def index():
    init_db()
    show_all = (request.args.get('all') == '1')

    with get_conn() as conn, conn.cursor() as c:
        if show_all:
            c.execute('''
                SELECT
                    id, name, start_date, end_date,
                    department, job_level,
                    salary_grade, base_salary, position_allowance,
                    on_leave_suspend, used_leave, entitled_leave,
                    entitled_leave_hours, used_leave_hours,
                    entitled_sick, used_sick,
                    entitled_personal, used_personal,
                    entitled_marriage, used_marriage,
                    is_active
                FROM employees
                ORDER BY id
            ''')
        else:
            c.execute('''
                SELECT
                    id, name, start_date, end_date,
                    department, job_level,
                    salary_grade, base_salary, position_allowance,
                    on_leave_suspend, used_leave, entitled_leave,
                    entitled_leave_hours, used_leave_hours,
                    entitled_sick, used_sick,
                    entitled_personal, used_personal,
                    entitled_marriage, used_marriage,
                    is_active
                    leave_adjust_hours
                FROM employees
                WHERE (end_date IS NULL OR end_date >= CURRENT_DATE)
                  AND COALESCE(is_active, TRUE) = TRUE
                ORDER BY id
            ''')
        rows = c.fetchall()

    employees = []
    for (sid, name, sd, ed, dept, level, grade, base, allowance,
         suspend, used_days, ent_days,
         ent_hours, used_hours,
         sick_ent, sick_used, per_ent, per_used, mar_ent, mar_used,
         is_active) in rows:

        # 特休改用「小時」（若為空則用天數*8 回退）
        ent_h = float(ent_hours) if ent_hours is not None else float((ent_days or 0) * 8)
        used_h = float(used_hours) if used_hours is not None else float((used_days or 0) * 8)
             # 加上調整值（可正可負，None 當 0）
        adj = float(adj_hours or 0)
        ent_h = max(ent_h_base + adj, 0.0)   # 負數保護
             
        # 年資（沿用你的算法）
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
            # <<< 這三個欄位現在是「小時」 >>>
            'entitled': ent_h,
            'used': used_h,
            'remaining': max(ent_h - used_h, 0.0),
            'suspend': suspend,
            'entitled_sick': sick_ent or 0,
            'used_sick': sick_used or 0,
            'remaining_sick': max((sick_ent or 0) - (sick_used or 0), 0),
            'entitled_personal': per_ent or 0,
            'used_personal': per_used or 0,
            'remaining_personal': max((per_ent or 0) - (per_used or 0), 0),
            'entitled_marriage': mar_ent or 0,
            'used_marriage': mar_used or 0,
            'remaining_marriage': max((mar_ent or 0) - (mar_used or 0), 0),
            'is_active': is_active,
        })

    return render_template('index.html', employees=employees, show_all=show_all)

# -------------------------
# 新增員工（已用特休以「小時」儲存，表單可填天或小時）
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

        sd_date = datetime.strptime(start_date, '%Y-%m-%d').date()
        ed_date = datetime.strptime(end_date_s, '%Y-%m-%d').date() if end_date_s else None
        years, months = calculate_seniority(sd_date)

        entitled_days  = entitled_leave_days(years, months, suspend)
        entitled_hours = Decimal(str(entitled_days)) * 8
        used_hours     = _form_used_leave_hours()

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
                  is_active
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
                  %s
                )
            ''', (
                name, start_date, ed_date,
                dept, level, grade,
                base, allowance,
                suspend,
                int(used_hours / 8), int(entitled_days),       # 天（保留相容用）
                str(entitled_hours), str(used_hours),          # 小時（主用）
                sick_ent,
                per_ent,
                mar_ent,
                is_active
            ))
            conn.commit()
        return redirect(url_for('index'))
    return render_template('add_employee.html')

# -------------------------
# 編輯員工（已用特休以「小時」儲存，表單可填天或小時）
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

        sd_date = datetime.strptime(start_date, '%Y-%m-%d').date()
        ed_date = datetime.strptime(end_date_s, '%Y-%m-%d').date() if end_date_s else None
        years, months = calculate_seniority(sd_date)

        entitled_days  = entitled_leave_days(years, months, suspend)
        entitled_hours = Decimal(str(entitled_days)) * 8
        used_hours     = _form_used_leave_hours()

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
                  is_active          = %s
                WHERE id = %s
            ''', (
                name, sd_date, ed_date,
                dept, level, grade,
                base, allowance,
                suspend,
                int(used_hours / 8), int(entitled_days),  # 天（保留）
                str(entitled_hours), str(used_hours),     # 小時（主用）
                sick_ent, 0,
                per_ent,  0,
                mar_ent,  0,
                is_active,
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
              entitled_leave_hours, used_leave_hours
            FROM employees
            WHERE id = %s
        ''', (emp_id,))
        r = c.fetchone()
    return render_template('edit_employee.html', emp=r)

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
            return redirect(url_for('list_insurance'))

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
# 請假紀錄（小時制）
# -------------------------
@app.route('/history/<int:emp_id>/<leave_type>')
def leave_history(emp_id, leave_type):
    init_db()
    with get_conn() as conn, conn.cursor() as c:
        c.execute('SELECT name FROM employees WHERE id=%s', (emp_id,))
        name = c.fetchone()[0]
        c.execute('''
            SELECT id, date_from, date_to, hours, days, note, created_at
              FROM leave_records
             WHERE employee_id=%s
               AND leave_type=%s
             ORDER BY date_from DESC
        ''', (emp_id, leave_type))
        rows = c.fetchall()

    records = []
    for rid, df, dt, hours, days, note, created in rows:
        records.append(SimpleNamespace(
            id=rid,
            start_date = df.strftime('%Y-%m-%d'),
            end_date   = dt.strftime('%Y-%m-%d'),
            hours      = float(hours or 0),   # 新：小時
            days       = int(days or 0),      # 舊：天（相容模板用）
            note       = note or '',
            created_at = created.strftime('%Y-%m-%d %H:%M')
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
        hours = _form_hours_or_days('hours', 'days')  # 可接收 hours 或 days
        # days 欄位只保留相容（整數即可）
        days_int = int(hours // 8)

        with get_conn() as conn, conn.cursor() as c:
            c.execute('''
                INSERT INTO leave_records
                  (employee_id, leave_type, date_from, date_to, hours, days, note)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
            ''', (emp_id, leave_type, df, dt, str(hours), days_int, note))
            conn.commit()
        return redirect(url_for('leave_history', emp_id=emp_id, leave_type=leave_type))

    # 你若有獨立的 add_leave.html 就會用到 GET；目前大多直接在 history.html 新增，這裡不會被走到
    return render_template('add_leave.html', emp_id=emp_id, leave_type=leave_type)

@app.route('/history/<int:emp_id>/<leave_type>/edit/<int:record_id>', methods=['GET','POST'])
def edit_leave_record(emp_id, leave_type, record_id):
    init_db()
    if request.method == 'POST':
        df   = request.form['start_date']
        dt   = request.form['end_date']
        note = request.form.get('note','')
        hours = _form_hours_or_days('hours', 'days')  # 可接收 hours 或 days
        days_int = int(hours // 8)

        with get_conn() as conn, conn.cursor() as c:
            c.execute('''
                UPDATE leave_records
                   SET date_from = %s,
                       date_to   = %s,
                       hours     = %s,
                       days      = %s,
                       note      = %s
                 WHERE id = %s
            ''', (df, dt, str(hours), days_int, note, record_id))
            conn.commit()
        return redirect(url_for('leave_history', emp_id=emp_id, leave_type=leave_type))

    # GET：讀一筆紀錄填到表單（同時提供 hours & days，兩種模板都相容）
    with get_conn() as conn, conn.cursor() as c:
        c.execute('''
            SELECT date_from, date_to, hours, days, note
              FROM leave_records
             WHERE id=%s
        ''', (record_id,))
        df, dt, hours, days, note = c.fetchone()

    return render_template('edit_leave.html',
                           emp_id=emp_id,
                           leave_type=leave_type,
                           record_id=record_id,
                           start_date=df.strftime('%Y-%m-%d'),
                           end_date  =dt.strftime('%Y-%m-%d'),
                           hours     =float(hours or 0),
                           days      =int(days or 0),
                           note      =note or '')

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
# 軟刪除/還原（維持不變）
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
# 啟動
# -------------------------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
