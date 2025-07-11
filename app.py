from flask import Flask, render_template, request, redirect, url_for
from models import calculate_seniority, entitled_leave_days
from datetime import datetime
import sqlite3

app = Flask(__name__)
DB = 'database.db'

def init_db():
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS employees (
                id INTEGER PRIMARY KEY,
                name TEXT,
                start_date TEXT,
                department TEXT,
                on_leave_suspend INTEGER,
                used_leave INTEGER
            )
        ''')
        conn.commit()

@app.before_first_request
def setup():
    init_db()

@app.route('/')
def index():
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute('SELECT * FROM employees')
        rows = c.fetchall()
    data = []
    for r in rows:
        sid, name, sd, dept, suspend, used = r
        sd_date = datetime.strptime(sd, '%Y-%m-%d').date()
        years, months = calculate_seniority(sd_date)
        entitled = entitled_leave_days(years, months, bool(suspend))
        remaining = max(entitled - used, 0)
        data.append({
            'id': sid, 'name': name, 'dept': dept,
            'years': years, 'months': months,
            'entitled': entitled,
            'used': used, 'remaining': remaining,
            'suspend': bool(suspend)
        })
    return render_template('index.html', employees=data)

@app.route('/add', methods=['GET','POST'])
def add_employee():
    if request.method == 'POST':
        name = request.form['name']
        start_date = request.form['start_date']
        dept = request.form['department']
        suspend = 1 if request.form.get('suspend') else 0
        used = int(request.form['used_leave'])
        with sqlite3.connect(DB) as conn:
            c = conn.cursor()
            c.execute(
                'INSERT INTO employees (name, start_date, department, on_leave_suspend, used_leave) VALUES (?,?,?,?,?)',
                (name, start_date, dept, suspend, used)
            )
            conn.commit()
        return redirect(url_for('index'))
    return render_template('add_employee.html')

@app.route('/edit/<int:emp_id>', methods=['GET','POST'])
def edit_employee(emp_id):
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        if request.method == 'POST':
            used = int(request.form['used_leave'])
            suspend = 1 if request.form.get('suspend') else 0
            c.execute(
                'UPDATE employees SET on_leave_suspend=?, used_leave=? WHERE id=?',
                (suspend, used, emp_id)
            )
            conn.commit()
            return redirect(url_for('index'))
        c.execute('SELECT * FROM employees WHERE id=?', (emp_id,))
        r = c.fetchone()
    return render_template('edit_employee.html', emp=r)

if __name__ == '__main__':
    if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

