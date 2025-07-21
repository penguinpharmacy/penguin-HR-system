from flask import Flask, render_template, request, redirect, url_for
from models import calculate_seniority, entitled_leave_days
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
    # 解析 URL
    result = urlparse(dsn)
    host = result.hostname
    port = result.port or 5432
    user = result.username
    password = result.password
    dbname = result.path.lstrip('/')
    # IPv4 解析
    ipv4 = socket.getaddrinfo(host, port, socket.AF_INET)[0][4][0]
    # 建立連線
    return psycopg.connect(
        host=ipv4,
        port=port,
        user=user,
        password=password,
        dbname=dbname,
        sslmode='require'
    )

def init_db():
    with get_conn() as conn, conn.cursor() as c:
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
    """員工特休總覽，首頁進來先確保有 table"""
    init_db()
    with get_conn() as conn, conn.cursor() as c:
        c.execute('''
            SELECT id, name, start_date, end_date, department,
                   job_level, salary_grade, base_salary, position_allowance,
                   on_leave_suspend, used_leave,
                   entitled_sick, used_sick,
                   entitled_personal, used_personal,
                   entitled_marriage, used_marriage
              FROM employees;
        ''')
        rows = c.fetchall()

    employees = []
    for sid, name, sd, ed, dept, grade, suspend, used in rows:
        # 計算年資：若有離職日，用離職日計算，否則用今天
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
            'salary_grade': grade,
            'years': years,
            'months': months,
            'entitled': entitled,
            'used': used,
            'remaining': max(entitled - used, 0),
            'suspend': suspend
        })

    return render_template('index.html', employees=employees)


@app.route('/add', methods=['GET', 'POST'])
def add_employee():
    """新增員工前也先確保有 table"""
    init_db()
    if request.method == 'POST':
        name       = request.form['name']
        start_date = request.form['start_date']
        end_date   = request.form.get('end_date') or None
        dept       = request.form['department']
        grade      = request.form['salary_grade']
        suspend    = bool(request.form.get('suspend'))
        used       = int(request.form.get('used_leave') or 0)

        with get_conn() as conn, conn.cursor() as c:
            c.execute('''
                INSERT INTO employees
                  (name, start_date, end_date, department, salary_grade, on_leave_suspend, used_leave)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
            ''', (name, start_date, end_date, dept, grade, suspend, used))
            conn.commit()
        return redirect(url_for('index'))

    return render_template('add_employee.html')


@app.route('/edit/<int:emp_id>', methods=['GET', 'POST'])
def edit_employee(emp_id):
    """編輯員工：更新所有欄位"""
    init_db()
    if request.method == 'POST':
        name       = request.form['name']
        start_date = request.form['start_date']
        end_date   = request.form.get('end_date') or None
        dept       = request.form['department']
        grade      = request.form['salary_grade']
        suspend    = bool(request.form.get('suspend'))
        used       = int(request.form.get('used_leave') or 0)

        with get_conn() as conn, conn.cursor() as c:
            c.execute('''
                UPDATE employees SET
                  name=%s, start_date=%s, end_date=%s,
                  department=%s, salary_grade=%s,
                  on_leave_suspend=%s, used_leave=%s
                WHERE id=%s
            ''', (name, start_date, end_date, dept, grade, suspend, used, emp_id))
            conn.commit()
        return redirect(url_for('index'))

    with get_conn() as conn, conn.cursor() as c:
        c.execute('''
            SELECT id, name, start_date, end_date,
                   department, salary_grade,
                   on_leave_suspend, used_leave
              FROM employees WHERE id=%s
        ''', (emp_id,))
        r = c.fetchone()

    

    return render_template('edit_employee.html', emp=r)


@app.route('/insurance')
def list_insurance():
    """列出所有員工的保險負擔"""
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

    employees = []
    for (sid, name, sd, ed, dept,
         level, grade, base, allowance,
         suspend, used,
         sick_ent, sick_used,
         per_ent, per_used,
         mar_ent, mar_used) in rows:
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
            'remaining_marriage': max(mar_ent - mar_used, 0)
        })

    return render_template('index.html', employees=employees)

# 其餘 /add, /edit, /insurance 等路由同理要加對應欄位讀寫

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)


@app.route('/insurance/edit/<int:emp_id>', methods=['GET', 'POST'])
def edit_insurance(emp_id):
    """新增或編輯某位員工的保險負擔"""
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
            request.form.get('note', ''),
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
