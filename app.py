from flask import Flask, render_template, request, redirect, url_for, abort
from models import calculate_seniority, entitled_leave_days, entitled_sick_days, entitled_personal_days, entitled_marriage_days
from datetime import datetime, date
import os
import psycopg
import socket
from urllib.parse import urlparse
from types import SimpleNamespace


app = Flask(__name__)

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

def init_db():
    """建立或升級 employees / insurances / leave_records 資料表"""
    with get_conn() as conn, conn.cursor() as c:
        # 確保 employees 欄位
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

        # 建立 employees 表（若不存在）
        c.execute('''
            CREATE TABLE IF NOT EXISTS employees (
              id SERIAL PRIMARY KEY,
              name TEXT, start_date DATE, end_date DATE,
              department TEXT, job_level TEXT, salary_grade TEXT,
              base_salary INTEGER, position_allowance INTEGER,
              on_leave_suspend BOOLEAN, used_leave INTEGER,
              entitled_leave INTEGER,
              entitled_sick INTEGER, used_sick INTEGER,
              entitled_personal INTEGER, used_personal INTEGER,
              entitled_marriage INTEGER, used_marriage INTEGER,
              is_active BOOLEAN DEFAULT TRUE
            );
        ''')
        conn.commit()

        # insurances 部分（同原）
        c.execute("ALTER TABLE insurances ADD COLUMN IF NOT EXISTS retirement6 INTEGER;")
        c.execute("ALTER TABLE insurances ADD COLUMN IF NOT EXISTS occupational_ins INTEGER;")
        c.execute("ALTER TABLE insurances ADD COLUMN IF NOT EXISTS total_company INTEGER;")
        c.execute("ALTER TABLE insurances ADD COLUMN IF NOT EXISTS note TEXT;")
        conn.commit()
        c.execute('''
            CREATE TABLE IF NOT EXISTS insurances (
              id SERIAL PRIMARY KEY,
              employee_id INTEGER UNIQUE REFERENCES employees(id),
              personal_labour INTEGER, personal_health INTEGER,
              company_labour INTEGER, company_health INTEGER,
              retirement6 INTEGER, occupational_ins INTEGER,
              total_company INTEGER, note TEXT
            );
        ''')
        conn.commit()

        # —— 新增請假紀錄表 leave_records （只要宣告一次） —— 
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


@app.route('/')
def index():
    """員工特休總覽：預設只顯示在職，?all=1 則顯示所有（含離職）"""
    init_db()

    # 讀 query string 決定要不要顯示所有員工
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
                WHERE is_active = TRUE
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

        # 把 None 變 0
        sick_ent  = sick_ent  or 0
        sick_used = sick_used or 0
        per_ent   = per_ent   or 0
        per_used  = per_used  or 0
        mar_ent   = mar_ent   or 0
        mar_used  = mar_used  or 0

        # 計算年資（如果要用離職日，ref_date 就是 ed；否則用 sd）
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
            'remaining': max(entitled - used, 0),
            'suspend': suspend,
            'entitled_sick': sick_ent,
            'used_sick': sick_used,
            'remaining_sick': max(sick_ent - sick_used, 0),
            'entitled_personal': per_ent,
            'used_personal': per_used,
            'remaining_personal': max(per_ent - per_used, 0),
            'entitled_marriage': mar_ent,
            'used_marriage': mar_used,
            'remaining_marriage': max(mar_ent - mar_used, 0),
            'is_active': is_active,
        })

    # 一併把 show_all 傳給模板，讓按鈕能切換
    return render_template('index.html',
                           employees=employees,
                           show_all=show_all)


@app.route('/add', methods=['GET','POST'])
def add_employee():
    init_db()
    if request.method == 'POST':
        # 1. 讀表單
        name       = request.form['name']
        start_date = request.form['start_date']
        end_date   = request.form.get('end_date') or None
        dept       = request.form['department']
        level      = request.form.get('job_level') or ''
        grade      = request.form['salary_grade']
        base       = int(request.form.get('base_salary') or 0)
        allowance  = int(request.form.get('position_allowance') or 0)
        suspend    = bool(request.form.get('suspend'))
        used       = int(request.form.get('used_leave') or 0)

        # 2. 自動計算各假別應有天數
        sd_date = datetime.strptime(start_date, '%Y-%m-%d').date()
        years, months = calculate_seniority(sd_date)

        # 特休（留停歸零）
        entitled = entitled_leave_days(years, months, suspend)
        # 其餘假別（對照勞基法固定值）
        sick_ent = entitled_sick_days(years, months)      # 病假 30
        per_ent  = entitled_personal_days(years, months)  # 事假 14
        mar_ent  = entitled_marriage_days()               # 婚假 8

        # 3. 插入
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
                  entitled_marriage, used_marriage
                ) VALUES (
                  %s,%s,%s,
                  %s,%s,%s,
                  %s,%s,
                  %s,%s,
                  %s,
                  %s,0,
                  %s,0,
                  %s,0
                )
            ''', (
                name, start_date, end_date,
                dept, level, grade,
                base, allowance,
                suspend, used,
                entitled,
                sick_ent,
                per_ent,
                mar_ent
            ))
            conn.commit()
        return redirect(url_for('index'))
    return render_template('add_employee.html')

@app.route('/edit/<int:emp_id>', methods=['GET','POST'])
def edit_employee(emp_id):
    init_db()

    if request.method == 'POST':
        # 1. 讀取表單
        name       = request.form['name']
        start_date = request.form['start_date']
        end_date   = request.form.get('end_date') or None
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

        # 2. 自動重新計算應有天數
        sd_date = datetime.strptime(start_date, '%Y-%m-%d').date()
        years, months    = calculate_seniority(sd_date)
        entitled         = entitled_leave_days(years, months, suspend)
        sick_ent         = entitled_sick_days(years, months)
        per_ent          = entitled_personal_days(years, months)
        mar_ent          = entitled_marriage_days()

        # 3. 根據 end_date 決定在職狀態
        is_active = False if end_date else True

        # 4. 更新所有欄位（含 is_active）
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
                name, start_date, end_date,
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

    # GET: 讀取原本資料填入表單
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

@app.route('/insurance')
def list_insurance():
    init_db()
    with get_conn() as conn, conn.cursor() as c:
        c.execute('''
            SELECT e.id, e.name,
                   i.personal_labour, i.personal_health,
                   i.company_labour, i.company_health,
                   i.retirement6, i.occupational_ins,
                   i.total_company, i.note
              FROM employees e
              LEFT JOIN insurances i ON e.id = i.employee_id
        ''')
        rows = c.fetchall()
    return render_template('insurance.html', items=rows)

@app.route('/insurance/edit/<int:emp_id>', methods=['GET','POST'])
def edit_insurance(emp_id):
    init_db()
    if request.method == 'POST':
        vals = [
            int(request.form.get('personal_labour') or 0),
            int(request.form.get('personal_health') or 0),
            int(request.form.get('company_labour') or 0),
            int(request.form.get('company_health') or 0),
            int(request.form.get('retirement6') or 0),
            int(request.form.get('occupational_ins') or 0),
            int(request.form.get('total_company') or 0),
            request.form.get('note',''),
            emp_id
        ]
        with get_conn() as conn, conn.cursor() as c:
            c.execute('SELECT id FROM insurances WHERE employee_id=%s', (emp_id,))
            if c.fetchone():
                c.execute('''
                    UPDATE insurances SET
                      personal_labour=%s, personal_health=%s,
                      company_labour=%s, company_health=%s,
                      retirement6=%s, occupational_ins=%s,
                      total_company=%s, note=%s
                    WHERE employee_id=%s
                ''', vals)
            else:
                c.execute('''
                    INSERT INTO insurances
                      (personal_labour, personal_health,
                       company_labour, company_health,
                       retirement6, occupational_ins,
                       total_company, note, employee_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ''', vals)
            conn.commit()
        return redirect(url_for('list_insurance'))
    with get_conn() as conn, conn.cursor() as c:
        c.execute('SELECT * FROM insurances WHERE employee_id=%s', (emp_id,))
        r = c.fetchone() or [None, emp_id,0,0,0,0,0,0,0,'']
    return render_template('edit_insurance.html', emp_id=emp_id, ins=r)

@app.route('/delete/<int:emp_id>')
def delete_employee(emp_id):
    """軟刪除：把 is_active 設為 False, 並填離職日"""
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

@app.route('/history/<int:emp_id>/<leave_type>')
def leave_history(emp_id, leave_type):
    init_db()
    # 1. 拿姓名
    with get_conn() as conn, conn.cursor() as c:
        c.execute('SELECT name FROM employees WHERE id=%s', (emp_id,))
        name = c.fetchone()[0]
        # 2. 拿請假紀錄
        c.execute('''
            SELECT id, date_from, date_to, days, note, created_at
              FROM leave_records
             WHERE employee_id=%s
               AND leave_type=%s
             ORDER BY date_from DESC
        ''', (emp_id, leave_type))
        rows = c.fetchall()

    # 3. 組成 SimpleNamespace list，讓模板能用 r.id、r.start_date 等
    records = []
    for rid, df, dt, days, note, created in rows:
        records.append(SimpleNamespace(
            id=rid,
            start_date=df.strftime('%Y-%m-%d'),
            end_date=dt.strftime('%Y-%m-%d'),
            days=days,
            note=note or '',
            created_at=created.strftime('%Y-%m-%d %H:%M')
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
        return redirect(url_for('leave_history',
                                emp_id=emp_id,
                                leave_type=leave_type))

    # GET：顯示新增表單
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
                   SET date_from = %s, date_to = %s, days = %s, note = %s
                 WHERE id = %s
            ''', (df, dt, days, note, record_id))
            conn.commit()
        return redirect(url_for('leave_history',
                                emp_id=emp_id,
                                leave_type=leave_type))

    # GET：讀一筆紀錄填到表單
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
                           end_date=dt.strftime('%Y-%m-%d'),
                           days=days,
                           note=note or '')

@app.route('/salary/<int:emp_id>')
def salary_detail(emp_id):
    """顯示某員工的勞健保／職保負擔明細"""
    init_db()
    # 先查員工基本資料
    with get_conn() as conn, conn.cursor() as c:
        c.execute('SELECT name, salary_grade FROM employees WHERE id=%s', (emp_id,))
        row = c.fetchone()
        if not row:
            return abort(404)
        name, grade = row

    # 再查該員工的勞健保與職保負擔（insurances 表）
    with get_conn() as conn, conn.cursor() as c:
        c.execute('''
            SELECT personal_labour, personal_health,
                   company_labour, company_health,
                   retirement6, occupational_ins, total_company, note
              FROM insurances
             WHERE employee_id = %s
        ''', (emp_id,))
        ins = c.fetchone() or (0,0,0,0,0,0,0,'')  # 若無則用 0
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

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

