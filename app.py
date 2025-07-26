from flask import Flask, render_template, request, redirect, url_for
from models import calculate_seniority, entitled_leave_days, entitled_sick_days, entitled_personal_days, entitled_marriage_days
from datetime import datetime
import os
import psycopg
import socket
from urllib.parse import urlparse

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
    """建立或升級 employees / insurances 資料表"""
    with get_conn() as conn, conn.cursor() as c:
        # 確保欄位存在
        c.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS job_level TEXT;")
        c.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS base_salary INTEGER;")
        c.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS position_allowance INTEGER;")
        c.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS entitled_sick INTEGER;")
        c.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS used_sick INTEGER;")
        c.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS entitled_personal INTEGER;")
        c.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS used_personal INTEGER;")
        c.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS entitled_marriage INTEGER;")
        c.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS used_marriage INTEGER;")
        conn.commit()
        # 建立表格
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
                entitled_sick INTEGER,
                used_sick INTEGER,
                entitled_personal INTEGER,
                used_personal INTEGER,
                entitled_marriage INTEGER,
                used_marriage INTEGER
            );
        ''')
        conn.commit()
        # insurances表欄位檢查與建立
        c.execute("ALTER TABLE insurances ADD COLUMN IF NOT EXISTS retirement6 INTEGER;")
        c.execute("ALTER TABLE insurances ADD COLUMN IF NOT EXISTS occupational_ins INTEGER;")
        c.execute("ALTER TABLE insurances ADD COLUMN IF NOT EXISTS total_company INTEGER;")
        c.execute("ALTER TABLE insurances ADD COLUMN IF NOT EXISTS note TEXT;")
        conn.commit()
        c.execute('''
            CREATE TABLE IF NOT EXISTS insurances (
                id SERIAL PRIMARY KEY,
                employee_id INTEGER UNIQUE REFERENCES employees(id),
                personal_labour INTEGER,
                personal_health INTEGER,
                company_labour INTEGER,
                company_health INTEGER,
                retirement6 INTEGER,
                occupational_ins INTEGER,
                total_company INTEGER,
                note TEXT
            );
        ''')
        conn.commit()

@app.route('/')
def index():
    """員工特休總覽"""
    init_db()
    with get_conn() as conn, conn.cursor() as c:
        c.execute('''
            SELECT
                id, name, start_date, end_date, department,
                job_level, salary_grade, base_salary, position_allowance,
                on_leave_suspend, used_leave,
                entitled_sick, used_sick,
                entitled_personal, used_personal,
                entitled_marriage, used_marriage
            FROM employees
        ''')
        rows = c.fetchall()

    employees = []
    for (sid, name, sd, ed, dept,
         level, grade, base, allowance,
         suspend, used,
         sick_ent, sick_used,
         per_ent, per_used,
         mar_ent, mar_used) in rows:

        # 將 None 轉成 0
        sick_ent   = sick_ent   or 0
        sick_used  = sick_used  or 0
        per_ent    = per_ent    or 0
        per_used   = per_used   or 0
        mar_ent    = mar_ent    or 0
        mar_used   = mar_used   or 0

        # 計算年資：若有離職日，用離職日；否則用到職日
        ref_date = ed or sd
        if isinstance(ref_date, str):
            ref_date = datetime.strptime(ref_date, '%Y-%m-%d').date()
        years, months = calculate_seniority(ref_date)
        entitled = entitled_leave_days(years, months, suspend)

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
        })

    return render_template('index.html', employees=employees)

@app.route('/add', methods=['GET','POST'])
def add_employee():
    init_db()
    if request.method == 'POST':
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
               # 重新依年資計算（忽略表單值）
        sd_date = datetime.strptime(request.form['start_date'], '%Y-%m-%d').date()
        years, months = calculate_seniority(sd_date)
        sick_ent = entitled_sick_days(years, months)
        per_ent  = entitled_personal_days(years, months)
        mar_ent  = entitled_marriage_days()
         # ===== 自動計算各假別應有天數 =====
        sd_date = datetime.strptime(start_date, '%Y-%m-%d').date()
        years, months = calculate_seniority(sd_date)
        # 特休（留停才歸零）
        entitled = entitled_leave_days(years, months, suspend)
        # 其餘假別全照勞基法
        sick_ent = entitled_sick_days(years, months)      # 病假 30
        per_ent  = entitled_personal_days(years, months)  # 事假 7
        mar_ent  = entitled_marriage_days()               # 婚假 8
        with get_conn() as conn, conn.cursor() as c:
            c.execute('''
                INSERT INTO employees (
                  name, start_date, end_date,
                  department, job_level, salary_grade,
                  base_salary, position_allowance,
                  on_leave_suspend, used_leave,
                  entitled_leave,   -- 特休
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
                sick_ent,    # used_sick 預設 0
                per_ent,     # used_personal 預設 0
                mar_ent      # used_marriage 預設 0
            ))
            conn.commit()
        return redirect(url_for('index'))
    return render_template('add_employee.html')

@app.route('/edit/<int:emp_id>', methods=['GET','POST'])
def edit_employee(emp_id):
    init_db()
    if request.method == 'POST':
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
        sick_ent   = int(request.form.get('entitled_sick') or 0)
        sick_used  = int(request.form.get('used_sick') or 0)
        per_ent    = int(request.form.get('entitled_personal') or 0)
        per_used   = int(request.form.get('used_personal') or 0)
        mar_ent    = int(request.form.get('entitled_marriage') or 0)
        mar_used   = int(request.form.get('used_marriage') or 0)
        with get_conn() as conn, conn.cursor() as c:
            c.execute('''
                UPDATE employees SET
                  name=%s, start_date=%s, end_date=%s,
                  department=%s, job_level=%s, salary_grade=%s,
                  base_salary=%s, position_allowance=%s,
                  on_leave_suspend=%s, used_leave=%s,
                  entitled_sick=%s, used_sick=%s,
                  entitled_personal=%s, used_personal=%s,
                  entitled_marriage=%s, used_marriage=%s
                WHERE id=%s
            ''',
             (name, start_date, end_date, dept, level, grade,
              base, allowance, suspend, used,               
              sick_ent, sick_used, per_ent, per_used, mar_ent, mar_used, emp_id))
            conn.commit()
        return redirect(url_for('index'))
    with get_conn() as conn, conn.cursor() as c:
        c.execute('SELECT * FROM employees WHERE id=%s', (emp_id,))
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

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

