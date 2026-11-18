"""ALTAF Telegram bot: 3-month ALTF mining campaign + project introduction.
Run: python -m app.tgbot (env TG_BOT_TOKEN). Rewards are paid into linked ALTAF Exchange accounts when the campaign ends."""
import logging, os, secrets, time
from datetime import datetime, timedelta
from decimal import Decimal
import httpx
from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, func, select
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base, SessionLocal, User, N, init_db, WRITE_LOCK
from .ledger import setting, set_setting, transfer, bal
from . import referral

log = logging.getLogger("tgbot")
TOKEN = os.environ.get("TG_BOT_TOKEN", "")
API = f"https://api.telegram.org/bot{TOKEN}/"
POOL = Decimal(os.environ.get("TG_POOL", "100000"))
DAYS = int(os.environ.get("TG_DAYS", "90"))
CLAIM = Decimal(os.environ.get("TG_CLAIM", "10"))
EVERY = timedelta(hours=3)
INVITE_BONUS, INVITE_SHARE = Decimal("50"), Decimal("0.10")
SITE = os.environ.get("ALTAF_SITE", "https://altaf.187-126-9-68.sslip.io")


class TgUser(Base):
    __tablename__ = "tg_users"
    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(80), default="")
    lang: Mapped[str] = mapped_column(String(5), default="ar")
    mined: Mapped[Decimal] = mapped_column(N(), default=0)
    claims: Mapped[int] = mapped_column(Integer, default=0)
    last_claim: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    invited_by: Mapped[int] = mapped_column(BigInteger, nullable=True, index=True)
    invites: Mapped[int] = mapped_column(Integer, default=0)
    user_id: Mapped[int] = mapped_column(Integer, nullable=True, index=True)
    paid: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TgLink(Base):
    __tablename__ = "tg_links"
    code: Mapped[str] = mapped_column(String(8), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer)
    expires: Mapped[datetime] = mapped_column(DateTime)


def new_link_code(db, uid):
    code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
    db.add(TgLink(code=code, user_id=uid, expires=datetime.utcnow() + timedelta(minutes=15)))
    return code


T = {
    "ar": {
        "welcome": "مرحباً {n} في ALTAF ⛏\nعدّن عملة ALTF مجاناً: اضغط «عدّن» كل 3 ساعات، وادعُ أصدقاءك لتربح أكثر.\nالحملة تنتهي بعد {d} يوماً، وتُحوَّل أرباحك إلى حسابك في منصة ALTAF.",
        "mine": "⛏ عدّن", "invite": "👥 ادعُ أصدقاءك", "me": "📊 رصيدي", "about": "ℹ️ عن ALTAF", "links": "🌐 روابط", "link": "🔗 ربط حسابي",
        "mined": "⛏ ربحت {a} ALTF. رصيدك الآن {b} ALTF.\nعُد بعد 3 ساعات.", "wait": "⏳ التعدين التالي بعد {m} دقيقة. رصيدك {b} ALTF.",
        "ended": "انتهت حملة التعدين. أرباحك {b} ALTF تُحوَّل إلى حسابك في المنصة بعد ربطه.", "empty": "نفدت جوائز الحملة. شكراً لمشاركتك!",
        "invitetxt": "رابط دعوتك:\n{l}\n\nلكل صديق ينضم ويعدّن: {x} ALTF، و10% من كل تعدين له.\nدعوت حتى الآن: {i}",
        "metxt": "📊 رصيدك: {b} ALTF\nمرات التعدين: {c}\nالمدعوون: {i}\nالحساب في المنصة: {s}\nتبقّى من الحملة: {d} يوماً",
        "linked": "مربوط ✅", "notlinked": "غير مربوط — اضغط «ربط حسابي»",
        "abouttxt": "ALTAF منظومة كاملة:\n• منصة تداول: فوري، عقود، P2P، Earn، Launchpad، بوتات، API\n• عملة ALTF: 21 مليوناً فقط، عقد موثّق على TRON\n• شبكة ALTAF: سلسلة خاصة يعدّنها الجميع بالحاسوب وبأجهزة ASIC\n• 16 سيرفراً للشركاء، لكل واحد مليون ALTF وصوت في القرارات",
        "linkhelp": "لربط حسابك:\n1. ادخل المنصة {s} وسجّل.\n2. في صفحة «الحساب» اضغط «ربط تلقرام» فيظهر رمز.\n3. أرسل هنا: /link الرمز",
        "linkok": "✅ رُبط حسابك في المنصة. ستصلك أرباحك عند نهاية الحملة.", "linkbad": "الرمز غير صحيح أو انتهت صلاحيته. اطلب رمزاً جديداً من صفحة «الحساب».",
        "paid": "🎉 حُوِّلت {b} ALTF إلى حسابك في منصة ALTAF.",
    },
    "fr": {
        "welcome": "Bienvenue {n} sur ALTAF ⛏\nMinez des ALTF gratuitement : appuyez sur « Miner » toutes les 3 heures et invitez vos amis pour gagner plus.\nLa campagne se termine dans {d} jours ; vos gains sont versés sur votre compte ALTAF Exchange.",
        "mine": "⛏ Miner", "invite": "👥 Inviter", "me": "📊 Mon solde", "about": "ℹ️ À propos", "links": "🌐 Liens", "link": "🔗 Lier mon compte",
        "mined": "⛏ +{a} ALTF. Solde : {b} ALTF.\nRevenez dans 3 heures.", "wait": "⏳ Prochain minage dans {m} min. Solde : {b} ALTF.",
        "ended": "La campagne est terminée. Vos {b} ALTF seront versés sur votre compte après liaison.", "empty": "Les récompenses de la campagne sont épuisées. Merci !",
        "invitetxt": "Votre lien d'invitation :\n{l}\n\nPar ami qui rejoint et mine : {x} ALTF + 10 % de chacun de ses minages.\nInvités : {i}",
        "metxt": "📊 Solde : {b} ALTF\nMinages : {c}\nInvités : {i}\nCompte Exchange : {s}\nJours restants : {d}",
        "linked": "lié ✅", "notlinked": "non lié — appuyez sur « Lier mon compte »",
        "abouttxt": "ALTAF est un écosystème complet :\n• Exchange : spot, futures, P2P, Earn, Launchpad, bots, API\n• ALTF : 21 millions seulement, contrat vérifié sur TRON\n• Réseau ALTAF : blockchain minable par PC et ASIC\n• 16 serveurs partenaires, 1 million d'ALTF et un vote chacun",
        "linkhelp": "Pour lier votre compte :\n1. Inscrivez-vous sur {s}.\n2. Page « Compte » → « Lier Telegram » pour obtenir un code.\n3. Envoyez ici : /link CODE",
        "linkok": "✅ Compte lié. Vos gains arriveront à la fin de la campagne.", "linkbad": "Code invalide ou expiré. Demandez-en un nouveau sur la page « Compte ».",
        "paid": "🎉 {b} ALTF versés sur votre compte ALTAF Exchange.",
    },
    "en": {
        "welcome": "Welcome {n} to ALTAF ⛏\nMine ALTF for free: tap “Mine” every 3 hours and invite friends to earn more.\nThe campaign ends in {d} days; your earnings are paid to your ALTAF Exchange account.",
        "mine": "⛏ Mine", "invite": "👥 Invite friends", "me": "📊 My balance", "about": "ℹ️ About ALTAF", "links": "🌐 Links", "link": "🔗 Link account",
        "mined": "⛏ +{a} ALTF. Balance: {b} ALTF.\nCome back in 3 hours.", "wait": "⏳ Next mining in {m} min. Balance: {b} ALTF.",
        "ended": "The mining campaign has ended. Your {b} ALTF will be paid to your exchange account once linked.", "empty": "Campaign rewards are used up. Thank you!",
        "invitetxt": "Your invite link:\n{l}\n\nFor each friend who joins and mines: {x} ALTF + 10% of every claim they make.\nInvited so far: {i}",
        "metxt": "📊 Balance: {b} ALTF\nClaims: {c}\nInvited: {i}\nExchange account: {s}\nDays left: {d}",
        "linked": "linked ✅", "notlinked": "not linked — tap “Link account”",
        "abouttxt": "ALTAF is a full ecosystem:\n• Exchange: spot, futures, P2P, Earn, Launchpad, bots, API\n• ALTF: only 21 million, verified contract on TRON\n• ALTAF Network: a blockchain anyone can mine with a PC or ASIC\n• 16 partner servers, 1 million ALTF and a vote each",
        "linkhelp": "To link your account:\n1. Sign up at {s}.\n2. On the “Account” page tap “Link Telegram” to get a code.\n3. Send here: /link CODE",
        "linkok": "✅ Account linked. Your earnings arrive when the campaign ends.", "linkbad": "Invalid or expired code. Get a new one on the “Account” page.",
        "paid": "🎉 {b} ALTF were paid to your ALTAF Exchange account.",
    },
}


def tr(u, k, **kw):
    return T.get(u.lang if u else "ar", T["en"])[k].format(**kw)


def fmt(x):
    return f"{Decimal(x).quantize(Decimal('0.01')):,}"


def call(method, **params):
    r = httpx.post(API + method, json=params, timeout=60)
    j = r.json()
    if not j.get("ok"):
        log.warning("telegram %s: %s", method, j.get("description"))
    return j.get("result")


def start_time(db):
    v = setting(db, "tg_start")
    if not v:
        v = str(int(time.time()))
        set_setting(db, "tg_start", v)
        db.commit()
    return datetime.utcfromtimestamp(int(v))


def end_time(db):
    return start_time(db) + timedelta(days=DAYS)


def days_left(db):
    return max(0, (end_time(db) - datetime.utcnow()).days)


def keyboard(u):
    b = lambda k: {"text": tr(u, k), "callback_data": k}
    return {"inline_keyboard": [[b("mine")], [b("invite"), b("me")], [b("about"), b("links")], [b("link")]]}


def links_kb():
    return {"inline_keyboard": [[{"text": "ALTAF Exchange", "url": SITE}],
                                [{"text": "Whitepaper", "url": SITE + "/static/ALTAF-Whitepaper.pdf"}],
                                [{"text": "Android app", "url": SITE + "/static/ALTAF.apk"}],
                                [{"text": "ALTAF Network", "url": "https://altafnet.187-126-9-68.sslip.io"}],
                                [{"text": "GitHub", "url": "https://github.com/ridha20259/altaf"}]]}


def get_user(db, frm):
    u = db.get(TgUser, frm["id"])
    if not u:
        lc = (frm.get("language_code") or "ar")[:2]
        u = TgUser(tg_id=frm["id"], name=(frm.get("first_name") or "")[:80], lang=lc if lc in T else "en", mined=Decimal(0))
        db.add(u)
        db.flush()
    return u


def resolve_inviter(db, code, me):
    code = (code or "").strip()
    if code.lower().startswith("t") and code[1:].isdigit():
        inv = db.get(TgUser, int(code[1:]))
        return inv.tg_id if inv and inv.tg_id != me else None
    r = db.scalars(select(referral.Referral).where(referral.Referral.code == code.upper())).first()
    if not r:
        return None
    inv = db.scalars(select(TgUser).where(TgUser.user_id == r.user_id)).first()
    if not inv:  # exchange user without Telegram: a virtual row holds their bonus until payout
        inv = db.get(TgUser, -r.user_id) or TgUser(tg_id=-r.user_id, user_id=r.user_id, mined=Decimal(0))
        db.add(inv)
        db.flush()
    return inv.tg_id if inv.tg_id != me else None


def pool_left(db):
    return POOL - (db.scalar(select(func.coalesce(func.sum(TgUser.mined), 0))) or 0)


def credit(db, row, amount):
    amount = min(Decimal(amount), pool_left(db))
    if amount > 0:
        row.mined = Decimal(row.mined or 0) + amount
    return amount


def mine(db, u):
    if datetime.utcnow() >= end_time(db):
        return tr(u, "ended", b=fmt(u.mined))
    if u.last_claim and datetime.utcnow() - u.last_claim < EVERY:
        m = int((EVERY - (datetime.utcnow() - u.last_claim)).total_seconds() // 60) + 1
        return tr(u, "wait", m=m, b=fmt(u.mined))
    got = credit(db, u, CLAIM)
    if got <= 0:
        return tr(u, "empty")
    first = u.claims == 0
    u.claims += 1
    u.last_claim = datetime.utcnow()
    if u.invited_by:
        inv = db.get(TgUser, u.invited_by)
        if inv:
            credit(db, inv, got * INVITE_SHARE + (INVITE_BONUS if first else 0))
    return tr(u, "mined", a=fmt(got), b=fmt(u.mined))


def invite_link(db, u):
    if u.user_id:
        code = referral.row(db, u.user_id).code
    else:
        code = f"t{u.tg_id}"
    return f"https://t.me/{setting(db, 'tg_bot_username')}?start={code}"


def link(db, u, code):
    row = db.get(TgLink, (code or "").strip().upper())
    if not row or row.expires < datetime.utcnow():
        return tr(u, "linkbad")
    other = db.scalars(select(TgUser).where(TgUser.user_id == row.user_id, TgUser.tg_id != u.tg_id)).first()
    if other and other.tg_id > 0:
        return tr(u, "linkbad")
    if other:  # merge the virtual bonus row (minus any bonus the user earned by inviting himself)
        extra, invites = Decimal(other.mined or 0), other.invites
        if u.invited_by == other.tg_id:
            extra -= (INVITE_BONUS if u.claims else 0) + INVITE_SHARE * CLAIM * u.claims
            invites -= 1
            u.invited_by = None
        u.mined = Decimal(u.mined or 0) + max(Decimal(0), extra)
        u.invites += max(0, invites)
        for x in db.scalars(select(TgUser).where(TgUser.invited_by == other.tg_id, TgUser.tg_id != u.tg_id)):
            x.invited_by = u.tg_id
        db.delete(other)
    u.user_id = row.user_id
    db.delete(row)
    if u.invited_by:
        inv = db.get(TgUser, u.invited_by)
        if inv and inv.user_id:
            referral.attach(db, row.user_id, referral.row(db, inv.user_id).code)
    return tr(u, "linkok")


def payout(db):
    """After the campaign: move mined ALTF from the pool account into linked users' exchange accounts."""
    if datetime.utcnow() < end_time(db):
        return
    pool_uid = int(setting(db, "tg_pool_uid") or 0) or db.scalar(select(User.id).where(User.is_admin == True).order_by(User.id))  # noqa: E712
    if not pool_uid:
        log.warning("tg payout: no pool account (set setting tg_pool_uid)")
        return
    for u in db.scalars(select(TgUser).where(TgUser.paid == False, TgUser.user_id != None, TgUser.mined > 0)).all():  # noqa: E711,E712
        amt = Decimal(u.mined).quantize(Decimal("0.000001"))
        if bal(db, pool_uid, "ALTF").available < amt:
            log.warning("tg payout: pool account %s needs %s ALTF", pool_uid, amt)
            return
        with WRITE_LOCK:
            transfer(db, pool_uid, u.user_id, "ALTF", amt, "tg_mining", f"tg{u.tg_id}")
            u.paid = True
            db.commit()
        if u.tg_id > 0:
            call("sendMessage", chat_id=u.tg_id, text=tr(u, "paid", b=fmt(amt)))


def handle(db, up):
    if "callback_query" in up:
        q = up["callback_query"]
        u = get_user(db, q["from"])
        k = q.get("data", "")
        call("answerCallbackQuery", callback_query_id=q["id"])
        text, kb = {"mine": lambda: mine(db, u),
                    "invite": lambda: tr(u, "invitetxt", l=invite_link(db, u), x=fmt(INVITE_BONUS), i=u.invites),
                    "me": lambda: tr(u, "metxt", b=fmt(u.mined), c=u.claims, i=u.invites, d=days_left(db),
                                     s=tr(u, "linked") if u.user_id else tr(u, "notlinked")),
                    "about": lambda: tr(u, "abouttxt"),
                    "links": lambda: "ALTAF",
                    "link": lambda: tr(u, "linkhelp", s=SITE)}.get(k, lambda: tr(u, "abouttxt"))(), None
        db.commit()
        call("sendMessage", chat_id=q["message"]["chat"]["id"], text=text, reply_markup=links_kb() if k == "links" else keyboard(u),
             disable_web_page_preview=True)
        return
    m = up.get("message")
    if not m or "from" not in m or m["chat"].get("type") != "private":
        return
    u = get_user(db, m["from"])
    t = (m.get("text") or "").strip()
    if t.startswith("/start"):
        arg = t.split(maxsplit=1)[1] if " " in t else ""
        if arg and not u.invited_by and u.claims == 0:
            inv = resolve_inviter(db, arg, u.tg_id)
            if inv:
                u.invited_by = inv
                db.get(TgUser, inv).invites += 1
        text = tr(u, "welcome", n=u.name or "", d=days_left(db))
    elif t.startswith("/link"):
        text = link(db, u, t.split(maxsplit=1)[1] if " " in t else "")
    else:
        text = tr(u, "welcome", n=u.name or "", d=days_left(db))
    db.commit()
    call("sendMessage", chat_id=m["chat"]["id"], text=text, reply_markup=keyboard(u), disable_web_page_preview=True)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not TOKEN:
        raise SystemExit("TG_BOT_TOKEN not set")
    init_db()
    me = call("getMe")
    db = SessionLocal()
    set_setting(db, "tg_bot_username", me["username"])
    start_time(db)
    db.commit()
    db.close()
    call("setMyCommands", commands=[{"command": "start", "description": "ALTAF"}, {"command": "link", "description": "Link ALTAF Exchange account"}])
    off, last_pay = 0, 0
    while True:
        try:
            ups = httpx.post(API + "getUpdates", json={"offset": off, "timeout": 50}, timeout=70).json().get("result", [])
        except Exception as e:
            log.warning("getUpdates: %s", e)
            time.sleep(5)
            continue
        for up in ups:
            off = up["update_id"] + 1
            db = SessionLocal()
            try:
                handle(db, up)
            except Exception:
                db.rollback()
                log.exception("update %s", up.get("update_id"))
            finally:
                db.close()
        if time.time() - last_pay > 600:
            last_pay = time.time()
            db = SessionLocal()
            try:
                payout(db)
            except Exception:
                db.rollback()
                log.exception("payout")
            finally:
                db.close()


if __name__ == "__main__":
    main()
