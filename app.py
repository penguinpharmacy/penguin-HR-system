from flask import Flask, render_template, request, redirect, url_for, abort
from models import calculate_seniority, entitled_leave_days, entitled_sick_days, entitled_personal_days, entitled_marriage_days
from datetime import datetime, date
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
    """
    解析 DATABASE_URL（Transaction Pooler），強制走 IPv4，並加入 sslmode=require
    """
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
# 資料表初始化/升級
# -------------------------
def init_db():
    """建立或升級 employees / insurances / leave_records 資料表"""
    with get_conn() as conn, conn.cursor() as c:
        # 先 CREATE，再 ALTER（避免舊程式先 ALTER 找不到表）
        c.execute('''
            CREATE TABLE IF NOT EXISTS employees (
              id SERIAL PRIMARY KEY,
              name TEXT,
              start_date DATE,
              end_date DATE,
              department TEXT,
              job_level TEXT,
              salary_grade TEXT,
              base_salary INTEGER,
              position_allowance INTEGER,
              on_leave_suspend BOOLEAN,
              used_leave INTEGER,
              entitled_leave INTEGER,
              entitled_sick INTEGER,
              used_sick INTEGER,
              entitled_personal INTEGER,
              used_personal INTEGER,
              entitled_marriage INTEGER,
              used_marriage INTEGER,
              is_active BOOLEAN DEFAULT TRUE
            );
        ''')
        conn.commit()

        # 升級：有缺就補
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
        conn.commit()

        # 保險表
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
        c.execute("""
            ALTER TABLE insurances
              ALTER COLUMN id
              SET DEFAULT nextval('insurances_id_seq');
        """)
        c.execute("""
            ALTER SEQUENCE insurances_id_seq
              OWNED BY insurances.id;
        """)
        conn.commit()

        # 請假紀錄表
        c.execute('''
            CREATE TABLE IF NOT EXISTS leave_records (
              id           SERIAL PRIMARY KEY,
              employee_id  INTEGER REFERENCES employees(id),
              leave_type   TEXT    NOT NULL,
              date_from    DATE    NOT NULL,
              date_to      DATE    NOT NULL,
              days         INTEGER NOT NULL,
              note         TEXT,
              created_at   TIMESTAMP DEFAULT NOW()
            );
        ''')
        conn.commit()

# -------------------------
# 共用：在職判定（SQL 與表單一致）
# -------------------------
def is_active_by_end_date(ed: date | None) -> bool:
    """end_date 為空 或 >= 今天 視為在職"""
    return (ed is None) or (ed >= date.today())

def _is_employee_active(conn, emp_id: int) -> bool:
    """查 DB 判斷員工是否在職（日期＋is_active 手動開關）"""
    with conn.cursor() as c:
        c.execute("""
          SELECT
            (end_date IS NULL OR end_date >= CURRENT_DATE)
            AND COALESCE(is_active, TRUE)
          FROM employees
          WHERE id = %s
        """, (emp_id,))
        r = c.fetchone()
        return bool(r and r[0])

# -------------------------
# 首頁：員工特休總覽
# -------------------------
@app.route('/')
def index():
    """預設只顯示在職；?all=1 顯示所有（含離職/停用）"""
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
                    entitled_sick, used_sick,
                    entitled_personal, used_personal,
                    entitled_marriage, used_marriage,
                    is_active
                FROM employees
                WHERE
                  (end_date IS NULL OR end_date >= CURRENT_DATE)
                  AND COALESCE(is_active, TRUE) = TRUE
                ORDER BY id
            ''')
        rows = c.fetchall()

    employees = []
    for (sid, name, sd, ed, dept,
         level, grade, base, allowance,
         suspend, used, entitled,
         sick_ent, sick_used,
         per_ent, per_used,
         mar_ent, mar_used,
         is_active) in rows:

        sick_ent  = sick_ent  or 0
        sick_used = sick_used or 0
        per_ent   = per_ent   or 0
        per_used  = per_used  or 0
        mar_ent   = mar_ent   or 0
        mar_used  = mar_used  or 0

        # 計算年資（維持你既有邏輯）
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
            'entitled': entitled,
            'used': used,
            'remaining': max((entitled or 0) - (used or 0), 0),
            'suspend': suspend,
            'entitled_sick': sick_ent,
            'used_sick': sick_used,
            'remaining_sick': max(sick_ent - sick_used, 0),
            'entitled_personal': per_ent,
            'used_personal': per_used,
            'remaining_personal': max(per_ent - per_used, 0),
            'entitled_marriage': mar_ent,
            'used_marriage': mar_used,
            'is_active': is_active,
        })

    return render_template('index.html', employees=employees, show_all=show_all)

# -------------------------
# 新增員工
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
        used       = int(request.form.get('used_leave') or 0)

        sd_date = datetime.strptime(start_date, '%Y-%m-%d').date()
        ed_date = datetime.strptime(end_date_s, '%Y-%m-%d').date() if end_date_s else None

        years, months = calculate_seniority(sd_date)
        entitled = entitled_leave_days(years, months, suspend)
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
                  on_leave_suspend, used_leave,
                  entitled_leave,
                  entitled_sick, used_sick,
                  entitled_personal, used_personal,
                  entitled_marriage, used_marriage,
                  is_active
                ) VALUES (
                  %s,%s,%s,
                  %s,%s,%s,
                  %s,%s,
                  %s,%s,
                  %s,
                  %s,0,
                  %s,0,
                  %s,0,
                  %s
                )
            ''', (
                name, start_date, ed_date,
                dept, level, grade,
                base, allowance,
                suspend, used,
                entitled,
                sick_ent,
                per_ent,
                mar_ent,
                is_active
            ))
            conn.commit()
        return redirect(url_for('index'))

    return render_template('add_employee.html')

# -------------------------
# 編輯員工
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
        used       = int(request.form.get('used_leave') or 0)
        sick_used  = int(request.form.get('used_sick') or 0)
        per_used   = int(request.form.get('used_personal') or 0)
        mar_used   = int(request.form.get('used_marriage') or 0)

        sd_date = datetime.strptime(start_date, '%Y-%m-%d').date()
        ed_date = datetime.strptime(end_date_s, '%Y-%m-%d').date() if end_date_s else None

        years, months = calculate_seniority(sd_date)
        entitled = entitled_leave_days(years, months, suspend)
        sick_ent = entitled_sick_days(years, months)
        per_ent  = entitled_personal_days(years, months)
        mar_ent  = entitled_marriage_days()

        # 到期日未到 = 在職
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
                  entitled_sick      = %s,  used_sick      = %s,
                  entitled_personal  = %s,  used_personal  = %s,
                  entitled_marriage  = %s,  used_marriage  = %s,
                  is_active          = %s
                WHERE id = %s
            ''', (
                name, sd_date, ed_date,
                dept, level, grade,
                base, allowance,
                suspend, used,
                entitled,
                sick_ent, sick_used,
                per_ent, per_used,
                mar_ent, mar_used,
                is_active,
                emp_id
            ))
            conn.commit()

        return redirect(url_for('index'))

    # GET
    with get_conn() as conn, conn.cursor() as c:
        c.execute('''
            SELECT
              id, name, start_date, end_date,
              department, job_level, salary_grade,
              base_salary, position_allowance,
              on_leave_suspend, used_leave,
              entitled_leave,
              entitled_sick, used_sick,
              entitled_personal, used_personal,
              entitled_marriage, used_marriage
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
                        ) VALUES (
                          %s, %s, %s, %s, %s, %s, %s, %s, %s
                        )
                    ''', (emp_id, pl, ph, cl, ch, r6, oi, tot, note))
                conn.commit()

            return redirect(url_for('list_insurance'))

        # GET：載入資料
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
# 軟刪除/還原
# -------------------------
@app.route('/delete/<int:emp_id>')
def delete_employee(emp_id):
    """軟刪除：把 is_active 設為 False, 並填離職日=今天"""
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
    """軟復原：把 is_active 設回 True，並清空離職日"""
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
# 請假紀錄
# -------------------------
@app.route('/history/<int:emp_id>/<leave_type>')
def leave_history(emp_id, leave_type):
    init_db()
    with get_conn() as conn, conn.cursor() as c:
        c.execute('SELECT name FROM employees WHERE id=%s', (emp_id,))
        name = c.fetchone()[0]
        c.execute('''
            SELECT id, date_from, date_to, days, note, created_at
              FROM leave_records
             WHERE employee_id=%s
               AND leave_type=%s
             ORDER BY date_from DESC
        ''', (emp_id, leave_type))
        rows = c.fetchall()

    records = []
    for rid, df, dt, days, note, created in rows:
        records.append(SimpleNamespace(
            id=rid,
            start_date = df.strftime('%Y-%m-%d'),
            end_date   = dt.strftime('%Y-%m-%d'),
            days       = days,
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
        days = int(request.form['days'])
        note = request.form.get('note','')
        with get_conn() as conn, conn.cursor() as c:
            c.execute('''
                INSERT INTO leave_records
                  (employee_id, leave_type, date_from, date_to, days, note)
                VALUES (%s,%s,%s,%s,%s,%s)
            ''', (emp_id, leave_type, df, dt, days, note))
            conn.commit()
        return redirect(url_for('leave_history', emp_id=emp_id, leave_type=leave_type))

    return render_template('add_leave.html',
                           emp_id=emp_id,
                           leave_type=leave_type)

@app.route('/history/<int:emp_id>/<leave_type>/edit/<int:record_id>', methods=['GET','POST'])
def edit_leave_record(emp_id, leave_type, record_id):
    init_db()
    if request.method == 'POST':
        df   = request.form['start_date']
        dt   = request.form['end_date']
        days = int(request.form['days'])
        note = request.form.get('note','')
        with get_conn() as conn, conn.cursor() as c:
            c.execute('''
                UPDATE leave_records
                   SET date_from = %s,
                       date_to   = %s,
                       days      = %s,
                       note      = %s
                 WHERE id = %s
            ''', (df, dt, days, note, record_id))
            conn.commit()
        return redirect(url_for('leave_history', emp_id=emp_id, leave_type=leave_type))

    with get_conn() as conn, conn.cursor() as c:
        c.execute('''
            SELECT date_from, date_to, days, note
              FROM leave_records
             WHERE id=%s
        ''', (record_id,))
        df, dt, days, note = c.fetchone()

    return render_template('edit_leave.html',
                           emp_id=emp_id,
                           leave_type=leave_type,
                           record_id=record_id,
                           start_date=df.strftime('%Y-%m-%d'),
                           end_date  =dt.strftime('%Y-%m-%d'),
                           days      =days,
                           note      =note or '')

# -------------------------
# 薪資/保險明細
# -------------------------
@app.route('/salary/<int:emp_id>')
def salary_detail(emp_id):
    """顯示某員工的勞健保／職保負擔明細"""
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
# 啟動
# -------------------------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)


