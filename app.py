from flask import Flask, render_template, request, redirect, url_for
from models import calculate_seniority, entitled_leave_days
from datetime import datetime
import sqlite3
import os

app = Flask(__name__)
DB = 'database.db'

def init_db():
    """建立資料表（若不存在就建立）"""
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS employees (
                id INTEGER PRIMARY KEY,
                name TEXT,
                start_date TEXT,
                end_date TEXT,
                department TEXT,
                salary_grade TEXT,
                on_leave_suspend INTEGER,
                used_leave INTEGER
            )
        ''')
        conn.commit()

@app.route('/')
def index():
    """員工特休總覽，首頁進來先確保有 table"""
    init_db()

    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute('''
            SELECT id, name, start_date, end_date,
                   department, salary_grade,
                   on_leave_suspend, used_leave
            FROM employees
        ''')
        rows = c.fetchall()

    employees = []
    for sid, name, sd, ed, dept, grade, suspend, used in rows:
        sd_date = datetime.strptime(sd, '%Y-%m-%d').date()
        years, months = calculate_seniority(sd_date)
        entitled = entitled_leave_days(years, months, bool(suspend))
        remaining = max(entitled - used, 0)
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
            'remaining': remaining,
            'suspend': bool(suspend),
        })

    return render_template('index.html', employees=employees)


@app.route('/add', methods=['GET', 'POST'])
def add_employee():
    """新增員工前也先確保有 table"""
    init_db()

    if request.method == 'POST':
        name       = request.form['name']
        start_date = request.form['start_date']
        end_date   = request.form.get('end_date') or ''
        dept       = request.form['department']
        grade      = request.form['salary_grade']
        suspend    = 1 if request.form.get('suspend') else 0
        used       = int(request.form.get('used_leave') or 0)

        with sqlite3.connect(DB) as conn:
            c = conn.cursor()
            c.execute(
                '''INSERT INTO employees
                   (name, start_date, end_date, department, salary_grade, on_leave_suspend, used_leave)
                   VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (name, start_date, end_date, dept, grade, suspend, used)
            )
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
        end_date   = request.form.get('end_date') or ''
        dept       = request.form['department']
        grade      = request.form['salary_grade']
        suspend    = 1 if request.form.get('suspend') else 0
        used       = int(request.form.get('used_leave') or 0)

        with sqlite3.connect(DB) as conn:
            c = conn.cursor()
            c.execute(
                '''UPDATE employees SET
                     name=?, start_date=?, end_date=?, department=?, salary_grade=?,
                     on_leave_suspend=?, used_leave=?
                   WHERE id=?''',
                (name, start_date, end_date, dept, grade, suspend, used, emp_id)
            )
            conn.commit()
        return redirect(url_for('index'))

    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute(
            '''SELECT id, name, start_date, end_date,
                      department, salary_grade,
                      on_leave_suspend, used_leave
               FROM employees WHERE id=?''',
            (emp_id,)
        )
        r = c.fetchone()

    return render_template('edit_employee.html', emp=r)


if __name__ == '__main__':
    # 使用 Render 給的 PORT 啟動
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
