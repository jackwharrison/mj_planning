from flask import (Flask, render_template, request, redirect,
                   jsonify, send_from_directory)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (LoginManager, UserMixin, login_user,
                         logout_user, login_required, current_user)
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, date, timedelta
import smtplib, os, uuid, json
try:
    from pywebpush import webpush, WebPushException
    _push_available = True
except ImportError:
    _push_available = False
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY']                     = os.environ.get('SECRET_KEY', 'dev-secret-change-me')

# Database location:
# - In production on Render, point at the persistent disk mounted at /var/data
#   (set DATA_DIR=/var/data as an env var on the web service).
# - Locally, fall back to a file next to app.py so dev data doesn't interfere.
# - If DATABASE_URL is set (e.g. switching to Postgres later), it takes priority.
_data_dir = os.environ.get('DATA_DIR', os.path.dirname(__file__))
os.makedirs(_data_dir, exist_ok=True)
_default_sqlite = 'sqlite:///' + os.path.join(_data_dir, 'jm.db')
app.config['SQLALCHEMY_DATABASE_URI']        = os.environ.get('DATABASE_URL', _default_sqlite)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db            = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

UPLOAD_FOLDER = os.path.join(_data_dir, 'uploads')
ALLOWED_EXT   = {'pdf','png','jpg','jpeg','gif','doc','docx','xls','xlsx','txt','zip'}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

PROJECT_HEX = {
    'blue':   '#0A84FF',
    'pink':   '#FF375F',
    'teal':   '#30B98A',
    'purple': '#BF5AF2',
    'amber':  '#FF9F0A',
}


# ── Food categories ───────────────────────────────────────────────────────────
# Single source of truth — used by the pantry form, the ingredient
# autocomplete's auto-create path, and the UI for color-coding & grouping.
# `order` drives the display sort; `other` sits at the end as the catch-all
# uncategorised bucket.
FOOD_CATEGORIES = [
    {'key': 'fruit',     'label': 'Fruit',         'icon': '🍎', 'color': 'pink',   'order': 10},
    {'key': 'veg',       'label': 'Vegetables',    'icon': '🥕', 'color': 'green',  'order': 20},
    {'key': 'meat',      'label': 'Meat',          'icon': '🥩', 'color': 'red',    'order': 30},
    {'key': 'fish',      'label': 'Fish',          'icon': '🐟', 'color': 'blue',   'order': 35},
    {'key': 'dairy',     'label': 'Dairy',         'icon': '🥛', 'color': 'sky',    'order': 40},
    {'key': 'grain',     'label': 'Grains & Carbs','icon': '🌾', 'color': 'amber',  'order': 50},
    {'key': 'bakery',    'label': 'Bakery',        'icon': '🍞', 'color': 'brown',  'order': 55},
    {'key': 'canned',    'label': 'Canned & Dry',  'icon': '🥫', 'color': 'orange', 'order': 60},
    {'key': 'condiment', 'label': 'Oils & Sauces', 'icon': '🫙', 'color': 'yellow', 'order': 65},
    {'key': 'spice',     'label': 'Spices',        'icon': '🧂', 'color': 'purple', 'order': 70},
    {'key': 'frozen',    'label': 'Frozen',        'icon': '🧊', 'color': 'ice',    'order': 75},
    {'key': 'drink',     'label': 'Drinks',        'icon': '🥤', 'color': 'teal',   'order': 80},
    {'key': 'other',     'label': 'Uncategorised', 'icon': '🍽', 'color': 'gray',   'order': 999},
]
FOOD_CATEGORY_BY_KEY = {c['key']: c for c in FOOD_CATEGORIES}

def _category_sort_key(cat_key):
    """Sort-order lookup for a category string, defaulting to 'other'."""
    return FOOD_CATEGORY_BY_KEY.get(cat_key or 'other',
                                     FOOD_CATEGORY_BY_KEY['other'])['order']


# ── Users ─────────────────────────────────────────────────────────────────────

USERS = {
    'jack': {
        'id':            'jack',
        'name':          'Jack',
        'password_hash': os.environ.get('JACK_PASSWORD_HASH', generate_password_hash('jack123')),
        'email':         os.environ.get('JACK_EMAIL', ''),
        'avatar':        'J',
        'color':         'blue',
    },
    'minke': {
        'id':            'minke',
        'name':          'Minke',
        'password_hash': os.environ.get('MINKE_PASSWORD_HASH', generate_password_hash('minke123')),
        'email':         os.environ.get('MINKE_EMAIL', ''),
        'avatar':        'M',
        'color':         'pink',
    },
}


class User(UserMixin):
    def __init__(self, data):
        self.id     = data['id']
        self.name   = data['name']
        self.avatar = data['avatar']
        self.color  = data['color']
        self.email  = data['email']

    def check_password(self, password):
        return check_password_hash(USERS[self.id]['password_hash'], password)


@login_manager.user_loader
def load_user(user_id):
    if user_id in USERS:
        return User(USERS[user_id])
    return None


# ── Email ─────────────────────────────────────────────────────────────────────

APP_URL    = os.environ.get('APP_URL', 'http://localhost:5000')

# ── Web Push / VAPID ──────────────────────────────────────────────────────────
VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY', '')
VAPID_PUBLIC_KEY  = os.environ.get('VAPID_PUBLIC_KEY',  '')
VAPID_CLAIMS      = {'sub': f"mailto:{os.environ.get('SMTP_USER', 'admin@example.com')}"}
SMTP_HOST  = os.environ.get('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT  = int(os.environ.get('SMTP_PORT', 587))
SMTP_USER  = os.environ.get('SMTP_USER', '')
SMTP_PASS  = os.environ.get('SMTP_PASS', '')
FROM_EMAIL = os.environ.get('FROM_EMAIL', SMTP_USER)


def _send(to_addresses, subject, html_body):
    """Low-level send. Silently skips if SMTP not configured."""
    if not SMTP_USER or not SMTP_PASS:
        app.logger.info(f'[email] SMTP not configured — skipping: {subject}')
        return
    if not to_addresses:
        return
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = f'Jack & Minke \u2665 <{FROM_EMAIL}>'
    msg['To']      = ', '.join(to_addresses)
    msg.attach(MIMEText(html_body, 'html'))
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(FROM_EMAIL, to_addresses, msg.as_string())
        app.logger.info(f'[email] Sent "{subject}" → {to_addresses}')
    except Exception as e:
        app.logger.error(f'[email] Failed: {e}')


def _email(user_id):
    """Return email address for a user id, or empty string."""
    return USERS.get(user_id, {}).get('email', '')


def _name(user_id):
    return USERS.get(user_id, {}).get('name', user_id.capitalize())


def _base_html(content, footer_url, footer_label):
    """Wrap content in a clean minimal email shell."""
    return f"""
    <div style="font-family:-apple-system,'Helvetica Neue',Arial,sans-serif;
                max-width:480px;margin:0 auto;padding:32px 24px;color:#1C1C1E;">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:28px;">
        <div style="width:8px;height:8px;border-radius:50%;background:#30B98A;"></div>
        <span style="font-size:16px;font-weight:600;letter-spacing:-0.3px;">Jack & Minke</span>
      </div>
      {content}
      <div style="margin-top:32px;padding-top:20px;border-top:1px solid #EAE8E2;">
        <a href="{footer_url}"
           style="display:inline-block;padding:10px 20px;background:#0A84FF;
                  color:#fff;text-decoration:none;border-radius:99px;
                  font-size:13px;font-weight:600;">
          {footer_label}
        </a>
      </div>
      <p style="margin-top:20px;font-size:11px;color:#AEAEB2;">
        Jack &amp; Minke life planner &middot;
        <a href="{APP_URL}/logout" style="color:#AEAEB2;">unsubscribe</a>
      </p>
    </div>"""


def notify_task_assigned(task):
    """Email the assignee(s) when a task is created for them."""
    recipients = []
    if task.assignee in ('jack', 'both'):
        recipients.append(_email('jack'))
    if task.assignee in ('minke', 'both'):
        recipients.append(_email('minke'))
    # Don't email the person who just created it
    recipients = [e for e in recipients if e and e != _email(current_user.id)]
    if not recipients:
        return

    due_str = task.due_date.strftime('%d/%m/%Y') if task.due_date else 'No deadline set'
    project_url = f"{APP_URL}/projects/{task.project_id}"
    content = f"""
      <h2 style="font-size:18px;font-weight:600;margin:0 0 8px;">{task.name}</h2>
      <p style="font-size:14px;color:#636366;margin:0 0 6px;">
        {current_user.name} assigned you a new task in
        <strong>{task.project.name}</strong>.
      </p>
      <p style="font-size:13px;color:#AEAEB2;margin:0;">Due: {due_str}</p>"""
    _send(recipients,
          f"New task: {task.name}",
          _base_html(content, project_url, 'View project →'))


def notify_comment(comment, task):
    """Email everyone involved in the task except the commenter."""
    involved = set()
    if task.assignee in ('jack', 'both'):
        involved.add('jack')
    if task.assignee in ('minke', 'both'):
        involved.add('minke')
    # Also include anyone who has commented
    for c in task.comments:
        involved.add(c.author)
    involved.discard(comment.author)   # don't email yourself

    recipients = [e for e in (_email(u) for u in involved) if e]
    if not recipients:
        return

    project_url = f"{APP_URL}/projects/{task.project_id}"
    content = f"""
      <p style="font-size:14px;color:#636366;margin:0 0 16px;">
        <strong>{_name(comment.author)}</strong> commented on
        <strong>{task.name}</strong>:
      </p>
      <div style="background:#F2F2F7;border-radius:4px 14px 14px 14px;
                  padding:12px 16px;font-size:14px;line-height:1.6;color:#1C1C1E;">
        {comment.body}
      </div>"""
    _send(recipients,
          f"{_name(comment.author)} commented on \"{task.name}\"",
          _base_html(content, project_url, 'View task →'))


def send_deadline_reminders():
    """Called daily at 8am. Sends reminders for tasks due in 3 days, 1 day, or today."""
    with app.app_context():
        today    = date.today()
        targets  = [today, today + timedelta(days=1), today + timedelta(days=3)]
        tasks    = Task.query.filter(
            Task.done     == False,
            Task.due_date.in_(targets),
        ).all()

        for t in tasks:
            days_left = (t.due_date - today).days
            if days_left == 0:   when = 'today'
            elif days_left == 1: when = 'tomorrow'
            else:                when = 'in 3 days'

            recipients = []
            if t.assignee in ('jack',  'both'): recipients.append(_email('jack'))
            if t.assignee in ('minke', 'both'): recipients.append(_email('minke'))
            recipients = [e for e in recipients if e]
            if not recipients:
                continue

            project_url = f"{APP_URL}/projects/{t.project_id}"
            content = f"""
              <h2 style="font-size:18px;font-weight:600;margin:0 0 8px;">{t.name}</h2>
              <p style="font-size:14px;color:#636366;margin:0 0 6px;">
                This task is due <strong>{when}</strong>
                ({t.due_date.strftime('%d/%m/%Y')}).
              </p>
              <p style="font-size:13px;color:#AEAEB2;margin:0;">
                Project: {t.project.name}
              </p>"""
            _send(recipients,
                  f"Reminder: \"{t.name}\" is due {when}",
                  _base_html(content, project_url, 'View task →'))


# Start the daily reminder scheduler
scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(send_deadline_reminders, 'cron', hour=8, minute=0)
scheduler.start()


# ── Models ────────────────────────────────────────────────────────────────────

class Project(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, default='')
    color       = db.Column(db.String(20), default='blue')
    archived    = db.Column(db.Boolean, default=False)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    tasks       = db.relationship('Task', backref='project', lazy=True,
                                  cascade='all, delete-orphan')

    @property
    def open_count(self):
        return sum(1 for t in self.tasks if not t.done)

    @property
    def done_count(self):
        return sum(1 for t in self.tasks if t.done)

    @property
    def total_actual(self):
        """Sum of actual costs recorded."""
        return sum(t.cost_actual for t in self.tasks if t.cost_actual is not None)

    @property
    def total_estimate_remaining(self):
        """Estimated cost only on tasks that have NO actual yet — once actual is set, estimate drops out."""
        return sum(t.cost_estimate for t in self.tasks
                   if t.cost_estimate is not None and t.cost_actual is None and not t.done)

    @property
    def remaining_cost(self):
        """What still needs to be spent: actual-less estimates on incomplete tasks."""
        return self.total_estimate_remaining


class Task(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('project.id'), nullable=False)
    name       = db.Column(db.String(300), nullable=False)
    assignee   = db.Column(db.String(10), default='both')
    done       = db.Column(db.Boolean, default=False)
    due_date   = db.Column(db.Date, nullable=True)
    cost_estimate = db.Column(db.Float, nullable=True)
    cost_actual   = db.Column(db.Float, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    done_at    = db.Column(db.DateTime, nullable=True)

    links    = db.relationship('Link',     backref='task', lazy=True, cascade='all, delete-orphan')
    files    = db.relationship('TaskFile', backref='task', lazy=True, cascade='all, delete-orphan')
    comments = db.relationship('Comment',  backref='task', lazy=True, cascade='all, delete-orphan',
                               order_by='Comment.created_at')

    @property
    def is_overdue(self):
        return bool(self.due_date and not self.done and self.due_date < date.today())

    @property
    def due_label(self):
        if not self.due_date:
            return None
        def fmt(d):
            return d.strftime('%d/%m/%Y')
        if self.done:
            return 'Done' + (f" · {self.done_at.strftime('%d/%m/%Y')}" if self.done_at else '')
        if self.is_overdue:
            return f"Overdue · {fmt(self.due_date)}"
        return f"Due {fmt(self.due_date)}"


class Link(db.Model):
    id      = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey('task.id'), nullable=False)
    url     = db.Column(db.String(2000), nullable=False)
    title   = db.Column(db.String(300), default='')
    note    = db.Column(db.String(500), default='')

    @property
    def domain(self):
        from urllib.parse import urlparse
        try:
            return urlparse(self.url).netloc.replace('www.', '')
        except Exception:
            return self.url

    @property
    def favicon_letter(self):
        return self.domain[0].upper() if self.domain else '?'


class TaskFile(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    task_id     = db.Column(db.Integer, db.ForeignKey('task.id'), nullable=False)
    filename    = db.Column(db.String(300), nullable=False)
    stored_name = db.Column(db.String(300), nullable=False)
    size_bytes  = db.Column(db.Integer, default=0)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def size_label(self):
        kb = self.size_bytes / 1024
        return f"{kb/1024:.1f} MB" if kb >= 1024 else f"{kb:.0f} KB"

    @property
    def ext(self):
        return self.filename.rsplit('.', 1)[-1].upper() if '.' in self.filename else 'FILE'


class Comment(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    task_id    = db.Column(db.Integer, db.ForeignKey('task.id'), nullable=False)
    author     = db.Column(db.String(10), nullable=False)
    body       = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def time_label(self):
        delta = datetime.utcnow() - self.created_at
        if delta.seconds < 60:   return 'just now'
        if delta.seconds < 3600: return f"{delta.seconds // 60} min ago"
        if delta.days == 0:      return f"{delta.seconds // 3600} hr ago"
        return self.created_at.strftime('%d/%m/%Y')


class Settings(db.Model):
    """Single-row settings table — key/value pairs."""
    id    = db.Column(db.Integer, primary_key=True)
    key   = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.String(500), default='')

    @staticmethod
    def get(key, default=''):
        row = Settings.query.filter_by(key=key).first()
        return row.value if row else default

    @staticmethod
    def set(key, value):
        row = Settings.query.filter_by(key=key).first()
        if row:
            row.value = str(value)
        else:
            db.session.add(Settings(key=key, value=str(value)))
        db.session.commit()


class CalEvent(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(300), nullable=False)
    event_date = db.Column(db.Date, nullable=False)
    end_date   = db.Column(db.Date, nullable=True)
    start_time = db.Column(db.String(5), nullable=True)   # HH:MM
    end_time   = db.Column(db.String(5), nullable=True)   # HH:MM
    assignee   = db.Column(db.String(10), default='both')
    status     = db.Column(db.String(20), default='confirmed')  # confirmed / tentative
    notes      = db.Column(db.Text, default='')
    project_id = db.Column(db.Integer, db.ForeignKey('project.id'), nullable=True)
    task_id    = db.Column(db.Integer, db.ForeignKey('task.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def who_label(self):
        return {'jack':'Jack','minke':'Minke','both':'Both'}.get(self.assignee, 'Both')


# ── Workout models ────────────────────────────────────────────────────────────

class Exercise(db.Model):
    """Library of exercises (e.g. Bench Press, Squat)."""
    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(200), nullable=False)
    muscle_group  = db.Column(db.String(100), default='')   # chest, back, legs…
    equipment     = db.Column(db.String(100), default='')   # barbell, dumbbell, machine…
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    sets          = db.relationship('WorkoutSet', backref='exercise', lazy=True,
                                    cascade='all, delete-orphan')


class WorkoutSession(db.Model):
    """A single gym session."""
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.String(10), nullable=False)   # jack / minke
    session_date = db.Column(db.Date, nullable=False, default=date.today)
    title      = db.Column(db.String(300), default='')      # "Push day", "Leg day"
    notes      = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    sets       = db.relationship('WorkoutSet', backref='session', lazy=True,
                                  cascade='all, delete-orphan',
                                  order_by='WorkoutSet.order')


class WorkoutSet(db.Model):
    """One set inside a session: exercise + reps + weight."""
    id          = db.Column(db.Integer, primary_key=True)
    session_id  = db.Column(db.Integer, db.ForeignKey('workout_session.id'), nullable=False)
    exercise_id = db.Column(db.Integer, db.ForeignKey('exercise.id'), nullable=False)
    set_number  = db.Column(db.Integer, default=1)
    reps        = db.Column(db.Integer, default=0)
    weight_kg   = db.Column(db.Float, default=0)
    rpe         = db.Column(db.Float, nullable=True)        # Rate of perceived exertion 1-10
    order       = db.Column(db.Integer, default=0)


class BodyWeight(db.Model):
    """Daily body-weight log."""
    id        = db.Column(db.Integer, primary_key=True)
    user_id   = db.Column(db.String(10), nullable=False)
    log_date  = db.Column(db.Date, nullable=False)
    weight_kg = db.Column(db.Float, nullable=False)

    __table_args__ = (db.UniqueConstraint('user_id', 'log_date'),)


# ── Meal-planning models ─────────────────────────────────────────────────────

class FoodItem(db.Model):
    """Pantry inventory — basic foodstuffs on hand."""
    id        = db.Column(db.Integer, primary_key=True)
    name      = db.Column(db.String(200), nullable=False)
    category  = db.Column(db.String(100), default='other')  # dairy, meat, veg, grain…
    quantity  = db.Column(db.Float, default=0)
    unit      = db.Column(db.String(50), default='pcs')     # pcs, g, kg, ml, l
    low_stock = db.Column(db.Float, default=0)               # threshold for shopping list
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Recipe(db.Model):
    """Template recipe."""
    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(300), nullable=False)
    servings    = db.Column(db.Integer, default=2)
    prep_mins   = db.Column(db.Integer, default=0)
    cook_mins   = db.Column(db.Integer, default=0)
    instructions = db.Column(db.Text, default='')
    source_url  = db.Column(db.String(2000), default='')
    tags        = db.Column(db.String(500), default='')
    star_rating = db.Column(db.Integer, default=0)       # 0-5 stars
    kcal        = db.Column(db.Integer, nullable=True)    # per serving
    protein_g   = db.Column(db.Float,   nullable=True)
    carbs_g     = db.Column(db.Float,   nullable=True)
    fat_g       = db.Column(db.Float,   nullable=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    ingredients = db.relationship('RecipeIngredient', backref='recipe', lazy=True,
                                   cascade='all, delete-orphan')
    meal_plans  = db.relationship('MealPlan', backref='recipe', lazy=True)


class RecipeIngredient(db.Model):
    """One ingredient line in a recipe, optionally linked to a pantry FoodItem."""
    id           = db.Column(db.Integer, primary_key=True)
    recipe_id    = db.Column(db.Integer, db.ForeignKey('recipe.id'), nullable=False)
    food_item_id = db.Column(db.Integer, db.ForeignKey('food_item.id'), nullable=True)
    name         = db.Column(db.String(200), nullable=False)   # display name
    quantity     = db.Column(db.Float, default=0)
    unit         = db.Column(db.String(50), default='pcs')


class MealPlan(db.Model):
    """Calendar entry: a meal on a date."""
    id        = db.Column(db.Integer, primary_key=True)
    plan_date = db.Column(db.Date, nullable=False)
    meal_type = db.Column(db.String(20), default='dinner')   # breakfast, lunch, dinner, snack
    recipe_id = db.Column(db.Integer, db.ForeignKey('recipe.id'), nullable=True)
    custom_name = db.Column(db.String(300), default='')       # if not from recipe library
    assignee  = db.Column(db.String(10), default='both')
    cooked    = db.Column(db.Boolean, default=True)           # True = ingredients used from pantry
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def display_name(self):
        if self.recipe:
            return self.recipe.name
        return self.custom_name or 'Meal'


class ShoppingItem(db.Model):
    """Custom shopping list item (not generated from recipes)."""
    id       = db.Column(db.Integer, primary_key=True)
    name     = db.Column(db.String(200), nullable=False)
    quantity = db.Column(db.Float, default=1)
    unit     = db.Column(db.String(50), default='pcs')
    is_food  = db.Column(db.Boolean, default=True)           # only food items get added to pantry
    bought   = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class PushSubscription(db.Model):
    """Stores a Web Push subscription for a user (one per browser/device)."""
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.String(10), nullable=False)
    endpoint   = db.Column(db.Text, nullable=False, unique=True)
    p256dh     = db.Column(db.Text, nullable=False)
    auth       = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect('/')
    error = None
    if request.method == 'POST':
        who      = request.form.get('who', '').lower().strip()
        password = request.form.get('password', '')
        if who in USERS:
            user = User(USERS[who])
            if user.check_password(password):
                login_user(user, remember=True)
                return redirect(request.args.get('next') or '/')
        error = 'Wrong name or password — try again.'
    return render_template('login.html', error=error)


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect('/login')


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route('/')
@login_required
def dashboard():
    projects    = Project.query.filter_by(archived=False).order_by(Project.created_at).all()
    jack_tasks  = (Task.query
                   .filter(Task.assignee.in_(['jack','both']), Task.done == False)
                   .order_by(Task.due_date.nullslast(), Task.created_at).all())
    minke_tasks = (Task.query
                   .filter(Task.assignee.in_(['minke','both']), Task.done == False)
                   .order_by(Task.due_date.nullslast(), Task.created_at).all())
    jack_done   = (Task.query
                   .filter(Task.assignee.in_(['jack','both']), Task.done == True)
                   .order_by(Task.done_at.desc()).limit(3).all())
    minke_done  = (Task.query
                   .filter(Task.assignee.in_(['minke','both']), Task.done == True)
                   .order_by(Task.done_at.desc()).limit(3).all())
    overdue  = sum(1 for t in jack_tasks + minke_tasks if t.is_overdue)
    due_week = sum(1 for t in jack_tasks + minke_tasks
                   if t.due_date and t.due_date <= date.today() + timedelta(days=7))
    projects     = Project.query.filter_by(archived=False).order_by(Project.created_at).all()
    total_estimate_remaining = sum(p.total_estimate_remaining for p in projects)
    total_actual   = sum(p.total_actual for p in projects)
    return render_template('dashboard.html',
        projects=projects,
        jack_tasks=jack_tasks,   jack_done=jack_done,
        minke_tasks=minke_tasks, minke_done=minke_done,
        overdue=overdue, due_week=due_week,
        total_estimate=total_estimate_remaining,
        total_actual=total_actual,
        now=datetime.now(), hex=PROJECT_HEX)


# ── Projects ──────────────────────────────────────────────────────────────────

@app.route('/projects/new', methods=['POST'])
@login_required
def project_new():
    p = Project(
        name        = request.form['name'].strip(),
        description = request.form.get('description', '').strip(),
        color       = request.form.get('color', 'blue'),
    )
    db.session.add(p)
    db.session.commit()
    return redirect(f'/projects/{p.id}')


@app.route('/projects/<int:pid>')
@login_required
def project_view(pid):
    project    = Project.query.get_or_404(pid)
    open_tasks = sorted([t for t in project.tasks if not t.done],
                        key=lambda t: (t.due_date or date(9999,1,1), t.created_at))
    done_tasks = sorted([t for t in project.tasks if t.done],
                        key=lambda t: t.done_at or datetime.min, reverse=True)
    return render_template('project.html',
        project=project, open_tasks=open_tasks, done_tasks=done_tasks,
        hex=PROJECT_HEX)


@app.route('/projects/<int:pid>/finance_totals')
@login_required
def project_finance_totals(pid):
    p = Project.query.get_or_404(pid)
    return jsonify(
        actual=p.total_actual,
        estimate_remaining=p.total_estimate_remaining,
    )


@app.route('/projects/<int:pid>/archive', methods=['POST'])
@login_required
def project_archive(pid):
    p = Project.query.get_or_404(pid)
    p.archived = True
    db.session.commit()
    return redirect('/')


@app.route('/projects/<int:pid>/unarchive', methods=['POST'])
@login_required
def project_unarchive(pid):
    p = Project.query.get_or_404(pid)
    p.archived = False
    db.session.commit()
    return redirect(f'/projects/{pid}')


@app.route('/projects/<int:pid>/delete', methods=['POST'])
@login_required
def project_delete(pid):
    p = Project.query.get_or_404(pid)
    db.session.delete(p)
    db.session.commit()
    return redirect('/')


# ── Tasks ─────────────────────────────────────────────────────────────────────

@app.route('/projects/<int:pid>/tasks/new', methods=['POST'])
@login_required
def task_new(pid):
    Project.query.get_or_404(pid)
    due = request.form.get('due_date', '').strip()
    est = request.form.get('cost_estimate', '').strip()
    act = request.form.get('cost_actual',   '').strip()
    t = Task(
        project_id    = pid,
        name          = request.form['name'].strip(),
        assignee      = request.form.get('assignee', 'both'),
        due_date      = date.fromisoformat(due) if due else None,
        cost_estimate = float(est) if est else None,
        cost_actual   = float(act) if act else None,
    )
    db.session.add(t)
    db.session.commit()

    # Notify assignee if it's not the person creating it
    notify_task_assigned(t)

    return redirect(f'/projects/{pid}')


@app.route('/tasks/<int:tid>/toggle', methods=['POST'])
@login_required
def task_toggle(tid):
    t         = Task.query.get_or_404(tid)
    t.done    = not t.done
    t.done_at = datetime.utcnow() if t.done else None
    db.session.commit()
    return jsonify(done=t.done)


@app.route('/tasks/<int:tid>/delete', methods=['POST'])
@login_required
def task_delete(tid):
    t   = Task.query.get_or_404(tid)
    pid = t.project_id
    db.session.delete(t)
    db.session.commit()
    return redirect(f'/projects/{pid}')


@app.route('/tasks/<int:tid>/edit', methods=['POST'])
@login_required
def task_edit(tid):
    t   = Task.query.get_or_404(tid)
    est = request.form.get('cost_estimate', '').strip()
    act = request.form.get('cost_actual',   '').strip()
    t.cost_estimate = float(est) if est else None
    t.cost_actual   = float(act) if act else None
    due = request.form.get('due_date', '').strip()
    if due:
        t.due_date = date.fromisoformat(due)
    t.assignee = request.form.get('assignee', t.assignee)
    db.session.commit()
    # Return JSON so the finance page can save in-place without redirecting
    return jsonify(
        ok=True,
        cost_estimate=t.cost_estimate,
        cost_actual=t.cost_actual,
    )


# ── Links ─────────────────────────────────────────────────────────────────────

@app.route('/tasks/<int:tid>/links/add', methods=['POST'])
@login_required
def link_add(tid):
    task = Task.query.get_or_404(tid)
    url  = request.form.get('url', '').strip()
    if url:
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        db.session.add(Link(
            task_id = tid,
            url     = url,
            title   = request.form.get('title', '').strip(),
            note    = request.form.get('note',  '').strip(),
        ))
        db.session.commit()
    return redirect(f'/projects/{task.project_id}')


@app.route('/links/<int:lid>/delete', methods=['POST'])
@login_required
def link_delete(lid):
    lnk = Link.query.get_or_404(lid)
    pid = lnk.task.project_id
    db.session.delete(lnk)
    db.session.commit()
    return redirect(f'/projects/{pid}')


# ── Files ─────────────────────────────────────────────────────────────────────

@app.route('/tasks/<int:tid>/files/upload', methods=['POST'])
@login_required
def file_upload(tid):
    task = Task.query.get_or_404(tid)
    f    = request.files.get('file')
    if f and f.filename:
        ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
        if ext in ALLOWED_EXT:
            stored = f"{uuid.uuid4().hex}.{ext}"
            path   = os.path.join(UPLOAD_FOLDER, stored)
            f.save(path)
            db.session.add(TaskFile(
                task_id     = tid,
                filename    = f.filename,
                stored_name = stored,
                size_bytes  = os.path.getsize(path),
            ))
            db.session.commit()
    return redirect(f'/projects/{task.project_id}')


@app.route('/files/<int:fid>/download')
@login_required
def file_download(fid):
    tf = TaskFile.query.get_or_404(fid)
    return send_from_directory(UPLOAD_FOLDER, tf.stored_name,
                               as_attachment=True, download_name=tf.filename)


@app.route('/files/<int:fid>/delete', methods=['POST'])
@login_required
def file_delete(fid):
    tf  = TaskFile.query.get_or_404(fid)
    pid = tf.task.project_id
    try:
        os.remove(os.path.join(UPLOAD_FOLDER, tf.stored_name))
    except FileNotFoundError:
        pass
    db.session.delete(tf)
    db.session.commit()
    return redirect(f'/projects/{pid}')


# ── Comments ──────────────────────────────────────────────────────────────────

@app.route('/tasks/<int:tid>/comments/add', methods=['POST'])
@login_required
def comment_add(tid):
    task = Task.query.get_or_404(tid)
    body = request.form.get('body', '').strip()
    if body:
        c = Comment(
            task_id = tid,
            author  = current_user.id,
            body    = body,
        )
        db.session.add(c)
        db.session.commit()

        # Notify the other person
        notify_comment(c, task)

    return redirect(f'/projects/{task.project_id}')


@app.route('/settings/savings', methods=['POST'])
@login_required
def savings_update():
    Settings.set('savings_current',  request.form.get('savings_current',  '0'))
    Settings.set('savings_investments', request.form.get('savings_investments', '0'))
    return redirect('/finance')


# ── Finance ───────────────────────────────────────────────────────────────────

@app.route('/finance')
@login_required
def finance():
    projects = Project.query.filter_by(archived=False).order_by(Project.created_at).all()
    total_actual            = sum(p.total_actual             for p in projects)
    total_estimate_remaining = sum(p.total_estimate_remaining for p in projects)
    total_remaining         = total_estimate_remaining
    savings_current         = float(Settings.get('savings_current',     '0') or 0)
    savings_investments     = float(Settings.get('savings_investments', '0') or 0)
    total_assets            = savings_current + savings_investments
    return render_template('finance.html',
        projects=projects,
        total_actual=total_actual,
        total_estimate_remaining=total_estimate_remaining,
        total_remaining=total_remaining,
        savings_current=savings_current,
        savings_investments=savings_investments,
        total_assets=total_assets,
        now=datetime.now(),
        hex=PROJECT_HEX)


# ── Calendar ──────────────────────────────────────────────────────────────────

# Colour config per project colour & event type
def _ev_colors(color):
    """Return dot, bg, fg for a given project colour key."""
    MAP = {
        'blue':   ('#0A84FF', '#E8F2FF', '#004FAD'),
        'pink':   ('#FF375F', '#FFF0F3', '#9B1030'),
        'teal':   ('#30B98A', '#E6F8F2', '#0E6648'),
        'purple': ('#BF5AF2', '#F5EEFF', '#6B1FA8'),
        'amber':  ('#FF9F0A', '#FFF5E6', '#7A4400'),
    }
    return MAP.get(color, MAP['blue'])

DEADLINE_COLORS = ('#FF9F0A', '#FFF5E6', '#7A4400')
EVENT_COLORS    = ('#BF5AF2', '#F5EEFF', '#6B1FA8')
WHO_LABELS      = {'jack':'Jack','minke':'Minke','both':'Both'}


@app.route('/calendar')
@login_required
def calendar():
    import calendar as cal_mod
    today = date.today()
    month = int(request.args.get('month', today.month))
    year  = int(request.args.get('year',  today.year))

    # Month navigation
    prev_month = month - 1 if month > 1 else 12
    prev_year  = year  if month > 1 else year - 1
    next_month = month + 1 if month < 12 else 1
    next_year  = year  if month < 12 else year + 1

    month_name = cal_mod.month_name[month]
    matrix     = cal_mod.monthcalendar(year, month)

    # Collect all events for this month keyed by YYYY-MM-DD
    events_by_day = {}

    def add_ev(key, entry):
        events_by_day.setdefault(key, []).append(entry)

    # Task deadlines this month
    tasks_this_month = Task.query.filter(
        Task.done == False,
        Task.due_date != None,
        db.extract('month', Task.due_date) == month,
        db.extract('year',  Task.due_date) == year,
    ).all()

    for t in tasks_this_month:
        key = t.due_date.strftime('%Y-%m-%d')
        dot, bg, fg = DEADLINE_COLORS
        add_ev(key, {
            'name':     t.name,
            'dot':      dot, 'bg': bg, 'fg': fg,
            'label':    t.project.name,
            'who':      WHO_LABELS.get(t.assignee, 'Both'),
            'event_id': None,
        })

    # Standalone events this month
    cal_events = CalEvent.query.filter(
        db.extract('month', CalEvent.event_date) == month,
        db.extract('year',  CalEvent.event_date) == year,
    ).all()

    for ev in cal_events:
        key = ev.event_date.strftime('%Y-%m-%d')
        assignee = ev.assignee or 'both'
        if assignee == 'jack':
            dot, bg, fg = '#0A84FF', '#E8F2FF', '#004FAD'
        elif assignee == 'minke':
            dot, bg, fg = '#FF375F', '#FFF0F3', '#9B1030'
        else:
            dot, bg, fg = EVENT_COLORS
        # For multi-day events, also add to intermediate days
        days_to_mark = [key]
        if ev.end_date and ev.end_date > ev.event_date:
            d = ev.event_date + timedelta(days=1)
            while d <= ev.end_date:
                days_to_mark.append(d.strftime('%Y-%m-%d'))
                d += timedelta(days=1)
        entry = {
            'name':       ev.name,
            'dot':        dot, 'bg': bg, 'fg': fg,
            'label':      'Personal event',
            'who':        WHO_LABELS.get(ev.assignee, 'Both'),
            'event_id':   ev.id,
            'assignee':   assignee,
            'status':     ev.status or 'confirmed',
            'notes':      ev.notes or '',
            'event_date': ev.event_date.isoformat(),
            'end_date':   ev.end_date.isoformat() if ev.end_date else '',
            'start_time': ev.start_time or '',
            'end_time':   ev.end_time or '',
            'project_id': ev.project_id,
            'task_id':    ev.task_id,
        }
        for day_key in days_to_mark:
            add_ev(day_key, entry)

    # Upcoming — task deadlines + events in next 30 days, sorted by date
    cutoff = today + timedelta(days=30)
    upcoming = []

    for t in Task.query.filter(
        Task.done == False,
        Task.due_date != None,
        Task.due_date >= today,
        Task.due_date <= cutoff,
    ).order_by(Task.due_date).all():
        upcoming.append({
            'date':    t.due_date,
            'name':    t.name,
            'type':    'task',
            'project': t.project.name,
            'color':   t.project.color,
            'who':     WHO_LABELS.get(t.assignee, 'Both'),
        })

    for ev in CalEvent.query.filter(
        CalEvent.event_date >= today,
        CalEvent.event_date <= cutoff,
    ).order_by(CalEvent.event_date).all():
        upcoming.append({
            'date':    ev.event_date,
            'name':    ev.name,
            'type':    'event',
            'project': None,
            'color':   'purple',
            'who':     WHO_LABELS.get(ev.assignee, 'Both'),
        })

    upcoming.sort(key=lambda x: x['date'])

    projects  = Project.query.filter_by(archived=False).order_by(Project.created_at).all()
    all_tasks = Task.query.filter_by(done=False).order_by(Task.name).all()

    return render_template('calendar.html',
        today=today, month=month, year=year,
        month_name=month_name, matrix=matrix,
        events_by_day=events_by_day, upcoming=upcoming,
        prev_month=prev_month, prev_year=prev_year,
        next_month=next_month, next_year=next_year,
        projects=projects, all_tasks=all_tasks, hex=PROJECT_HEX)


@app.route('/events/new', methods=['POST'])
@login_required
def event_new():
    name       = request.form.get('name','').strip()
    event_date = request.form.get('event_date','').strip()
    if name and event_date:
        end_date_str = request.form.get('end_date','').strip()
        pid = request.form.get('project_id', type=int)
        tid = request.form.get('task_id', type=int)
        ev = CalEvent(
            name       = name,
            event_date = date.fromisoformat(event_date),
            end_date   = date.fromisoformat(end_date_str) if end_date_str else None,
            start_time = request.form.get('start_time','').strip() or None,
            end_time   = request.form.get('end_time','').strip() or None,
            assignee   = request.form.get('assignee','both'),
            status     = request.form.get('status','confirmed'),
            notes      = request.form.get('notes','').strip(),
            project_id = pid or None,
            task_id    = tid or None,
        )
        db.session.add(ev)
        db.session.commit()
        d = ev.event_date
        return redirect(f'/calendar?month={d.month}&year={d.year}')
    return redirect('/calendar')


@app.route('/events/<int:eid>/edit', methods=['POST'])
@login_required
def event_edit(eid):
    ev = CalEvent.query.get_or_404(eid)
    ev.name       = request.form.get('name', ev.name).strip()
    ev.event_date = date.fromisoformat(request.form.get('event_date', ev.event_date.isoformat()))
    end_date_str  = request.form.get('end_date','').strip()
    ev.end_date   = date.fromisoformat(end_date_str) if end_date_str else None
    ev.start_time = request.form.get('start_time','').strip() or None
    ev.end_time   = request.form.get('end_time','').strip() or None
    ev.assignee   = request.form.get('assignee', ev.assignee)
    ev.status     = request.form.get('status', 'confirmed')
    ev.notes      = request.form.get('notes', '').strip()
    pid = request.form.get('project_id', type=int)
    tid = request.form.get('task_id', type=int)
    ev.project_id = pid or None
    ev.task_id    = tid or None
    db.session.commit()
    d = ev.event_date
    return redirect(f'/calendar?month={d.month}&year={d.year}')


@app.route('/events/<int:eid>/delete', methods=['POST'])
@login_required
def event_delete(eid):
    ev = CalEvent.query.get_or_404(eid)
    m, y = ev.event_date.month, ev.event_date.year
    db.session.delete(ev)
    db.session.commit()
    return redirect(f'/calendar?month={m}&year={y}')


# ── Workouts ─────────────────────────────────────────────────────────────────

@app.route('/workouts')
@login_required
def workouts():
    import calendar as cal_mod
    today = date.today()
    month = int(request.args.get('month', today.month))
    year  = int(request.args.get('year',  today.year))

    prev_month = month - 1 if month > 1 else 12
    prev_year  = year  if month > 1 else year - 1
    next_month = month + 1 if month < 12 else 1
    next_year  = year  if month < 12 else year + 1
    month_name = cal_mod.month_name[month]
    matrix     = cal_mod.monthcalendar(year, month)

    # Sessions this month keyed by date string
    sessions_this_month = WorkoutSession.query.filter(
        db.extract('month', WorkoutSession.session_date) == month,
        db.extract('year',  WorkoutSession.session_date) == year,
    ).order_by(WorkoutSession.session_date.desc()).all()

    sessions_by_day = {}
    for s in sessions_this_month:
        key = s.session_date.strftime('%Y-%m-%d')
        sessions_by_day.setdefault(key, []).append(s)

    # Recent sessions for the sidebar
    recent_sessions = WorkoutSession.query.filter_by(
        user_id=current_user.id
    ).order_by(WorkoutSession.session_date.desc()).limit(10).all()

    # Exercise library
    exercises = Exercise.query.order_by(Exercise.muscle_group, Exercise.name).all()

    # Body weight log (last 60 entries)
    bw_log = BodyWeight.query.filter_by(
        user_id=current_user.id
    ).order_by(BodyWeight.log_date.desc()).limit(60).all()

    # Progress data: for each exercise, last 20 sessions' best set
    exercise_progress = {}
    for ex in exercises:
        rows = (db.session.query(
            WorkoutSession.session_date,
            db.func.max(WorkoutSet.weight_kg).label('max_weight'),
            db.func.max(WorkoutSet.reps).label('max_reps'),
        ).join(WorkoutSet, WorkoutSet.session_id == WorkoutSession.id)
         .filter(WorkoutSet.exercise_id == ex.id,
                 WorkoutSession.user_id == current_user.id)
         .group_by(WorkoutSession.session_date)
         .order_by(WorkoutSession.session_date.desc())
         .limit(20).all())
        if rows:
            exercise_progress[ex.id] = [
                {'date': r.session_date.strftime('%d/%m'), 'weight': r.max_weight, 'reps': r.max_reps}
                for r in reversed(rows)
            ]

    return render_template('workouts.html',
        today=today, month=month, year=year,
        month_name=month_name, matrix=matrix,
        sessions_by_day=sessions_by_day,
        recent_sessions=recent_sessions,
        exercises=exercises,
        bw_log=bw_log,
        exercise_progress=exercise_progress,
        prev_month=prev_month, prev_year=prev_year,
        next_month=next_month, next_year=next_year,
        hex=PROJECT_HEX)


@app.route('/exercises/new', methods=['POST'])
@login_required
def exercise_new():
    name = request.form.get('name', '').strip()
    if name:
        ex = Exercise(
            name         = name,
            muscle_group = request.form.get('muscle_group', '').strip(),
            equipment    = request.form.get('equipment', '').strip(),
        )
        db.session.add(ex)
        db.session.commit()
    return redirect('/workouts')


@app.route('/exercises/<int:eid>/delete', methods=['POST'])
@login_required
def exercise_delete(eid):
    ex = Exercise.query.get_or_404(eid)
    db.session.delete(ex)
    db.session.commit()
    return redirect('/workouts')


@app.route('/workout-sessions/new', methods=['POST'])
@login_required
def workout_session_new():
    sd = request.form.get('session_date', '').strip()
    s = WorkoutSession(
        user_id      = current_user.id,
        session_date = date.fromisoformat(sd) if sd else date.today(),
        title        = request.form.get('title', '').strip(),
        notes        = request.form.get('notes', '').strip(),
    )
    db.session.add(s)
    db.session.commit()
    return redirect(f'/workout-sessions/{s.id}')


@app.route('/workout-sessions/<int:sid>')
@login_required
def workout_session_view(sid):
    session = WorkoutSession.query.get_or_404(sid)
    exercises = Exercise.query.order_by(Exercise.muscle_group, Exercise.name).all()
    sets_by_exercise = {}
    for ws in session.sets:
        sets_by_exercise.setdefault(ws.exercise_id, []).append(ws)
    return render_template('workout_session.html',
        session=session, exercises=exercises,
        sets_by_exercise=sets_by_exercise, hex=PROJECT_HEX)


@app.route('/workout-sessions/<int:sid>/add-set', methods=['POST'])
@login_required
def workout_set_add(sid):
    WorkoutSession.query.get_or_404(sid)
    ex_id = request.form.get('exercise_id', type=int)
    if ex_id:
        existing = WorkoutSet.query.filter_by(session_id=sid, exercise_id=ex_id).count()
        max_order = db.session.query(db.func.max(WorkoutSet.order)).filter_by(session_id=sid).scalar() or 0
        ws = WorkoutSet(
            session_id  = sid,
            exercise_id = ex_id,
            set_number  = existing + 1,
            reps        = request.form.get('reps', 0, type=int),
            weight_kg   = request.form.get('weight_kg', 0, type=float),
            rpe         = request.form.get('rpe', None, type=float),
            order       = max_order + 1,
        )
        db.session.add(ws)
        db.session.commit()
    return redirect(f'/workout-sessions/{sid}')


@app.route('/workout-sets/<int:wsid>/delete', methods=['POST'])
@login_required
def workout_set_delete(wsid):
    ws = WorkoutSet.query.get_or_404(wsid)
    sid = ws.session_id
    db.session.delete(ws)
    db.session.commit()
    return redirect(f'/workout-sessions/{sid}')


@app.route('/workout-sessions/<int:sid>/delete', methods=['POST'])
@login_required
def workout_session_delete(sid):
    s = WorkoutSession.query.get_or_404(sid)
    db.session.delete(s)
    db.session.commit()
    return redirect('/workouts')


@app.route('/bodyweight/log', methods=['POST'])
@login_required
def bodyweight_log():
    w = request.form.get('weight_kg', '').strip()
    d = request.form.get('log_date', '').strip()
    if w:
        log_date = date.fromisoformat(d) if d else date.today()
        existing = BodyWeight.query.filter_by(user_id=current_user.id, log_date=log_date).first()
        if existing:
            existing.weight_kg = float(w)
        else:
            db.session.add(BodyWeight(
                user_id=current_user.id, log_date=log_date, weight_kg=float(w)
            ))
        db.session.commit()
    return redirect('/workouts')


@app.route('/api/exercise-progress/<int:eid>')
@login_required
def api_exercise_progress(eid):
    rows = (db.session.query(
        WorkoutSession.session_date,
        db.func.max(WorkoutSet.weight_kg).label('max_weight'),
        db.func.max(WorkoutSet.reps).label('max_reps'),
    ).join(WorkoutSet, WorkoutSet.session_id == WorkoutSession.id)
     .filter(WorkoutSet.exercise_id == eid,
             WorkoutSession.user_id == current_user.id)
     .group_by(WorkoutSession.session_date)
     .order_by(WorkoutSession.session_date)
     .limit(30).all())
    return jsonify([
        {'date': r.session_date.strftime('%d/%m'), 'weight': r.max_weight, 'reps': r.max_reps}
        for r in rows
    ])


# ── Meals ────────────────────────────────────────────────────────────────────

@app.route('/meals')
@login_required
def meals():
    today = date.today()

    # Current week for the meal calendar
    week_offset = int(request.args.get('week_offset', 0))
    start_of_week = today - timedelta(days=today.weekday()) + timedelta(weeks=week_offset)
    week_dates = [start_of_week + timedelta(days=i) for i in range(7)]

    # Meal plans this week
    meal_plans = MealPlan.query.filter(
        MealPlan.plan_date >= week_dates[0],
        MealPlan.plan_date <= week_dates[6],
    ).order_by(MealPlan.plan_date, MealPlan.meal_type).all()
    meals_by_day = {}
    for mp in meal_plans:
        key = mp.plan_date.strftime('%Y-%m-%d')
        meals_by_day.setdefault(key, []).append(mp)

    # Pantry — sorted by our preferred category order, then by name
    pantry = FoodItem.query.all()
    pantry.sort(key=lambda f: (_category_sort_key(f.category), f.name.lower()))
    low_stock_items = [f for f in pantry if f.low_stock > 0 and f.quantity <= f.low_stock]

    # Split pantry into items we actually have vs. known-but-not-on-hand.
    # Qty > 0 means in stock; qty == 0 means it's a known item (e.g. auto-
    # created from a recipe ingredient) but we don't have it right now.
    pantry_in_stock     = [f for f in pantry if f.quantity > 0]
    pantry_out_of_stock = [f for f in pantry if f.quantity <= 0]

    # Recipes
    recipes = Recipe.query.order_by(Recipe.name).all()

    # Shopping list: items needed for this week's cooked meal plans minus pantry
    shopping_needed = {}
    for mp in meal_plans:
        if mp.recipe and mp.cooked:
            for ing in mp.recipe.ingredients:
                # Prefer linked food id as the aggregation key — more reliable
                # than free-text names across recipes.
                key = f'food:{ing.food_item_id}' if ing.food_item_id else f'name:{ing.name.lower()}'
                if key not in shopping_needed:
                    shopping_needed[key] = {
                        'name': ing.name, 'quantity': 0, 'unit': ing.unit,
                        'food_item_id': ing.food_item_id,
                    }
                shopping_needed[key]['quantity'] += ing.quantity

    # Subtract pantry quantities
    pantry_by_id = {f.id: f for f in pantry}
    pantry_by_name = {f.name.lower(): f for f in pantry}
    shopping_list = []
    for key, item in shopping_needed.items():
        on_hand = None
        food = None
        if item.get('food_item_id'):
            food = pantry_by_id.get(item['food_item_id'])
            on_hand = food
        if on_hand is None:
            food = pantry_by_name.get(item['name'].lower())
            on_hand = food
        cat = (food.category if food else 'other') or 'other'
        if on_hand:
            needed = item['quantity'] - on_hand.quantity
            if needed > 0:
                shopping_list.append({**item, 'quantity': needed, 'auto': True, 'category': cat})
        else:
            shopping_list.append({**item, 'auto': True, 'category': cat})

    # Add low-stock pantry items not already on the list
    already_food_ids = {it.get('food_item_id') for it in shopping_list if it.get('food_item_id')}
    already_names = {it['name'].lower() for it in shopping_list}
    for f in low_stock_items:
        if f.id in already_food_ids or f.name.lower() in already_names:
            continue
        shopping_list.append({
            'name': f.name,
            'quantity': f.low_stock - f.quantity,
            'unit': f.unit,
            'auto': True,
            'food_item_id': f.id,
            'category': f.category or 'other',
        })

    # Sort shopping list by category display order
    shopping_list.sort(key=lambda it: _category_sort_key(it.get('category', 'other')))

    # Custom shopping items
    custom_items = ShoppingItem.query.filter_by(bought=False).order_by(ShoppingItem.created_at).all()

    return render_template('meals.html',
        today=today, week_dates=week_dates,
        week_offset=week_offset,
        meals_by_day=meals_by_day,
        pantry=pantry,
        pantry_in_stock=pantry_in_stock,
        pantry_out_of_stock=pantry_out_of_stock,
        recipes=recipes, shopping_list=shopping_list,
        custom_items=custom_items,
        low_stock_items=low_stock_items,
        food_categories=FOOD_CATEGORIES,
        hex=PROJECT_HEX)


@app.route('/pantry/add', methods=['POST'])
@login_required
def pantry_add():
    name = request.form.get('name', '').strip()
    if name:
        db.session.add(FoodItem(
            name      = name,
            category  = request.form.get('category', 'other').strip(),
            quantity  = float(request.form.get('quantity', 0) or 0),
            unit      = request.form.get('unit', 'pcs').strip(),
            low_stock = float(request.form.get('low_stock', 0) or 0),
        ))
        db.session.commit()
    return redirect('/meals#pantry')


@app.route('/pantry/<int:fid>/update', methods=['POST'])
@login_required
def pantry_update(fid):
    f = FoodItem.query.get_or_404(fid)
    # Supports partial updates — any field provided gets applied.
    data = request.get_json(silent=True) or request.form
    if data.get('quantity') is not None:
        f.quantity = float(data.get('quantity') or 0)
    if data.get('low_stock') is not None:
        f.low_stock = float(data.get('low_stock') or 0)
    if data.get('category') is not None:
        new_cat = (data.get('category') or 'other').strip() or 'other'
        # Validate against known categories; unknown values fall back to 'other'.
        if new_cat not in FOOD_CATEGORY_BY_KEY:
            new_cat = 'other'
        f.category = new_cat
    if data.get('unit') is not None:
        f.unit = (data.get('unit') or 'pcs').strip() or 'pcs'
    db.session.commit()
    return jsonify(ok=True, quantity=f.quantity, category=f.category,
                   low_stock=f.low_stock, unit=f.unit)


@app.route('/pantry/<int:fid>/delete', methods=['POST'])
@login_required
def pantry_delete(fid):
    f = FoodItem.query.get_or_404(fid)
    db.session.delete(f)
    db.session.commit()
    return redirect('/meals#pantry')


@app.route('/recipes/new', methods=['POST'])
@login_required
def recipe_new():
    name = request.form.get('name', '').strip()
    if name:
        r = Recipe(
            name         = name,
            servings     = int(request.form.get('servings', 2) or 2),
            prep_mins    = int(request.form.get('prep_mins', 0) or 0),
            cook_mins    = int(request.form.get('cook_mins', 0) or 0),
            instructions = request.form.get('instructions', '').strip(),
            source_url   = request.form.get('source_url', '').strip(),
            tags         = request.form.get('tags', '').strip(),
            star_rating  = int(request.form.get('star_rating', 0) or 0),
            kcal         = int(request.form.get('kcal') or 0) or None,
            protein_g    = float(request.form.get('protein_g') or 0) or None,
            carbs_g      = float(request.form.get('carbs_g') or 0) or None,
            fat_g        = float(request.form.get('fat_g') or 0) or None,
        )
        db.session.add(r)
        db.session.flush()
        _save_recipe_ingredients(r.id, request.form)
        db.session.commit()
    return redirect('/meals#recipes')


@app.route('/recipes/<int:rid>/edit', methods=['GET', 'POST'])
@login_required
def recipe_edit(rid):
    r = Recipe.query.get_or_404(rid)
    if request.method == 'POST':
        r.name         = request.form.get('name', r.name).strip()
        r.servings     = int(request.form.get('servings', r.servings) or 2)
        r.prep_mins    = int(request.form.get('prep_mins', r.prep_mins) or 0)
        r.cook_mins    = int(request.form.get('cook_mins', r.cook_mins) or 0)
        r.instructions = request.form.get('instructions', '').strip()
        r.source_url   = request.form.get('source_url', '').strip()
        r.tags         = request.form.get('tags', '').strip()
        r.star_rating  = int(request.form.get('star_rating', 0) or 0)
        r.kcal         = int(request.form.get('kcal') or 0) or None
        r.protein_g    = float(request.form.get('protein_g') or 0) or None
        r.carbs_g      = float(request.form.get('carbs_g') or 0) or None
        r.fat_g        = float(request.form.get('fat_g') or 0) or None
        # Clear and re-add ingredients
        RecipeIngredient.query.filter_by(recipe_id=r.id).delete()
        _save_recipe_ingredients(r.id, request.form)
        db.session.commit()
        return redirect('/meals#recipes')
    pantry = FoodItem.query.order_by(FoodItem.name).all()
    return render_template('recipe_edit.html', recipe=r, pantry=pantry,
                           food_categories=FOOD_CATEGORIES, hex=PROJECT_HEX)


def _save_recipe_ingredients(recipe_id, form):
    """Parse ingredient fields from form and save them.

    Prefers the explicit food_item_id[] posted by the autocomplete widget.
    Falls back to case-insensitive name matching, and if still unmatched,
    auto-creates a zero-quantity FoodItem so future matches are seamless.
    """
    names     = form.getlist('ing_name[]')
    qtys      = form.getlist('ing_qty[]')
    units     = form.getlist('ing_unit[]')
    food_ids  = form.getlist('ing_food_id[]')  # may be empty strings for unmatched
    for i, iname in enumerate(names):
        iname = iname.strip()
        if not iname:
            continue
        iqty  = float(qtys[i]) if i < len(qtys) and qtys[i] else 0
        iunit = units[i].strip() if i < len(units) and units[i] else 'pcs'

        # 1) Explicit food id from autocomplete?
        food = None
        raw_id = food_ids[i].strip() if i < len(food_ids) and food_ids[i] else ''
        if raw_id:
            try:
                food = FoodItem.query.get(int(raw_id))
            except (ValueError, TypeError):
                food = None

        # 2) Case-insensitive name fallback
        if food is None:
            food = FoodItem.query.filter(
                db.func.lower(FoodItem.name) == iname.lower()
            ).first()

        # 3) Still nothing? Create a canonical pantry entry at qty 0.
        if food is None:
            food = FoodItem(name=iname, category='other',
                            quantity=0, unit=iunit, low_stock=0)
            db.session.add(food)
            db.session.flush()  # assigns food.id

        db.session.add(RecipeIngredient(
            recipe_id    = recipe_id,
            food_item_id = food.id,
            name         = food.name,   # use canonical name for consistency
            quantity     = iqty,
            unit         = iunit,
        ))


# ── Ingredient autocomplete API ───────────────────────────────────────────────

@app.route('/api/foods')
@login_required
def api_foods():
    """List pantry foods for ingredient autocomplete.

    Optional ?q= substring filter (case-insensitive). Returns all foods if
    q is empty, so the frontend can show the full list in a dropdown.
    """
    q = (request.args.get('q') or '').strip().lower()
    query = FoodItem.query
    if q:
        query = query.filter(db.func.lower(FoodItem.name).like(f'%{q}%'))
    foods = query.order_by(FoodItem.name).limit(50).all()
    return jsonify([
        {'id': f.id, 'name': f.name, 'unit': f.unit,
         'category': f.category, 'quantity': f.quantity}
        for f in foods
    ])


@app.route('/api/foods/create', methods=['POST'])
@login_required
def api_food_create():
    """Create a pantry FoodItem inline and return it.

    Used by the ingredient autocomplete when a typed name doesn't yet exist.
    """
    data = request.get_json(silent=True) or request.form
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify(error='Name required'), 400

    # Dedupe: if something with this name already exists, return it.
    existing = FoodItem.query.filter(
        db.func.lower(FoodItem.name) == name.lower()
    ).first()
    if existing:
        return jsonify(id=existing.id, name=existing.name, unit=existing.unit,
                       category=existing.category, quantity=existing.quantity,
                       created=False)

    unit     = (data.get('unit') or 'pcs').strip() or 'pcs'
    category = (data.get('category') or 'other').strip() or 'other'
    if category not in FOOD_CATEGORY_BY_KEY:
        category = 'other'
    f = FoodItem(name=name, unit=unit, category=category,
                 quantity=0, low_stock=0)
    db.session.add(f)
    db.session.commit()
    return jsonify(id=f.id, name=f.name, unit=f.unit, category=f.category,
                   quantity=f.quantity, created=True)


@app.route('/recipes/<int:rid>/delete', methods=['POST'])
@login_required
def recipe_delete(rid):
    r = Recipe.query.get_or_404(rid)
    db.session.delete(r)
    db.session.commit()
    return redirect('/meals#recipes')


@app.route('/recipes/<int:rid>/shopping-list')
@login_required
def recipe_shopping_list(rid):
    r = Recipe.query.get_or_404(rid)
    items = []
    for ing in r.ingredients:
        on_hand = 0
        if ing.food_item_id:
            fi = FoodItem.query.get(ing.food_item_id)
            if fi:
                on_hand = fi.quantity
        needed = max(0, ing.quantity - on_hand)
        items.append({'name': ing.name, 'need': ing.quantity, 'have': on_hand,
                      'buy': needed, 'unit': ing.unit})
    return jsonify(recipe=r.name, items=items)


@app.route('/api/recipes/<int:rid>')
@login_required
def api_recipe_detail(rid):
    """Return full recipe detail as JSON for the popup viewer."""
    r = Recipe.query.get_or_404(rid)
    return jsonify(
        id=r.id, name=r.name, servings=r.servings,
        prep_mins=r.prep_mins, cook_mins=r.cook_mins,
        instructions=r.instructions or '',
        source_url=r.source_url or '',
        tags=[t.strip() for t in (r.tags or '').split(',') if t.strip()],
        ingredients=[
            {'name': ing.name, 'quantity': ing.quantity, 'unit': ing.unit}
            for ing in r.ingredients
        ],
    )


@app.route('/meal-plans/add', methods=['POST'])
@login_required
def meal_plan_add():
    d = request.form.get('plan_date', '').strip()
    wo = request.form.get('week_offset', '0')
    if d:
        rid = request.form.get('recipe_id', type=int)
        db.session.add(MealPlan(
            plan_date   = date.fromisoformat(d),
            meal_type   = request.form.get('meal_type', 'dinner'),
            recipe_id   = rid if rid else None,
            custom_name = request.form.get('custom_name', '').strip(),
            assignee    = request.form.get('assignee', 'both'),
            cooked      = True,
        ))
        db.session.commit()
    return redirect(f'/meals?week_offset={wo}')


@app.route('/meal-plans/<int:mid>/toggle-cooked', methods=['POST'])
@login_required
def meal_plan_toggle_cooked(mid):
    mp = MealPlan.query.get_or_404(mid)
    mp.cooked = not mp.cooked
    db.session.commit()
    return jsonify(ok=True, cooked=mp.cooked)


@app.route('/meal-plans/<int:mid>/delete', methods=['POST'])
@login_required
def meal_plan_delete(mid):
    mp = MealPlan.query.get_or_404(mid)
    db.session.delete(mp)
    db.session.commit()
    wo = request.args.get('week_offset', '0')
    return redirect(f'/meals?week_offset={wo}')


@app.route('/shopping/add-custom', methods=['POST'])
@login_required
def shopping_add_custom():
    name = request.form.get('name', '').strip()
    if name:
        db.session.add(ShoppingItem(
            name     = name,
            quantity = float(request.form.get('quantity', 1) or 1),
            unit     = request.form.get('unit', 'pcs').strip(),
            is_food  = request.form.get('is_food') == 'on',
        ))
        db.session.commit()
        _notify_shopping_updated(current_user.id, 'add', name)
    return redirect('/meals#shopping')


@app.route('/shopping/<int:sid>/delete', methods=['POST'])
@login_required
def shopping_item_delete(sid):
    si = ShoppingItem.query.get_or_404(sid)
    name = si.name
    db.session.delete(si)
    db.session.commit()
    _notify_shopping_updated(current_user.id, 'remove', name)
    return redirect('/meals#shopping')


@app.route('/shopping/complete', methods=['POST'])
@login_required
def shopping_complete():
    """Mark checked items as bought — food items get added to pantry."""
    data = request.get_json() or {}
    items = data.get('items', [])
    for item in items:
        name    = item.get('name', '').strip()
        qty     = float(item.get('quantity', 0) or 0)
        is_food = item.get('is_food', True)
        if not name or qty <= 0:
            continue
        # Only add food items to the pantry
        if is_food:
            food = FoodItem.query.filter(db.func.lower(FoodItem.name) == name.lower()).first()
            if food:
                food.quantity += qty
            else:
                db.session.add(FoodItem(name=name, quantity=qty, unit=item.get('unit', 'pcs')))
    # Only delete custom items that were checked off
    checked_names = [i.get('name', '').strip().lower() for i in items if i.get('checked')]
    for si in ShoppingItem.query.filter_by(bought=False).all():
        if si.name.lower() in checked_names:
            db.session.delete(si)
    db.session.commit()
    return jsonify(ok=True)


# ── Push notification helpers ─────────────────────────────────────────────────

def _send_push(user_id, title, body, url='/meals'):
    if not _push_available or not VAPID_PRIVATE_KEY or not VAPID_PUBLIC_KEY:
        return
    subs = PushSubscription.query.filter_by(user_id=user_id).all()
    for sub in subs:
        try:
            webpush(
                subscription_info={'endpoint': sub.endpoint, 'keys': {'p256dh': sub.p256dh, 'auth': sub.auth}},
                data=json.dumps({'title': title, 'body': body, 'url': url, 'icon': '/static/icon-192.png'}),
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims=VAPID_CLAIMS,
            )
        except Exception as e:
            app.logger.error(f'[push] {e}')
            if hasattr(e, 'response') and e.response is not None and e.response.status_code in (404, 410):
                db.session.delete(sub)
                db.session.commit()


def _notify_shopping_updated(actor_id, action, item_name):
    other = 'minke' if actor_id == 'jack' else 'jack'
    actor = _name(actor_id)
    body  = f'{actor} added "{item_name}" to the shopping list' if action == 'add' \
            else f'{actor} removed "{item_name}" from the shopping list'
    _send_push(other, '🛒 Shopping list updated', body, url='/meals')


@app.route('/api/push/vapid-public-key')
@login_required
def push_vapid_key():
    return jsonify(key=VAPID_PUBLIC_KEY)


@app.route('/api/push/subscribe', methods=['POST'])
@login_required
def push_subscribe():
    data   = request.get_json(silent=True) or {}
    endpoint = data.get('endpoint', '').strip()
    p256dh   = data.get('keys', {}).get('p256dh', '').strip()
    auth     = data.get('keys', {}).get('auth', '').strip()
    if not endpoint or not p256dh or not auth:
        return jsonify(error='Invalid subscription'), 400
    sub = PushSubscription.query.filter_by(endpoint=endpoint).first()
    if sub:
        sub.p256dh = p256dh; sub.auth = auth; sub.user_id = current_user.id
    else:
        db.session.add(PushSubscription(user_id=current_user.id, endpoint=endpoint, p256dh=p256dh, auth=auth))
    db.session.commit()
    return jsonify(ok=True)


@app.route('/api/push/unsubscribe', methods=['POST'])
@login_required
def push_unsubscribe():
    data = request.get_json(silent=True) or {}
    endpoint = data.get('endpoint', '').strip()
    if endpoint:
        PushSubscription.query.filter_by(endpoint=endpoint).delete()
        db.session.commit()
    return jsonify(ok=True)


@app.route('/sw.js')
def service_worker():
    return send_from_directory(app.static_folder, 'sw.js', mimetype='application/javascript')


# ── Init ─────────────────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()


if __name__ == '__main__':
    app.run(debug=True)