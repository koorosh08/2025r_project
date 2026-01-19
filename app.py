# Fortnite item shop - app.py
import os
import json
import datetime as dt
from zoneinfo import ZoneInfo

import requests
from flask import Flask, render_template, redirect, url_for, request, flash, jsonify, Response
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, login_required, logout_user, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash


app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")

app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(app.root_path, "app.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"


FORTNITE_API_KEY = "cceac738-5d85ae1e-4306b609-15afd7d5"
SHOP_URL = "https://fortnite-api.com/v2/shop"

HEADERS = {
    "Authorization": FORTNITE_API_KEY,
    "User-Agent": "FortniteItemShopSchoolProject/1.0",
}


TZ = ZoneInfo("America/Toronto")
UTC = dt.timezone.utc


def toronto_now() -> dt.datetime:
    return dt.datetime.now(TZ)


def utc_now_naive() -> dt.datetime:
    return dt.datetime.now(UTC).replace(tzinfo=None)


def last_8pm_boundary_toronto(now_tor: dt.datetime) -> dt.datetime:
    """Most recent 8:00 PM Toronto time boundary."""
    today_8pm = now_tor.replace(hour=20, minute=0, second=0, microsecond=0)
    return today_8pm if now_tor >= today_8pm else (today_8pm - dt.timedelta(days=1))


def shop_day_str(now_tor: dt.datetime) -> str:
    """Shop rotates at 8pm Toronto -> label by last 8pm boundary date."""
    boundary = last_8pm_boundary_toronto(now_tor)
    return boundary.date().isoformat()  


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
    image = db.Column(db.Text, nullable=True)

    added_at = db.Column(db.DateTime, default=lambda: utc_now_naive())

    __table_args__ = (
        db.UniqueConstraint("user_id", "offer_id", name="uq_user_offer"),
    )


class ShopCache(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    fetched_at = db.Column(db.DateTime, nullable=False)  
    raw_json = db.Column(db.Text, nullable=False)


class ShopItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    shop_date = db.Column(db.String(10), index=True, nullable=False)  
    offer_id = db.Column(db.String(64), index=True, nullable=False)

    section = db.Column(db.String(80), nullable=True)
    name = db.Column(db.String(200), nullable=False)
    price = db.Column(db.Integer, nullable=True)
    rarity = db.Column(db.String(50), nullable=True)
    type = db.Column(db.String(50), nullable=True)
    description = db.Column(db.Text, nullable=True)
    image = db.Column(db.Text, nullable=True)

    __table_args__ = (
        db.UniqueConstraint("shop_date", "offer_id", name="uq_shopdate_offer"),
    )



@login_manager.user_loader
def load_user(user_id: str):
    return db.session.get(User, int(user_id))



def get_shop_data() -> dict:
    now_tor = toronto_now()
    boundary_tor = last_8pm_boundary_toronto(now_tor)
    boundary_utc_naive = boundary_tor.astimezone(UTC).replace(tzinfo=None)

    cache = ShopCache.query.order_by(ShopCache.fetched_at.desc()).first()
    if cache and cache.fetched_at >= boundary_utc_naive:
        return json.loads(cache.raw_json)

    r = requests.get(SHOP_URL, headers=HEADERS, timeout=25)
    r.raise_for_status()
    data = r.json()

    db.session.add(ShopCache(fetched_at=utc_now_naive(), raw_json=json.dumps(data)))
    db.session.commit()

    return data



def parse_shop_items(shop_json: dict) -> list[dict]:
    shop_data = (shop_json or {}).get("data") or {}
    entries = shop_data.get("entries") or []
    out: list[dict] = []

    def best_image(entry: dict) -> str:
        """
        Robust image picker for /v2/shop.
        Tries known paths first, then falls back to scanning entire entry
        for any image URL.
        """

        nda = entry.get("newDisplayAsset") or {}

        render_images = nda.get("renderImages") or []
        if isinstance(render_images, list) and render_images:
            img = render_images[0].get("image")
            if isinstance(img, str) and img.startswith("http"):
                return img

        mi_list = nda.get("materialInstances") or []
        if isinstance(mi_list, list):
            for mi in mi_list:
                imgs = (mi.get("images") or {})
                if isinstance(imgs, dict):
                    for key in ("OfferImage", "TileImage", "Background", "ItemShopTile"):
                        v = imgs.get(key)
                        if isinstance(v, str) and v.startswith("http"):
                            return v

        da = entry.get("displayAsset") or {}
        if isinstance(da, dict):
            imgs = da.get("images") or {}
            if isinstance(imgs, dict):
                for key in ("OfferImage", "TileImage", "Background", "ItemShopTile"):
                    v = imgs.get(key)
                    if isinstance(v, str) and v.startswith("http"):
                        return v

        for k in ("brItems", "items"):
            arr = entry.get(k) or []
            if isinstance(arr, list) and arr:
                images = (arr[0].get("images") or {})
                if isinstance(images, dict):
                    for key in ("featured", "icon", "smallIcon"):
                        v = images.get(key)
                        if isinstance(v, str) and v.startswith("http"):
                            return v

        found: list[str] = []

        def score_url(url: str) -> int:
            u = url.lower()
            s = 0
            for kw in ("offer", "tile", "shop", "render", "background"):
                if kw in u:
                    s += 10
            if any(ext in u for ext in (".png", ".webp", ".jpg", ".jpeg")):
                s += 5
            if "small" in u or "icon" in u:
                s -= 2
            return s

        def walk(x):
            if isinstance(x, dict):
                for v in x.values():
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
            elif isinstance(x, str):
                if x.startswith("http") and any(ext in x.lower() for ext in (".png", ".webp", ".jpg", ".jpeg")):
                    found.append(x)

        walk(entry)

        if found:
            found.sort(key=score_url, reverse=True)
            return found[0]

        return ""

    for e in entries:
        offer_id = str(e.get("offerId") or e.get("id") or "").strip()
        if not offer_id:
            continue

        price = e.get("finalPrice")
        section = (e.get("section") or {}).get("name") or "Shop"

        items = e.get("items") or []
        first = items[0] if isinstance(items, list) and items else {}

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


def save_shop_items_to_db(items: list[dict], shop_date: str) -> None:
    saved = 0
    for it in items:
        offer_id = (it.get("offer_id") or "").strip()
        if not offer_id:
            continue

        row = ShopItem.query.filter_by(shop_date=shop_date, offer_id=offer_id).first()
        if not row:
            row = ShopItem(shop_date=shop_date, offer_id=offer_id)
            db.session.add(row)

        row.section = it.get("section")
        row.name = it.get("name") or "Unknown Item"
        row.price = it.get("price")
        row.rarity = it.get("rarity")
        row.type = it.get("type")
        row.description = it.get("description")
        row.image = it.get("image")

        saved += 1

    db.session.commit()
    print(f"[DB] Saved/Updated {saved} items for shop_date={shop_date}")



@app.route("/")
def shop():
    now = toronto_now()
    sd = shop_day_str(now)
    next_refresh = last_8pm_boundary_toronto(now) + dt.timedelta(days=1)

    items_db = ShopItem.query.filter_by(shop_date=sd).all()

    if items_db:
        missing = sum(1 for x in items_db if not x.image)
        if missing > len(items_db) * 0.25:
            try:
                data = get_shop_data()
                items = parse_shop_items(data)
                save_shop_items_to_db(items, sd)
                items_db = ShopItem.query.filter_by(shop_date=sd).all()
            except Exception as ex:
                flash(f"Could not refresh images: {ex}", "error")

        items = [{
            "section": x.section,
            "offer_id": x.offer_id,
            "name": x.name,
            "price": x.price,
            "rarity": x.rarity,
            "type": x.type,
            "description": x.description,
            "image": x.image,
        } for x in items_db]

    else:
        try:
            data = get_shop_data()
            items = parse_shop_items(data)
            save_shop_items_to_db(items, sd)
        except Exception as ex:
            items = []
            flash(f"Could not load item shop: {ex}", "error")

    wishlist_offer_ids = set()
    if current_user.is_authenticated:
        wishlist_offer_ids = {
            w.offer_id for w in WishlistItem.query.filter_by(user_id=current_user.id).all()
        }

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
            password_hash=generate_password_hash(password, method="pbkdf2:sha256", salt_length=16),
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
    items = WishlistItem.query.filter_by(user_id=current_user.id).order_by(WishlistItem.id.desc()).all()
    return render_template("wishlist.html", items=items)


@app.route("/api/wishlist/toggle", methods=["POST"])
@login_required
def toggle_wishlist():
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
    db.create_all()
    return "DB initialized."


@app.route("/debug/shopjson")
def debug_shopjson():
    data = get_shop_data()
    return Response(json.dumps(data, indent=2), mimetype="application/json")


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)
