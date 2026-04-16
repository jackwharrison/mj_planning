# Jack & Minke — Life Planner

A shared life planning app for Jack and Minke. Built with Flask, SQLite (local) / PostgreSQL (production).

## Local setup

```bash
# 1. Clone the repo
git clone https://github.com/YOUR_USERNAME/jackandminke.git
cd jackandminke

# 2. Create a virtual environment
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set up environment variables
cp .env.example .env
# Edit .env with your values (see notes below)

# 5. Run the app
python app.py
```

Open http://localhost:5000 in your browser.

---

## Email reminders (Gmail)

To enable email reminders:

1. Go to https://myaccount.google.com/apppasswords
2. Generate an App Password for "Mail"
3. Add to `.env`:
   ```
   SMTP_USER=your@gmail.com
   SMTP_PASS=xxxx-xxxx-xxxx-xxxx   # the App Password
   JACK_EMAIL=jack@example.com
   MINKE_EMAIL=minke@example.com
   ```

Reminders run daily at 8:00 AM. Each goal can have its own reminder schedule.

---

## Deploy to Render

1. Push this repo to GitHub
2. Go to https://render.com → New → Web Service
3. Connect your GitHub repo
4. Settings:
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `gunicorn app:app`
   - **Environment:** Python 3
5. Add environment variables in Render dashboard (copy from `.env.example`)
6. For the database, add a **Render PostgreSQL** instance and set `DATABASE_URL`

> Tip: In Render, set `APP_URL` to your Render service URL so reminder email links work correctly.

---

## Features

- **Dashboard** — progress rings for Jack & Minke, active goals, upcoming events
- **Goals** — add/edit/delete goals with progress tracking, assignee, deadline, category
- **Timeline** — Gantt-style year view of all goals
- **Calendar** — monthly view with events and goal deadlines
- **Email reminders** — configurable per-goal reminders via Gmail/SMTP

## Stack

- Flask + SQLAlchemy
- SQLite (local) / PostgreSQL (production)
- APScheduler for background reminder jobs
- Jinja2 templates (no JS framework needed)
- Deployed on Render
