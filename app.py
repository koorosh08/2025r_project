import requests
import os
import datetime as dt
from zoneinfo import ZoneInfo
import datetime as dt

import requests
from flask import Flask, render_template, redirect, url_for, request, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, login_required, logout_user, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash

# ---------------------------
# App + DB setup
# ---------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///app.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"

TZ = ZoneInfo("America/Toronto")
UTC = dt.timezone.utc

def utc_now_naive() -> dt.datetime:
    # Store & compare in naive UTC to avoid sqlite timezone issues
    return dt.datetime.now(UTC).replace(tzinfo=None)

def toronto_now() -> dt.datetime:
    return dt.datetime.now(TZ)
SHOP_URL = "https://fortnite-api.com/v2/shop?language=en"

# ---------------------------
# Models
# ---------------------------
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(32), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)

class WishlistItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    offer_id = db.Column(db.String(64), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    price = db.Column(db.Integer, nullable=True)
    rarity = db.Column(db.String(50), nullable=True)
    image = db.Column(db.String(500), nullable=True)
    added_at = db.Column(db.DateTime, default=lambda: dt.datetime.now(TZ))

    __table_args__ = (
        db.UniqueConstraint("user_id", "offer_id", name="uq_user_offer"),
    )

class ShopCache(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    fetched_at = db.Column(db.DateTime, nullable=False)
    raw_json = db.Column(db.Text, nullable=False)

# ---------------------------
# Auth helpers
# ---------------------------
@login_manager.user_loader
def load_user(user_id: str):
    return db.session.get(User, int(user_id))

# ---------------------------
# Item shop caching with 8pm boundary
# ---------------------------
def last_8pm_boundary_toronto(now_tor: dt.datetime) -> dt.datetime:
    """Most recent 8:00 PM Toronto time."""
    today_8pm = now_tor.replace(hour=20, minute=0, second=0, microsecond=0)
    if now_tor >= today_8pm:
        return today_8pm
    return today_8pm - dt.timedelta(days=1)

    """
    Returns the most recent 8:00 PM (20:00) local time boundary.
    If it's before 8pm today -> returns 8pm yesterday.
    """
    today_8pm = now.replace(hour=20, minute=0, second=0, microsecond=0)
    if now >= today_8pm:
        return today_8pm
    return today_8pm - dt.timedelta(days=1)

def get_shop_data() -> dict:
    now_tor = toronto_now()
    boundary_tor = last_8pm_boundary_toronto(now_tor)

    # Convert boundary (Toronto) -> UTC naive for DB comparison
    boundary_utc_naive = boundary_tor.astimezone(UTC).replace(tzinfo=None)

    cache = ShopCache.query.order_by(ShopCache.fetched_at.desc()).first()

    # fetched_at in DB is naive UTC
    if cache and cache.fetched_at >= boundary_utc_naive:
        import json
        return json.loads(cache.raw_json)

    # Fetch fresh shop data
    r = requests.get(SHOP_URL, timeout=15)
    r.raise_for_status()
    data = r.json()

    import json
    new_cache = ShopCache(
        fetched_at=utc_now_naive(),  # store naive UTC
        raw_json=json.dumps(data)
    )
    db.session.add(new_cache)
    db.session.commit()

    return data
    """
    Cached shop:
    - If we already fetched AFTER the last 8pm boundary, use cache.
    - Otherwise fetch fresh from the API and store it.
    """
    now = dt.datetime.now(TZ)
    boundary = last_8pm_boundary(now)

    cache = ShopCache.query.order_by(ShopCache.fetched_at.desc()).first()
    if cache and cache.fetched_at >= boundary:
        # Use cache
        import json
        return json.loads(cache.raw_json)

    # Fetch fresh
    r = requests.get(SHOP_URL, timeout=15)
    r.raise_for_status()
    data = r.json()

    # Save cache
    import json
    new_cache = ShopCache(fetched_at=now, raw_json=json.dumps(data))
    db.session.add(new_cache)
    db.session.commit()

    return data

def parse_shop_items(shop_json: dict) -> list[dict]:
    shop_data = (shop_json or {}).get("data") or {}
    entries = shop_data.get("entries") or []
    out = []

    def best_image(entry: dict) -> str:
        # 1) newDisplayAsset (best shop tile)
        nda = entry.get("newDisplayAsset") or {}
        mi = nda.get("materialInstances") or []
        if mi and isinstance(mi, list):
            imgs = (mi[0].get("images") or {})
            for key in ("OfferImage", "TileImage", "Background"):
                if imgs.get(key):
                    return imgs[key]

        # 2) displayAsset fallback (sometimes present)
        da = entry.get("displayAsset") or {}
        if isinstance(da, dict):
            imgs = da.get("images") or {}
            for key in ("OfferImage", "TileImage", "Background"):
                if imgs.get(key):
                    return imgs[key]

        # 3) first item images fallback
        items = entry.get("items") or []
        if items and isinstance(items, list):
            images = (items[0].get("images") or {})
            return images.get("featured") or images.get("icon") or images.get("smallIcon") or ""

        return ""

    for e in entries:
        offer_id = str(e.get("offerId") or e.get("id") or "")
        price = e.get("finalPrice")

        section = (e.get("section") or {}).get("name") or "Shop"

        items = e.get("items") or []
        first = items[0] if items else {}

        name = first.get("name") or e.get("devName") or "Unknown Item"
        rarity = ((first.get("rarity") or {}).get("value")) or ""
        itype = ((first.get("type") or {}).get("value")) or ""
        description = first.get("description") or ""

        image = best_image(e)

        out.append({
            "section": section,
            "offer_id": offer_id,
            "name": name,
            "price": price,
            "rarity": rarity,
            "type": itype,
            "description": description,
            "image": image,
        })

    return out

# ---------------------------
# Routes
# ---------------------------
@app.route("/")
@app.route("/")
def shop():
    try:
        data = get_shop_data()
        items = parse_shop_items(data)
    except Exception as ex:
        items = []
        flash(f"Could not load item shop right now: {ex}", "error")

    wishlist_offer_ids = set()
    if current_user.is_authenticated:
        wishlist_offer_ids = {
            w.offer_id for w in WishlistItem.query.filter_by(user_id=current_user.id).all()
        }

    now = toronto_now()
    next_refresh = last_8pm_boundary_toronto(now) + dt.timedelta(days=1)

    return render_template(
        "shop.html",
        items=items,
        wishlist_offer_ids=wishlist_offer_ids,
        next_refresh=next_refresh,
        now=now,
    )
    
@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("shop"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if len(username) < 3:
            flash("Username must be at least 3 characters.", "error")
            return redirect(url_for("register"))
        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
            return redirect(url_for("register"))

        if User.query.filter_by(username=username).first():
            flash("That username is taken.", "error")
            return redirect(url_for("register"))

        u = User(
    username=username,
    password_hash=generate_password_hash(password, method="pbkdf2:sha256", salt_length=16)
)
        db.session.add(u)
        db.session.commit()
        login_user(u)
        flash("Account created. You are now logged in!", "success")
        return redirect(url_for("shop"))

    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("shop"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        u = User.query.filter_by(username=username).first()
        if not u or not check_password_hash(u.password_hash, password):
            flash("Invalid username or password.", "error")
            return redirect(url_for("login"))

        login_user(u)
        flash("Logged in!", "success")
        return redirect(url_for("shop"))

    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out.", "success")
    return redirect(url_for("shop"))

@app.route("/wishlist")
@login_required
def wishlist():
    items = WishlistItem.query.filter_by(user_id=current_user.id).order_by(WishlistItem.added_at.desc()).all()
    return render_template("wishlist.html", items=items)

@app.route("/api/wishlist/toggle", methods=["POST"])
@login_required
def toggle_wishlist():
    """
    Expects JSON:
    { offer_id, name, price, rarity, image }
    If exists -> remove
    If not -> add
    Returns: { in_wishlist: true/false }
    """
    payload = request.get_json(force=True) or {}
    offer_id = str(payload.get("offer_id") or "").strip()

    if not offer_id:
        return jsonify({"error": "Missing offer_id"}), 400

    existing = WishlistItem.query.filter_by(user_id=current_user.id, offer_id=offer_id).first()
    if existing:
        db.session.delete(existing)
        db.session.commit()
        return jsonify({"in_wishlist": False})

    w = WishlistItem(
        user_id=current_user.id,
        offer_id=offer_id,
        name=payload.get("name") or "Unknown Item",
        price=payload.get("price"),
        rarity=payload.get("rarity"),
        image=payload.get("image"),
    )
    db.session.add(w)
    db.session.commit()
    return jsonify({"in_wishlist": True})

@app.route("/initdb")
def initdb():
    """
    One-time helper route to create tables quickly.
    After it runs successfully once, you can remove this route.
    """
    db.create_all()
    return "DB initialized. You can now register/login."

# ---------------------------
# Run
# ---------------------------
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)

